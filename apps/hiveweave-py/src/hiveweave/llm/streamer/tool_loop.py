"""Tool-loop orchestration mixin."""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Callable

import structlog

from hiveweave.llm.provider import ProviderConfig

from .constants import (
    ACTIVITY_EXTEND_S,
    BUDGET_PACING_HINT_S,
    DEFAULT_PLACEHOLDER,
    FORCE_COMMIT_GRACE_ROUNDS,
    FORCE_COMMIT_ROUNDS,
    HARD_TOTAL_TIMEOUT_S,
    MAX_TOOLS_PER_ROUND,
    MIN_ROUND_BUDGET_S,
    MIN_TOOL_BUDGET_S,
    NO_TEXT_HINT_MAX,
    NO_TEXT_ROUNDS_THRESHOLD,
    TOOL_BUDGET_GRACE_S,
    TOOL_LOOP_READONLY_STALL_LIMIT,
    TOOL_LOOP_STALL_LIMIT,
    TOTAL_TIMEOUT_S,
    TRUNCATED_TOOL_CALL_ROUNDS_LIMIT,
    TURN_STREAM_CUT_GRACE_S,
)
from .doom_loop import (
    BLOCKED_STALL_LIMIT,
    STALL_REASON_BLOCKED,
    STALL_REASON_NO_PROGRESS,
    STALL_REASON_READONLY,
    STALL_REASON_RUNNER_FAILED,
    STALL_REASON_TOOL_FAILED,
    TOOL_FAIL_STALL_LIMIT,
    classify_stall_round,
    doom_loop_limit,
    fail_signature_for_round,
    readonly_fingerprint,
)
# F8：advisory 重复提醒（3/5/8，never block，per-agent 计数）
from .advisory import advisory_guard
from .types import DeltaCallback, ToolCallCallback

log = structlog.get_logger(__name__)


def _round_fact_flags(
    tool_results: list[dict[str, Any]], error_ids: set[str]
) -> dict[str, bool]:
    """F4：从本轮工具回执聚合正交事实位（runner_failed / command_failed /
    blocked）。供 F8 advisory 生成归因一句话。best-effort：任何失败回执带
    blocked=true 即 blocked；优先 runner_failed（DSH 顺序：runner 失败优先
    于 denial —— 命令没跑起来时只能记 runner）。
    """
    flags = {"runner_failed": False, "command_failed": False, "blocked": False}
    try:
        for tr in tool_results:
            tid = tr.get("tool_call_id") or ""
            if tid and tid not in error_ids:
                continue
            if tr.get("blocked"):
                flags["blocked"] = True
            if tr.get("runner_failed"):
                flags["runner_failed"] = True
            elif tr.get("command_failed"):
                flags["command_failed"] = True
    except Exception:
        pass
    return flags


def _is_valid_json_arguments(arguments: Any) -> bool:
    """tool_call arguments 合法性（截断检测）。

    空串/None = 无参调用，合法；非字符串（已结构化）合法；
    非空字符串必须可 json.loads —— SSE 提前断流时 arguments 只收到
    半截 JSON（TEST_DSH_16 实证：round 1 finish=None，round 2 网关
    400 "`arguments` must be valid JSON"）。
    """
    if arguments is None or arguments == "":
        return True
    if not isinstance(arguments, str):
        return True
    try:
        json.loads(arguments)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _assistant_with_reasoning(
    content: str, supports_thinking: bool, reasoning: str
) -> dict:
    """Final assistant 消息：reasoning 模型把本轮 thinking 一并落账。

    供 build_display_segments 原位保留 thinking 块（DSH 整轮视图）——
    主收口与预算/截断/内容过滤等提前收口路径统一，避免末轮思考在
    done reload 后因 segments 缺 thinking 块而丢失（segments 含
    中间轮 thinking 时 MessageBubble 会隐藏 _thinking 列）。
    """
    msg: dict = {"role": "assistant", "content": content}
    if supports_thinking and reasoning:
        msg["reasoning_content"] = reasoning
    return msg


class ToolLoopMixin:
    """Tool loop main cycle for Streamer."""

    if TYPE_CHECKING:
        max_tool_rounds: Any
        _strip_placeholder: Any
        _fire_delta: Any
        _error_result: Any
        _trim_context_if_needed: Any
        _pressure_compact_if_needed: Any
        _maybe_inject_mid_round_reminder: Any
        _stream_with_empty_retry: Any
        _detect_doom_loop: Any
        _execute_tools: Any
        _make_max_rounds_summary: Any

    @staticmethod
    def _normalize_usage(
        usage: dict | None, provider: str | None = None
    ) -> dict | None:
        """归一化一轮 usage（provider 差异 + total 口径），供 token metering 使用。"""
        from hiveweave.llm.util import normalize_usage

        return normalize_usage(usage, provider)

    def _budget_exhausted_result(
        self,
        *,
        text_acc: str,
        thinking_acc: str,
        tool_history: list[dict],
        tool_turn_acc: list[dict],
        round_num: int,
        last_usage: dict | None,
        usage_rounds: list[dict],
        note: str | None = None,
        reason: str = "hard_budget",
        current_reasoning: str = "",
    ) -> dict:
        """硬预算耗尽的优雅收口（所有预算闸口共用）。

        保留全部 tool 产出与已累积文本，明确指引 commit_turn 续跑 ——
        与「agent SAFETY_TIMEOUT 强杀 → run interrupted + 90s 冷却 +
        上下文重建」相比，这条路径零损失：下一轮 wake 直接继续。

        疏通（2026-08-08）：无论是否有累积文本都附收口说明 —— 下轮
        wake 的 agent 能从历史中读懂「上轮为何结束、如何继续」；否则
        它面对一截突兀的半截文本，不知预算耗尽，容易重试同款重型
        操作再次撞闸。

        E14：``note``/``reason`` 可覆盖默认措辞 —— 轮次疏导上限
        （force_commit_rounds）宽限轮用尽后同样走本收口，仅文案区分。

        ``current_reasoning``：当前（被切断）轮的 thinking，仅补本轮
        （不是 accumulated）—— 中间轮 thinking 已由各轮 assistant
        消息的 reasoning_content 落账，避免重复。
        """
        base = self._strip_placeholder(text_acc)
        if note is None:
            note = (
                "[TURN BUDGET] Hard turn budget exhausted — all progress so "
                "far is kept. Call commit_turn(phase='in_progress') to "
                "checkpoint, then continue next wake with smaller slices "
                "(split large operations; prefer background + polling over "
                "long blocking calls)."
            )
        final_text = f"{base}\n\n{note}" if base else note
        final_msg = {"role": "assistant", "content": final_text}
        # 仅补当前被切断轮的 thinking（调用方已按 provider.supports_thinking
        # 门控 current_reasoning）；中间轮已由各轮 reasoning_content 落账。
        if current_reasoning:
            final_msg["reasoning_content"] = current_reasoning
        tool_turn_acc.append(final_msg)
        return {
            "status": "ok",
            "content": final_text,
            "thinking": thinking_acc,
            "tool_calls": tool_history,
            "tool_turn_messages": tool_turn_acc,
            "rounds": round_num,
            "usage": last_usage,
            "usage_rounds": usage_rounds,
            "budget_exhausted": True,
            "steering_reason": reason,
        }

    async def _run_tool_loop(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        on_tool_call: ToolCallCallback | None,
        max_tool_rounds: int | None = None,
        steer_queue: asyncio.Queue | None = None,
        usage_sink: Callable[[dict], None] | None = None,
    ) -> dict:
        """Tool loop: 流式请求 → 检查 tool_calls → 执行工具 → 重复。

        ``steer_queue``：插话通道。每轮开头的 next-step 窗口 poll 其中消息，
        作为 user 消息注入本轮 LLM 请求（参考 DSH session.prompt(mode='steer')），
        让运行中的 turn 不必等整轮结束即可响应用户插话。
        """
        # 使用调用方传入的上限，回退到实例默认值
        rounds_cap = max_tool_rounds if max_tool_rounds else self.max_tool_rounds
        # F8：新 run（新一轮用户消息/唤醒）重置 advisory 计数 —— 计数只在
        # 同一轮内追踪连续失败（对齐 DSH「new user prompt resets」）。
        advisory_guard.reset_for_user_message(agent_id)
        text_acc = ""
        thinking_acc = ""
        tool_history: list[dict] = []
        tool_turn_acc: list[dict] = []
        last_usage: dict | None = None
        no_text_rounds = 0
        no_text_hint_count = 0  # 无文字提示注入次数，超过 NO_TEXT_HINT_MAX 时 break
        # R2: 跟踪连续相同的 (tool_name, tool_args) 调用。
        # 累加式计数会误判合法的跨轮重复操作；改为「连续相同」计数，
        # 遇到不同调用时重置。只在连续 DOOM_LOOP_THRESHOLD 次相同调用时才判定。
        doom_tracker: dict[str, Any] = {"last_key": None, "count": 0}

        # Doom loop 警告标志: 第一次触发时注入反馈给 LLM 纠正机会，
        # 只有第二次再次触发才真正中断。
        doom_warning_given: bool = False

        # Bug-5 修复: 跟踪本对话是否已注入过占位符，避免 LLM 把占位符当
        # 自己的输出后陷入 "调工具不说话 → 占位注入 → LLM 看到 '好的开始处理'
        # → 不结束 → 再调工具 → 再注入" 的死循环。
        placeholder_injected: bool = False

        # Per-turn poll fingerprint counts (TEST4 get_tasks hard reject)
        poll_turn_counts: dict[tuple[str, str], int] = {}

        # DESIGN-2: Magentic-One stall counter — consecutive no-progress rounds
        stall_count = 0
        readonly_stall_count = 0
        # P0-1: 跨轮历史只读指纹（工具名, 规范化参数）—— readonly stall 只
        # 计「同参重复」轮，读不同文件/参数的合法只读轮不算空转。
        seen_readonly_fingerprints: set[tuple[str, str]] = set()
        # H3: 平台护栏拒绝轮（blocked）独立计数 —— 护栏拒绝是平台拒环境，
        # 不是模型空转，不累计普通 stall_count；超过 BLOCKED_STALL_LIMIT
        # 仍收口兜底。
        blocked_stall_count = 0
        # DSH_33: 工具自身执行失败轮独立计数 —— 与 blocked_stall_count 同为
        # 正交事实位。收口时机不变（限值同 TOOL_LOOP_STALL_LIMIT），但归因
        # 落到工具层，文案不再说「模型无进展」。
        tool_fail_stall_count = 0
        # P0-1（R3）：同源失败判据 —— 上轮失败指纹（tool_name, args[:60]）。
        # 同指纹 = 原地撞同一面墙（2 轮快收口）；指纹变化 = 试错/方向在变
        # （不快速掐，交给普通 stall_count 总限兜底）。
        last_fail_signature: tuple[str, str] | None = None
        # 触发收口那一轮的归因（供文案 / 返回值 stall_reason）。
        last_stall_reason: str | None = None

        # 连续「arguments 截断」轮计数（TRUNCATED_TOOL_CALL_ROUNDS_LIMIT 兜底）
        truncated_rounds = 0

        # Turn budget（写死启用，见 constants.py 顶部说明）: session wall clock。
        loop_start = time.monotonic()
        hard_deadline = loop_start + HARD_TOTAL_TIMEOUT_S
        soft_deadline = loop_start + TOTAL_TIMEOUT_S
        budget_hint_injected = False
        # pacing 提示一次性；记录注入轮次供 soft 提示做【同轮】去重 ——
        # 永久性压制会让 soft（临近硬截止的最后通牒）在默认常量下成死代码：
        # pacing（剩余<300s）恒早于 soft（剩余≤30s）触发，凡走到 soft 的
        # turn 必然已注入 pacing（审计 2026-08-08 P1）。
        pacing_hint_injected = False
        pacing_hint_round = -1
        # E14: 轮次疏导线提示一次性（不重复塞系统消息浪费 token）
        force_commit_hint_injected = False

        # Token metering: 累加每轮归一化 usage（供 agent 层落库）。
        # 每轮只保留末轮 usage 在 last_usage，中间轮在这里累积。
        usage_rounds: list[dict] = []

        for round_num in range(rounds_cap):
            now_mono = time.monotonic()
            if now_mono >= hard_deadline:
                log.warning(
                    "stream_budget_hard_exhausted",
                    agent_id=agent_id,
                    round=round_num,
                    elapsed_s=round(now_mono - loop_start, 1),
                    tools=len(tool_history),
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.stream_budget_exhausted(agent_id)
                except Exception:
                    pass
                if tool_history:
                    return self._budget_exhausted_result(
                        text_acc=text_acc,
                        thinking_acc=thinking_acc,
                        tool_history=tool_history,
                        tool_turn_acc=tool_turn_acc,
                        round_num=round_num,
                        last_usage=last_usage,
                        usage_rounds=usage_rounds,
                    )
                await self._fire_delta(on_delta, {
                    "type": "error",
                    "content": f"请求总超时（{HARD_TOTAL_TIMEOUT_S}s）",
                })
                return self._error_result("请求总超时", loop_start)
            # 新轮预算闸门（结构性修复 2026-08-07）：硬截止只在轮间检查
            # 不够 —— 单轮（流式 + 工具批）时长无界，可冲过 HARD 直至 agent
            # SAFETY_TIMEOUT 强杀。剩余预算买不起一轮有意义的 LLM 请求时
            # （思考模型首 chunk 即可达 90s），不开新轮，直接优雅收口。
            # 首轮（无 tool_history）不受此闸门限制 —— 预算刚起始必充足。
            if (
                tool_history
                and hard_deadline - now_mono < MIN_ROUND_BUDGET_S
            ):
                log.warning(
                    "stream_budget_round_gate",
                    agent_id=agent_id,
                    round=round_num,
                    remaining_s=round(hard_deadline - now_mono, 1),
                    tools=len(tool_history),
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.stream_budget_exhausted(agent_id)
                except Exception:
                    pass
                return self._budget_exhausted_result(
                    text_acc=text_acc,
                    thinking_acc=thinking_acc,
                    tool_history=tool_history,
                    tool_turn_acc=tool_turn_acc,
                    round_num=round_num,
                    last_usage=last_usage,
                    usage_rounds=usage_rounds,
                )
            # 预算 pacing 提示（疏通层 2026-08-08）：在撞任何闸口【之前】
            # 把预算状态告诉 agent —— 剩余硬预算首次低于阈值（默认 300s，
            # 约半程）时注入一次温和提示，让它主动规划收口、避免启动
            # 全量测试/大重构/长阻塞轮询等重型操作。一次性、不打断当前轮；
            # agent 不收口才轮到闸口（堵截兜底）动手。
            if (
                not pacing_hint_injected
                and tool_history
                and hard_deadline - now_mono < BUDGET_PACING_HINT_S
            ):
                pacing_hint_injected = True
                pacing_hint_round = round_num
                messages.append({
                    "role": "system",
                    "content": (
                        f"[TURN BUDGET] ~{int(now_mono - loop_start)}s "
                        f"elapsed, ~{int(hard_deadline - now_mono)}s of "
                        "hard turn budget left. Plan "
                        "your exit: finish the current slice and checkpoint "
                        "with commit_turn(phase='in_progress'). Avoid "
                        "launching new heavy operations (full test suites, "
                        "large refactors, long blocking polls) — they will "
                        "be cut off when the budget runs out."
                    ),
                })
            # soft 提示与 pacing 提示职责重叠（都是催收口）：同轮不重复
            # 注入（两条措辞相似的 [TURN BUDGET] 浪费 token）；但 pacing 在
            # 更早轮次注入过【不】压制 soft —— soft 是临近硬截止的最后通牒
            # （附 soft 续命给 commit_turn 留窗），强度不同，缺了它 agent
            # 在半程温和提示之后直到撞闸再无警告（审计 2026-08-08 P1）。
            if (
                now_mono >= soft_deadline
                and not budget_hint_injected
                and pacing_hint_round != round_num
                and tool_history
            ):
                budget_hint_injected = True
                remaining = max(0.0, hard_deadline - now_mono)
                messages.append({
                    "role": "system",
                    "content": (
                        f"[TURN BUDGET] Soft budget reached "
                        f"(~{int(now_mono - loop_start)}s elapsed, "
                        f"~{int(remaining)}s hard remaining). "
                        "You have been productive — call "
                        "commit_turn(phase='in_progress') now to checkpoint, "
                        "then continue next wake. Do not start large new work."
                    ),
                })
                # One more soft slice to allow commit_turn
                soft_deadline = min(
                    now_mono + ACTIVITY_EXTEND_S, hard_deadline
                )

            # E14 (复盘 P2): turn 工具轮次疏导上限 —— 单 turn 磨了太多
            # 工具轮次（S7 根源：44+ 轮无界磨，管理通道被屏蔽）时主动疏导：
            # 1) 首达 FORCE_COMMIT_ROUNDS → 注入强制 commit 提示（一次性，
            #    不打断当前轮，给模型主动收口机会）；
            # 2) 过 GRACE 宽限仍未 commit → 优雅收口（保留全部产出，
            #    语义同 budget_exhausted → completion.py 自动 retrigger 续跑）。
            if (
                tool_history
                and round_num >= FORCE_COMMIT_ROUNDS
                and not force_commit_hint_injected
            ):
                force_commit_hint_injected = True
                messages.append({
                    "role": "system",
                    "content": (
                        f"[TURN ROUND CAP] 已达单 turn 工具轮次疏导线 "
                        f"({FORCE_COMMIT_ROUNDS} 轮)。本轮已足够：停止继续"
                        "调用工具，立即调用 commit_turn(phase='in_progress') "
                        "收束当前 slice —— 平台会自动续跑，上下文与产出全部"
                        "保留。不要启动新的重型操作 / 全量测试 / 长轮询。"
                    ),
                })
                log.info(
                    "stream_round_cap_hint",
                    agent_id=agent_id,
                    round=round_num,
                    reason="force_commit_steering",
                )
            if (
                tool_history
                and round_num >= FORCE_COMMIT_ROUNDS + FORCE_COMMIT_GRACE_ROUNDS
            ):
                log.warning(
                    "stream_round_cap_forced_commit",
                    agent_id=agent_id,
                    round=round_num,
                    tools=len(tool_history),
                    force_commit_rounds=FORCE_COMMIT_ROUNDS,
                )
                return self._budget_exhausted_result(
                    text_acc=text_acc,
                    thinking_acc=thinking_acc,
                    tool_history=tool_history,
                    tool_turn_acc=tool_turn_acc,
                    round_num=round_num,
                    last_usage=last_usage,
                    usage_rounds=usage_rounds,
                    note=(
                        f"[TURN ROUND CAP] 单 turn 工具轮次超过疏导线"
                        f"（{FORCE_COMMIT_ROUNDS}+{FORCE_COMMIT_GRACE_ROUNDS}"
                        " 轮仍未被 commit_turn 收口）。全部产出已保留。"
                        "立即调用 commit_turn(phase='in_progress') 收束，"
                        "平台会自动续跑；后续避免单 turn 内无限调用工具。"
                    ),
                    reason="force_commit_rounds",
                )

            # 通知回调：新一轮开始（用于重置流式文本累积器）
            # BUG-7: also fire on round 0 so LLM call counters stay accurate
            if on_delta:
                await self._fire_delta(on_delta, {
                    "type": "round_start",
                    "round": round_num,
                })

            # 溢出才改写前缀。未过 0.8×usable 必须 append-only，否则 DeepSeek
            # 前缀缓存从第一处 replace 整段作废。压力线先 DSH 锯齿（prune /
            # 摘要旧头），0.95 硬裁仍是 API 安全网。
            messages = await self._pressure_compact_if_needed(messages, provider)
            messages = self._trim_context_if_needed(messages, provider)

            # 中轮提醒: 80% 轮次时注入
            messages = self._maybe_inject_mid_round_reminder(
                messages, round_num, rounds_cap
            )

            log.info("tool_loop_round",
                     agent_id=agent_id, round=round_num,
                     msg_count=len(messages))

            # 插话注入：poll steer 队列，把用户插话作为 user 消息塞进本轮
            # 请求的 next-step 窗口（参考 DSH steer）。上一轮的工具
            # assistant+tool_results 已在 messages 尾部，追加 user 顺序合法。
            # 注意：只进本轮内存 messages，不写 tool_turn_acc —— 持久化由
            # phoenix_adapter 的 _save_user_and_ack 以纯文本落库到历史，
            # 避免把 {"from":...} JSON 信封写进对话历史污染下一轮上下文。
            if steer_queue is not None:
                try:
                    while True:
                        _steer_msg = steer_queue.get_nowait()
                        messages.append(
                            {"role": "user", "content": _steer_msg}
                        )
                        await self._fire_delta(on_delta, {
                            "type": "insert_accepted",
                            "content": _steer_msg,
                        })
                        log.info("steer_injected",
                                 agent_id=agent_id, round=round_num)
                except asyncio.QueueEmpty:
                    pass

            # 单轮流式请求（带空响应重试）。预算截止提前
            # TURN_STREAM_CUT_GRACE_S —— 给 merge/记账/返回留出时间，
            # 保证 streamer 总在 agent SAFETY_TIMEOUT 之前返回。
            # TEST_DSH_32 O2（测量补齐）：实测本轮耗时，供 llm_usage.duration_ms。
            _round_t0 = time.monotonic()
            round_result = await self._stream_with_empty_retry(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=messages,
                tools=tools,
                on_delta=on_delta,
                round_num=round_num,
                budget_deadline=hard_deadline - TURN_STREAM_CUT_GRACE_S,
            )
            round_result["round_duration_ms"] = int(
                (time.monotonic() - _round_t0) * 1000
            )

            if round_result["status"] == "error":
                return {
                    "status": "error",
                    "content": text_acc or "",
                    "thinking": thinking_acc,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "usage_rounds": usage_rounds,
                    "error": round_result.get("error"),
                    "error_status": round_result.get("error_status"),
                    "error_headers": round_result.get("error_headers"),
                }

            new_text = round_result["text"] or ""
            new_thinking = round_result["thinking"] or ""
            tool_calls = round_result["tool_calls"]
            finish_reason = round_result["finish_reason"]

            # budget_cut 的流几乎必无 usage 事件（usage 在流末尾 chunk），
            # 无条件覆盖会把上一轮的记账抹成 None（审计 2026-08-08）。
            last_usage = round_result.get("usage") or last_usage

            # Token metering: 归一化本轮 usage 并累加（错误轮无 usage 则跳过）
            # best-effort：畸形 provider 数据不得中断主流程，异常时跳过该轮计费
            # （记账放在截断防御之前 —— 全畸形 continue 路径不漏计）
            try:
                usage = self._normalize_usage(
                    round_result.get("usage"), provider.provider_type
                )
            except Exception:
                log.warning("usage_normalize_skipped",
                            agent_id=agent_id, round=round_num)
                usage = None
            if usage:
                # TEST_DSH_32 O2（测量补齐）：provider 不报 duration——
                # 用本轮实测耗时补齐（此前 294/294 全空）。
                if not usage.get("duration_ms"):
                    usage["duration_ms"] = int(
                        round_result.get("round_duration_ms") or 0
                    )
                # cache_creation 上报 vs 真 0 的区分已由 llm/util.normalize_usage
                # 统一处理（cache_creation_reported 位）——勿在此重复。
                # P2 观测（八轮 TEST_DSH_38）：逐次真实时间戳随行——批量落库
                # 在 run 结束，created_at 若统一盖 end 章，按小时指标与时间
                # 对齐分析全部失真（本轮审计两度被误导）。写路径不变，仅补时刻。
                usage["ts"] = int(time.time() * 1000)
                usage_rounds.append(usage)
                # P1-6：usage 实时推给调用方（取消/中断路径不依赖最终 return ——
                # 用户取消时 result 永不返回，账本会蒸发）。与 append 同域，
                # 保证 sink 只收有效 usage。
                if usage_sink is not None:
                    try:
                        usage_sink(usage)
                    except Exception:
                        log.warning("usage_sink_failed", agent_id=agent_id)

            # ── 截断 tool_calls 防御（TEST_DSH_16 实证）─────────────
            # SSE 提前断流（网关丢 response.completed，finish=None）时
            # arguments 只收到半截 JSON。畸形调用一旦写进 assistant
            # (tool_calls) 历史回传，网关 400 "`arguments` must be valid
            # JSON" 杀死整个 turn。丢弃畸形 + 注入提示让模型重发。
            combined_text_pre = text_acc + new_text
            malformed_names = [
                str(tc.get("name") or "?") for tc in tool_calls
                if not _is_valid_json_arguments(tc.get("arguments"))
            ]
            if malformed_names:
                truncated_rounds += 1
                log.warning(
                    "drop_truncated_tool_calls",
                    agent_id=agent_id,
                    round=round_num,
                    finish=finish_reason,
                    names=malformed_names,
                    consecutive=truncated_rounds,
                )
                if truncated_rounds >= TRUNCATED_TOOL_CALL_ROUNDS_LIMIT:
                    # 链路持续劣化：继续重发只会继续截断。保产出收口。
                    # FIX(text-acc)：只用末轮文本 —— 中间轮文本已通过
                    # per-round assistant 消息保存，拼接会重复（审计）。
                    real_text = self._strip_placeholder(new_text)
                    warning = (
                        "\n\n⚠️ 响应流连续 "
                        f"{truncated_rounds} 轮截断（工具调用参数不完整），"
                        "已丢弃未执行。请稍后重试该调用。"
                    )
                    tool_turn_acc.append(_assistant_with_reasoning(
                        real_text + warning,
                        provider.supports_thinking,
                        new_thinking,
                    ))
                    return {
                        "status": "ok",
                        "content": real_text + warning,
                        "thinking": thinking_acc + new_thinking,
                        "tool_calls": tool_history,
                        "tool_turn_messages": tool_turn_acc,
                        "rounds": round_num + 1,
                        "usage": last_usage,
                        "usage_rounds": usage_rounds,
                    }
                tool_calls = [
                    tc for tc in tool_calls
                    if _is_valid_json_arguments(tc.get("arguments"))
                ]
                messages.append({
                    "role": "system",
                    "content": (
                        "[STREAM TRUNCATED] Your tool call(s) arrived "
                        "incomplete (arguments JSON cut off mid-stream): "
                        f"{', '.join(malformed_names)}. Discarded, NOT "
                        "executed. Re-send the complete call(s)."
                    ),
                })
                if not tool_calls:
                    # 全部畸形：本轮文本留作中间轮，继续循环让模型看到
                    # 提示后立即重发（不走 turn 收口）。
                    if new_text:
                        tool_turn_acc.append(_assistant_with_reasoning(
                            new_text, provider.supports_thinking, new_thinking
                        ))
                    text_acc = self._strip_placeholder(combined_text_pre)
                    thinking_acc = (
                        f"{thinking_acc}\n\n---\n\n{new_thinking}"
                        if thinking_acc and new_thinking
                        else thinking_acc + new_thinking
                    )
                    continue
            else:
                truncated_rounds = 0

            combined_text = text_acc + new_text
            combined_thinking = (
                f"{thinking_acc}\n\n---\n\n{new_thinking}"
                if thinking_acc and new_thinking
                else thinking_acc + new_thinking
            )

            log.info("round_result",
                     agent_id=agent_id, round=round_num,
                     text_len=len(new_text), tool_count=len(tool_calls),
                     finish=finish_reason)

            # 流式被硬预算中途切断：已累积文本保留（usage 已在上方记账），
            # 不完整 tool_calls 已在流内丢弃 —— 直接优雅收口，本轮产出
            # 全部保留，下轮 wake 续跑。先于 finish_reason 分支判断：
            # budget_cut 与 length/content_filter 语义不同，不走截断告警。
            if round_result.get("budget_cut"):
                log.warning(
                    "stream_budget_cut_return",
                    agent_id=agent_id,
                    round=round_num,
                    tools=len(tool_history),
                )
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.stream_budget_exhausted(agent_id)
                except Exception:
                    pass
                return self._budget_exhausted_result(
                    text_acc=combined_text,
                    thinking_acc=combined_thinking,
                    tool_history=tool_history,
                    tool_turn_acc=tool_turn_acc,
                    round_num=round_num + 1,
                    last_usage=last_usage,
                    usage_rounds=usage_rounds,
                    current_reasoning=(
                        new_thinking if provider.supports_thinking else ""
                    ),
                )

            # 处理截断的响应
            if finish_reason in ("length", "content_filter") and tool_calls:
                # 截断的 tool_calls 可能不完整，丢弃
                log.warning("discard_incomplete_tool_calls",
                            agent_id=agent_id, round=round_num,
                            finish=finish_reason)
                real_text = self._strip_placeholder(combined_text)
                warning = f"\n\n⚠️ 响应被截断（{finish_reason}），部分工具调用可能不完整。"
                tool_turn_acc.append(_assistant_with_reasoning(
                    real_text + warning, provider.supports_thinking, new_thinking
                ))
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "usage_rounds": usage_rounds,
                }

            if finish_reason == "length":
                log.warning("response_truncated_length", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被截断（达到最大输出长度），请继续以完成。"
                tool_turn_acc.append(_assistant_with_reasoning(
                    real_text + warning, provider.supports_thinking, new_thinking
                ))
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "usage_rounds": usage_rounds,
                }

            if finish_reason == "content_filter":
                log.warning("content_filtered", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被内容过滤器截断。"
                tool_turn_acc.append(_assistant_with_reasoning(
                    real_text + warning, provider.supports_thinking, new_thinking
                ))
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "usage_rounds": usage_rounds,
                }

            # 有 tool_calls → 执行工具，继续循环
            if tool_calls:
                # 截断到每轮最多 MAX_TOOLS_PER_ROUND 个
                if len(tool_calls) > MAX_TOOLS_PER_ROUND:
                    log.warning("truncate_tool_calls",
                                round=round_num,
                                total=len(tool_calls),
                                capped=MAX_TOOLS_PER_ROUND)
                    tool_calls = tool_calls[:MAX_TOOLS_PER_ROUND]

                # 占位文本: 如果累积文本为空且本轮还没注入过占位符，广播占位（UI 提示）
                # Bug-5 修复: 1) 同一 round 只注入一次 2) 占位不进入 text_acc
                # 避免 UI 上看到 5 个 "好的，开始处理" 的循环。
                if not combined_text and not placeholder_injected:
                    await self._fire_delta(on_delta, {
                        "type": "text_delta",
                        "content": DEFAULT_PLACEHOLDER,
                        "delta_id": f"default_{round_num}",
                        "is_placeholder": True,
                    })
                    # 不要把占位符塞进 combined_text / text_acc，避免下一轮再次注入
                    placeholder_injected = True

                # Doom loop 检测
                doom = self._detect_doom_loop(tool_calls, doom_tracker)
                if doom:
                    limit = doom_loop_limit(doom)
                    if not doom_warning_given:
                        # 第一次触发: 拦截重复调用，注入反馈给 LLM 纠正机会
                        log.warning("doom_loop_warned",
                                    agent_id=agent_id, tool=doom, count=limit)
                        try:
                            from hiveweave.services.telemetry import telemetry
                            telemetry.doom_loop(agent_id, doom, stage="warned")
                        except Exception:
                            pass
                        doom_warning_given = True
                        # 构造 assistant 消息（含 tool_calls）让 LLM 看到自己的请求
                        doom_assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": new_text if new_text else None,
                            "tool_calls": [
                                {
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": tc["arguments"],
                                    },
                                }
                                for tc in tool_calls
                            ],
                        }
                        if provider.supports_thinking and new_thinking:
                            doom_assistant_msg["reasoning_content"] = new_thinking
                        tool_turn_acc.append(doom_assistant_msg)
                        for tc in tool_calls:
                            tool_history.append({
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            })
                        # 返回拦截结果（不执行真正的工具）
                        tool_results = [
                            {
                                "role": "tool",
                                "content": (
                                    f"[DOOM LOOP 拦截] 你已连续 {limit} 次用完全相同的参数调用 '{doom}'。"
                                    f"这可能是死循环。请换用其他工具、调整参数，或先用文字说明"
                                    f"你为何需要重复执行相同命令。"
                                ),
                                "tool_call_id": tc["id"],
                            }
                            for tc in tool_calls
                        ]
                        messages = messages + [doom_assistant_msg] + tool_results
                        tool_turn_acc.extend(tool_results)
                        # 重置 tracker，给 LLM 一轮纠正机会
                        doom_tracker = {"last_key": None, "count": 0}
                        # 累积文本和 thinking
                        text_acc = self._strip_placeholder(combined_text)
                        thinking_acc = combined_thinking
                        continue
                    else:
                        # 第二次触发: 真正中断
                        log.warning("doom_loop_detected",
                                    agent_id=agent_id, tool=doom)
                        try:
                            from hiveweave.services.telemetry import telemetry
                            telemetry.doom_loop(agent_id, doom, stage="detected")
                        except Exception:
                            pass
                        return {
                            "status": "error",
                            "content": text_acc or "",
                            "thinking": thinking_acc,
                            "tool_calls": tool_history,
                            "tool_turn_messages": tool_turn_acc,
                            "rounds": round_num + 1,
                            "usage": last_usage,
                            "usage_rounds": usage_rounds,
                            "error": f"Doom loop detected: tool '{doom}' called "
                                     f"{limit}+ times with same args (after warning)",
                        }

                # 工具批预算闸门（结构性修复 2026-08-07）：剩余预算不足以
                # 执行工具批时（一批最长 question 200s / bash 120s；
                # spawn_subagent 已改为 off-turn 立即返回），丢弃本批
                # tool_calls 优雅收口。闸口必须在
                # assistant(tool_calls) 落账【之前】——否则持久化的
                # assistant(tool_calls) 缺对应 tool 回执，下次请求 400。
                # 丢弃无副作用：未执行的工具由下次唤醒重新发起。
                tool_budget_s = (
                    hard_deadline - TOOL_BUDGET_GRACE_S - time.monotonic()
                )
                if tool_budget_s < MIN_TOOL_BUDGET_S:
                    log.warning(
                        "stream_budget_tool_gate",
                        agent_id=agent_id,
                        round=round_num,
                        tool_budget_s=round(tool_budget_s, 1),
                        discarded_tools=len(tool_calls),
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        telemetry.stream_budget_exhausted(agent_id)
                    except Exception:
                        pass
                    return self._budget_exhausted_result(
                        text_acc=combined_text,
                        thinking_acc=combined_thinking,
                        tool_history=tool_history,
                        tool_turn_acc=tool_turn_acc,
                        round_num=round_num + 1,
                        last_usage=last_usage,
                        usage_rounds=usage_rounds,
                        current_reasoning=(
                            new_thinking if provider.supports_thinking else ""
                        ),
                    )

                # 构建 assistant 消息（含 tool_calls）
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": new_text if new_text else None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                # reasoning 模型: 附加 reasoning_content 保持思维链
                if provider.supports_thinking and new_thinking:
                    assistant_msg["reasoning_content"] = new_thinking

                tool_turn_acc.append(assistant_msg)

                # 累积 tool_history
                for tc in tool_calls:
                    tool_history.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    })

                # 执行工具
                end_turn = False
                error_ids: set[str] = set()
                blocked_ids: set[str] = set()
                duplicate_ids: set[str] = set()
                runner_failed = False
                if on_tool_call is None:
                    log.error("no_tool_executor", agent_id=agent_id)
                    tool_results = [
                        {"role": "tool", "content": "[No tool executor]",
                         "tool_call_id": tc["id"]}
                        for tc in tool_calls
                    ]
                    error_ids = {tc["id"] for tc in tool_calls}
                    # 执行器缺失 = runner 故障，不是工具体报错、更不是模型空转。
                    runner_failed = True
                else:
                    tool_results, error_ids, blocked_ids, duplicate_ids, end_turn = (
                        await self._execute_tools(
                            agent_id=agent_id,
                            tool_calls=tool_calls,
                            on_tool_call=on_tool_call,
                            on_delta=on_delta,
                            poll_turn_counts=poll_turn_counts,
                            budget_s=tool_budget_s,
                        )
                    )
                    # duplicate 信号：工具返回 duplicate=True 表示本次调用无新
                    # 效果（如 commit_turn 同参已接受过）。强制 +1 计数，让下一轮
                    # _detect_doom_loop 更快触顶。error_ids 保留用于日志/观测。
                    if duplicate_ids:
                        doom_tracker["count"] = (
                            doom_tracker.get("count", 0) + 1
                        )

                # ADR-001 补丁：失败调用在 tool_history 条目上落 ok=False
                # （条目在执行前已 append，失败也留账）。完成闸窄集
                # _task_ids_gate_resolved_this_turn 只认未失败调用——
                # 否则"submit 被证据门拒收 → commit done_slice"以失败
                # 调用解除义务，逃逸口换壳复活。无标记 = 成功（兼容）。
                if error_ids:
                    for _e in tool_history:
                        if _e.get("id") in error_ids:
                            _e["ok"] = False

                # 追加 assistant + tool_results 到 messages
                messages = messages + [assistant_msg] + tool_results
                tool_turn_acc.extend(tool_results)

                # BUG-3: commit_turn accepted → hard-stop tool loop (no next LLM round)
                if end_turn:
                    log.info(
                        "commit_turn_end_turn",
                        agent_id=agent_id,
                        round=round_num,
                    )
                    final_text = self._strip_placeholder(combined_text)
                    if not final_text:
                        final_text = "(turn committed)"
                    return {
                        "status": "ok",
                        "content": final_text,
                        "thinking": combined_thinking,
                        "tool_calls": tool_history,
                        "tool_turn_messages": tool_turn_acc,
                        "rounds": round_num + 1,
                        "usage": last_usage,
                        "usage_rounds": usage_rounds,
                        "end_turn": True,
                    }

                # DESIGN-2: stall counter — no mutating progress → force outer
                # loop exit. Pure-readonly polling uses a higher limit.
                # 归因与计数分离（DSH_33）：classify_stall_round 先摘出
                # 「工具/runner 没跑成」的轮次，再判护栏拒绝 / 只读 / 空转 ——
                # 顺序不可换（DSH: runner failure outranks denial）。
                # H3: 护栏拒绝（blocked）≠ 模型空转 —— 走独立
                # blocked_stall_count；工具执行失败同理走 tool_fail_stall_count。
                round_reason = classify_stall_round(
                    tool_calls,
                    error_ids=error_ids,
                    blocked_ids=blocked_ids,
                    duplicate_ids=duplicate_ids,
                    seen_readonly_fingerprints=seen_readonly_fingerprints,
                )
                # P0-1：判定之后再并入本轮成功只读指纹（判定期历史不含本轮，
                # 否则新指纹会被误判为"已见过"）。readonly_fingerprint 内部
                # 自带容错（json 失败回退 repr），不再包宽 except。
                for tc in tool_calls:
                    tid = tc.get("id") or ""
                    if tid in (error_ids or set()) or tid in (duplicate_ids or set()):
                        continue
                    fp = readonly_fingerprint(tc)
                    if fp is not None:
                        seen_readonly_fingerprints.add(fp)
                if round_reason == STALL_REASON_TOOL_FAILED and runner_failed:
                    round_reason = STALL_REASON_RUNNER_FAILED
                if round_reason is None:
                    stall_count = 0
                    readonly_stall_count = 0
                    blocked_stall_count = 0
                    tool_fail_stall_count = 0
                    last_stall_reason = None
                    # TEST21 M4: activity renews soft budget within hard cap
                    soft_deadline = min(
                        time.monotonic() + ACTIVITY_EXTEND_S, hard_deadline
                    )
                elif round_reason == STALL_REASON_READONLY:
                    readonly_stall_count += 1
                    stall_count = 0
                    blocked_stall_count = 0
                    tool_fail_stall_count = 0
                    last_stall_reason = round_reason
                elif round_reason == STALL_REASON_BLOCKED:
                    # 全部失败都是平台护栏拒绝（blocked）→ 独立计数，
                    # 不给模型判空转（H3）。不清零普通 stall_count：
                    # [error, blocked] 交替轮必须仍能累计到 STALL BREAK
                    # （2026-08-13 审计：清零会让交替序列永不触顶）。
                    blocked_stall_count += 1
                    last_stall_reason = round_reason
                elif round_reason in (
                    STALL_REASON_TOOL_FAILED, STALL_REASON_RUNNER_FAILED
                ):
                    # 工具自身执行失败（非护栏拒绝、非模型空转）→ 独立归因位。
                    # P0-1（R3）：只对「同源失败」快速收口 —— 同工具同参同墙
                    # 才算原地位；换参/换工具失败 = 试错在收敛，不得 2 轮掐死
                    # （R2/R3 实证 10+4 次被掐均是被试错判死）。同时累计
                    # stall_count 保持总限兜底与交替序列可触顶。
                    fail_sig = fail_signature_for_round(
                        tool_calls,
                        error_ids=error_ids,
                        duplicate_ids=duplicate_ids,
                    )
                    if fail_sig is not None and fail_sig == last_fail_signature:
                        tool_fail_stall_count += 1  # 同墙原地：连续累计
                    else:
                        # 方向在变/首轮建立：重置为「本次失败」（=1）——
                        # 收敛判据下连续 2 轮同源才快速收口；发散轮本身不
                        # 涨到这个计数（只贡献普通 stall_count 总限）。
                        tool_fail_stall_count = 1
                    last_fail_signature = fail_sig
                    stall_count += 1
                    readonly_stall_count = 0
                    blocked_stall_count = 0
                    last_stall_reason = round_reason
                else:
                    stall_count += 1
                    readonly_stall_count = 0
                    blocked_stall_count = 0
                    tool_fail_stall_count = 0
                    last_stall_reason = STALL_REASON_NO_PROGRESS
                stalled = (
                    stall_count >= TOOL_LOOP_STALL_LIMIT
                    or readonly_stall_count >= TOOL_LOOP_READONLY_STALL_LIMIT
                    or blocked_stall_count >= BLOCKED_STALL_LIMIT
                    or tool_fail_stall_count >= TOOL_FAIL_STALL_LIMIT
                )
                if stalled:
                    # 收口归因：工具失败优先（DSH 顺序），其次护栏，
                    # 再退回本轮观测到的原因。
                    if tool_fail_stall_count >= TOOL_FAIL_STALL_LIMIT:
                        stall_reason = (
                            last_stall_reason
                            if last_stall_reason == STALL_REASON_RUNNER_FAILED
                            else STALL_REASON_TOOL_FAILED
                        )
                    elif (
                        blocked_stall_count >= BLOCKED_STALL_LIMIT
                        and stall_count < TOOL_LOOP_STALL_LIMIT
                        and readonly_stall_count < TOOL_LOOP_READONLY_STALL_LIMIT
                    ):
                        stall_reason = STALL_REASON_BLOCKED
                    elif readonly_stall_count >= TOOL_LOOP_READONLY_STALL_LIMIT:
                        stall_reason = STALL_REASON_READONLY
                    else:
                        stall_reason = last_stall_reason or STALL_REASON_NO_PROGRESS
                    log.warning(
                        "tool_loop_stall",
                        agent_id=agent_id,
                        round=round_num,
                        stall_count=stall_count,
                        readonly_stall_count=readonly_stall_count,
                        blocked_stall_count=blocked_stall_count,
                        tool_fail_stall_count=tool_fail_stall_count,
                        stall_reason=stall_reason,
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        telemetry.tool_loop_stall(
                            agent_id,
                            stall_count=max(
                                stall_count,
                                readonly_stall_count,
                                blocked_stall_count,
                                tool_fail_stall_count,
                            ),
                        )
                    except Exception:
                        pass
                    if stall_reason in (
                        STALL_REASON_TOOL_FAILED, STALL_REASON_RUNNER_FAILED
                    ):
                        # 计数如实：触发闸口可能是普通 stall_count（[无进展,
                        # 工具失败] 混合序列），此时补报总轮数，避免只写工具
                        # 失败连续数让模型误判收口时机。
                        scope = (
                            f"（最近 {stall_count} 轮均无进展）"
                            if stall_count > tool_fail_stall_count
                            else ""
                        )
                        break_text = (
                            f"[STALL BREAK] 连续 {tool_fail_stall_count} 轮工具"
                            f"调用失败（非模型空转）{scope}—— 工具自身返回了执行"
                            "失败，不是你没动作。先读失败回执的具体报错：检查工具"
                            "名与参数用法是否正确、前置条件是否满足；换一种方式或"
                            "换工具达成同一目标，或用 commit_turn 提交已完成的"
                            "部分并说明卡点。不要原样重发同一个失败调用。"
                        )
                    elif stall_reason == STALL_REASON_BLOCKED:
                        break_text = (
                            f"[STALL BREAK] {blocked_stall_count} consecutive "
                            "tool-loop rounds were refused by platform guards "
                            "(permission / sandbox / security rules) — not "
                            "model errors. Stop repeating blocked calls: "
                            "change approach, commit partial work with "
                            "commit_turn, or ask your superior for the missing "
                            "permission."
                        )
                    else:
                        break_text = (
                            f"[STALL BREAK] {max(stall_count, readonly_stall_count)} "
                            "consecutive tool-loop "
                            "rounds repeated identical readonly calls (same tool + "
                            "same args) with no mutating progress. Stop polling the "
                            "same state: call commit_turn now (waiting/blocked/"
                            "done_slice) or change approach — do not repeat the "
                            "same readonly call."
                        )
                    messages.append({
                        "role": "system",
                        "content": break_text,
                    })
                    summary = await self._make_max_rounds_summary(
                        agent_id, provider, messages, on_delta,
                        reason="stall_break",
                        budget_deadline=hard_deadline,
                        stall_reason=stall_reason,
                    )
                    final_text = self._strip_placeholder(summary)
                    if not final_text:
                        if stall_reason in (
                            STALL_REASON_TOOL_FAILED, STALL_REASON_RUNNER_FAILED
                        ):
                            final_text = (
                                f"[STALL BREAK] 连续 {tool_fail_stall_count} 轮"
                                "工具调用失败（非模型空转）—— 回合结束。"
                            )
                        else:
                            final_text = (
                                "[STALL BREAK] No progress for "
                                f"{max(stall_count, readonly_stall_count, blocked_stall_count)} rounds — "
                                "turn ended."
                            )
                    # 本轮 assistant_msg（含 reasoning_content）已在上方
                    # append（line 847），此处只追加 summary 文本，不重复附 thinking。
                    tool_turn_acc.append(
                        {"role": "assistant", "content": final_text}
                    )
                    return {
                        "status": "ok",
                        "content": final_text,
                        "thinking": combined_thinking,
                        "tool_calls": tool_history,
                        "tool_turn_messages": tool_turn_acc,
                        "rounds": round_num + 1,
                        "usage": last_usage,
                        "usage_rounds": usage_rounds,
                        "stall_break": True,
                        "stall_reason": stall_reason,
                    }

                # F8（平台修复计划 2026-08-30）：advisory 重复工具失败提醒 —
                # 硬门（doom/stall）之前先给软提醒（3/5/8，never block）。
                # 计数按 (agent, tool, canonical args)；提醒附上次失败归因
                # （来自本轮工具回执的 F4 事实位），注入 messages 让下一轮
                # LLM 请求看到。per-agent 独立，不阻断本轮。
                if error_ids:
                    _advisory_attribution = ""
                    try:
                        # F4 事实位 → 一句话归因（先 runner 后 command）
                        _rf = _round_fact_flags(tool_results, error_ids)
                        if _rf.get("runner_failed"):
                            _advisory_attribution = (
                                "runner_failed: 命令未执行（执行器/方言/权限/审批）"
                            )
                        elif _rf.get("command_failed"):
                            _advisory_attribution = (
                                "command_failed: 命令执行了但失败（业务/测试未过）"
                            )
                        elif _rf.get("blocked"):
                            _advisory_attribution = (
                                "blocked: 平台护栏拒绝（权限/沙箱/安全）"
                            )
                    except Exception:
                        _advisory_attribution = ""
                    _adv_hits = []
                    for _tc in tool_calls:
                        _tid = _tc.get("id") or ""
                        if _tid not in (error_ids or set()):
                            continue
                        _at = advisory_guard.record_failure(
                            agent_id,
                            _tc.get("name") or "",
                            _tc.get("arguments"),
                            _advisory_attribution,
                        )
                        if _at:
                            _adv_hits.append(_at)
                            log.info(
                                "advisory_repeat_tool_reminder",
                                agent_id=agent_id,
                                tool=_tc.get("name") or "",
                                round=round_num,
                            )
                    if _adv_hits:
                        messages.append(
                            {"role": "system", "content": " ".join(_adv_hits)}
                        )
                        tool_turn_acc.append(
                            {"role": "assistant", "content": " ".join(_adv_hits)}
                        )

                # 连续无文字轮次检测
                if not new_text:
                    no_text_rounds += 1
                    if no_text_rounds >= NO_TEXT_ROUNDS_THRESHOLD:
                        no_text_hint_count += 1
                        if no_text_hint_count > NO_TEXT_HINT_MAX:
                            # 第 2 次注入后仍然只调工具不说话 → 强制结束 tool loop
                            log.warning("no_text_hint_exhausted",
                                        agent_id=agent_id,
                                        round=round_num,
                                        hint_count=no_text_hint_count)
                            summary = await self._make_max_rounds_summary(
                                agent_id, provider, messages, on_delta,
                                reason="no_text",
                                budget_deadline=hard_deadline,
                            )
                            # FIX(text-acc): 同 max_rounds 路径，只用 summary
                            final_text = self._strip_placeholder(summary)
                            # 本轮 assistant_msg 已含 reasoning_content（line 847），
                            # 此处不重复附 thinking。
                            tool_turn_acc.append(
                                {"role": "assistant", "content": final_text}
                            )
                            return {
                                "status": "ok",
                                "content": final_text,
                                "thinking": thinking_acc,
                                "tool_calls": tool_history,
                                "tool_turn_messages": tool_turn_acc,
                                "rounds": round_num + 1,
                                "usage": last_usage,
                                "usage_rounds": usage_rounds,
                            }
                        log.info("inject_no_text_hint", round=round_num,
                                 no_text_rounds=no_text_rounds,
                                 hint_count=no_text_hint_count)
                        messages.append({
                            "role": "system",
                            "content": (
                                f"你已经连续{no_text_rounds}轮只调用工具没有输出文字。"
                                "从现在开始，你必须在调用工具之前先用一段文字说明"
                                "你正在做什么、分析到了什么。不要只调用工具不说话。"
                            ),
                        })
                        no_text_rounds = 0
                else:
                    no_text_rounds = 0

                # Bug-5 修复: 累积前剥除可能混入的占位符（防御性，多余但安全）
                text_acc = self._strip_placeholder(combined_text)
                thinking_acc = combined_thinking
                continue

            # 无 tool_calls — 检查是否有真实文本
            has_real_text = bool(combined_text) and combined_text != DEFAULT_PLACEHOLDER
            if not has_real_text:
                log.warning("empty_response_final", round=round_num)
                return {
                    "status": "empty",
                    "content": "",
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "usage_rounds": usage_rounds,
                }

            # 有真实文本 — 剥离占位符，结束
            # FIX(text-acc): 只用最终轮的文本 (new_text)，不拼接中间轮。
            # 中间轮文本已通过 line 777 的 per-round assistant 消息保存在
            # tool_turn_acc 中，无需在最终消息中重复。之前的 combined_text
            # 会拼接所有轮的文本，导致同一分析语句在最终消息中重复 3-5 次，
            # 进而污染 conversation_turns 和下一轮的 LLM 上下文。
            final_text = self._strip_placeholder(new_text)
            # reasoning 模型末轮 thinking 一并写入 tool_turn_messages →
            # build_display_segments 原位保留 thinking 块（DSH 整轮视图：
            # 末轮思考在 done reload 后仍可见）。
            final_msg = _assistant_with_reasoning(
                final_text, provider.supports_thinking, new_thinking
            )
            tool_turn_acc.append(final_msg)

            log.info("stream_complete",
                     agent_id=agent_id,
                     text_len=len(final_text),
                     rounds=round_num + 1)

            return {
                "status": "ok",
                "content": final_text,
                "thinking": combined_thinking,
                "tool_calls": tool_history,
                "tool_turn_messages": tool_turn_acc,
                "rounds": round_num + 1,
                "usage": last_usage,
                "usage_rounds": usage_rounds,
            }

        # 达到最大轮次 — 做一次无工具的总结调用
        log.warning("max_rounds_reached",
                    agent_id=agent_id,
                    max_rounds=rounds_cap)
        summary = await self._make_max_rounds_summary(
            agent_id, provider, messages, on_delta,
            reason="max_rounds",
            budget_deadline=hard_deadline,
        )
        # FIX(text-acc): 只用 summary，不拼接 text_acc。
        # summary 是专门的 LLM 调用，已概括全部进展。拼接 text_acc 会引入
        # 所有中间轮的重复文本（同正常退出路径的修复逻辑）。
        final_text = self._strip_placeholder(summary)
        final_msg = {"role": "assistant", "content": final_text}
        tool_turn_acc.append(final_msg)

        return {
            "status": "ok",
            "content": final_text,
            "thinking": thinking_acc,
            "tool_calls": tool_history,
            "tool_turn_messages": tool_turn_acc,
            "rounds": rounds_cap,
            "usage": last_usage,
            "usage_rounds": usage_rounds,
        }

