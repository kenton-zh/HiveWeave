"""Error / timeout / escalation recovery handlers.

Extracted from agent.py — behavior-preserving mechanical split (P1b).
Module-level functions take ``agent`` as first arg; Agent methods are thin wrappers.

MUST NOT top-level import hiveweave.agents.trigger — lazy import inside functions only.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.agents.types import AgentState
from hiveweave.agents.constants import (
    EMPTY_RETRY_DELAYS,
    MAX_EMPTY_RETRIES,
    TIMEOUT_RESUME_COOLDOWN_S,
    ERROR_RESUME_COOLDOWN_S,
    RATE_LIMIT_RESUME_COOLDOWN_S,
    RATE_LIMIT_SOFT_MAX_S,
    RATE_LIMIT_BACKOFF_STEPS_S,
    RATE_LIMIT_SOFT_STREAK_ESCALATE,
)
from hiveweave.agents.helpers.rate_limit import (
    broadcast_project_capacity_pause,
    broadcast_project_rate_limit,
    is_account_rate_limit,
    is_rate_limit_error,
)

log = structlog.get_logger(__name__)

# Structured wake hint (not language-scanned). Injected when history ends
# with a failed-turn assistant marker so "继续" / "retry" still see the
# pending user instruction that never made it through a successful append.
FAILED_TURN_MARKER = "[PLATFORM_TURN_FAILED]"
FAILED_TURN_NEXT_WAKE_HINT = (
    "[PLATFORM] Previous LLM turn failed before a reply. "
    "The user's last instruction is in history and still pending. "
    "Continue that work unless this wake is a clearly new, different request."
)

# ── E5 断流降级记账（复盘致命链二：断流不自证，直接诱发 waiver 收口）──
#
# agent 是长驻服务，进程内 registry 跨 turn 存活；被断流（SSL EOF / stream
# idle / tool-loop stall / safety_timeout 等）打断时置位，成功完成一轮
# （complete_run）后清除。工具层（waive / VERIFY submit）在「降级 ∧ 名下有
# 未闭环 VERIFY」时拒绝 waiver 型收口，强制续跑或升级 coordinator。
# 说明：这是判段出处（P2）前的兜底补丁——registry 为进程内态，后端重启
# 即清零；不建完整「判断出处」系统，只堵最危险的就地收口路径。
_DEGRADED_AGENTS: dict[str, int] = {}  # agent_id -> set_at_ms


def mark_degraded(agent_id: str) -> None:
    """断流/stall 打断时置位降级标志。"""
    if agent_id:
        _DEGRADED_AGENTS[agent_id] = int(time.time() * 1000)


def clear_degraded(agent_id: str) -> None:
    """成功续跑一轮（run 完成）后清除降级标志。"""
    if agent_id:
        _DEGRADED_AGENTS.pop(agent_id, None)


def is_degraded(agent_id: str) -> bool:
    """降级标志是否仍置位（供工具层收口入口检查）。"""
    return agent_id in _DEGRADED_AGENTS


def _is_interruption_break(error: Exception) -> bool:
    """是否断流类打断（SSL EOF / stream idle / tool-loop stall 等）。

    这类错误意味着 turn 被打断而非可恢复的瞬时抖动，被打断后 agent 面临
    「续跑重验 vs 就地收口」的选择——正是它们需要一个降级标记来拦截
    waiver 捷径。普通限流 / 业务错误不计入。
    """
    msg = str(error).lower()
    # 类型级判定优先（比消息子串稳——避免 "SSL certificate" 等配置类文案
    # 误标降级）：httpx 流层 / ssl / 读超时 / asyncio 超时（stream idle）。
    try:
        import asyncio as _aio
        import http.client as _http

        import httpx
        import ssl

        if isinstance(
            error,
            (
                ssl.SSLError,
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.ConnectError,
                httpx.PoolTimeout,
                _aio.TimeoutError,
                _http.IncompleteRead,
            ),
        ):
            return True
    except Exception:
        pass
    # 消息子串兜底（tool-loop stall / Turn ended early 等服务层文案无专用类型）。
    markers = (
        "socket connection was closed",
        "serverdisconnected",
        "server disconnected",
        "stream idle",
        "write timeout",
        "tool-loop stalled",
        "tool loop stalled",
        "turn ended early",
        "valueerror: stream",
        "ssl eof",
        "read eof",
    )
    return any(m in msg for m in markers)


def history_ends_with_failed_turn(history: list | None) -> bool:
    """True when the last assistant turn is a failed-LLM marker."""
    for msg in reversed(history or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role == "assistant":
            content = str(msg.get("content") or "").lstrip()
            return content.startswith(FAILED_TURN_MARKER)
        if role == "user":
            return False
    return False


def _same_failed_turn_already_persisted(
    history: list | None, user_msg: str
) -> bool:
    if not user_msg or not history_ends_with_failed_turn(history):
        return False
    for msg in reversed(history or []):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") == "user":
            return str(msg.get("content") or "") == user_msg
    return False


def attach_failed_turn_hint(history: list | None, user_content: str) -> str:
    """Append the pending-instruction hint when the last turn failed."""
    if not history_ends_with_failed_turn(history):
        return user_content
    if FAILED_TURN_NEXT_WAKE_HINT in (user_content or ""):
        return user_content
    return f"{user_content}\n\n{FAILED_TURN_NEXT_WAKE_HINT}"


def _job_user_message(agent: Any) -> str:
    job = getattr(agent, "current_job", None) or {}
    return str(job.get("message") or "").strip()


async def persist_failed_turn(agent: Any, assistant_content: str) -> None:
    """Write this wake's user message + error marker into conversation store.

    Success path persists in handle_completion via append_turn. Error/timeout
    used to skip that, so the next human nudge (e.g. 继续) only saw ledger
    state and dropped the original instruction.
    """
    user_msg = _job_user_message(agent)
    if not user_msg:
        return
    conv = getattr(agent, "_conversation", None)
    if conv is None or not hasattr(conv, "append_turn"):
        return
    text = str(assistant_content or "").strip() or "[ERROR]"
    if not text.lstrip().startswith(FAILED_TURN_MARKER):
        text = f"{FAILED_TURN_MARKER} {text}"
    try:
        try:
            existing = await conv.get_history(agent.id, agent.project_id)
        except Exception:
            existing = []
        if _same_failed_turn_already_persisted(existing, user_msg):
            return
        from hiveweave.services.vision import messages_without_images

        await conv.append_turn(
            agent.id,
            agent.project_id,
            messages_without_images(
                [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": text},
                ]
            ),
        )
    except Exception as e:
        log.warning(
            "persist_failed_turn_failed",
            agent_id=getattr(agent, "id", None),
            error=str(e),
        )
        # Fail-open: next _build_messages still sees the instruction once.
        prev = getattr(agent, "_pending_resume_hint", None)
        fallback = (
            f"{FAILED_TURN_NEXT_WAKE_HINT}\n\n"
            f"[PENDING USER INSTRUCTION]\n{user_msg}"
        )
        agent._pending_resume_hint = (
            f"{prev}\n\n{fallback}" if prev else fallback
        )


async def persist_partial_turn(agent: Any, partial_result: dict) -> bool:
    """断流时持久化已产出的中间轮消息。

    参考 DSH 事件溯源：模型请求失败时不丢弃已 durable 的产出 ——
    streamer 的 error result 携带 ``tool_turn_messages``（本 turn 已
    完成的多轮 assistant+tool 对），断流前若直接丢弃，resume 时模型
    读不到这些中间产出，只能重想。此处把它们连同 user 指令一并写入
    conversation store，下个 wake 重建历史时完整可见。

    与 ``persist_failed_turn`` 的关系：后者只写 user + [ERROR] 标记
    （保留指令但不保留中间产出）；本函数是它的替代 —— 有中间产出时
    用更完整的一版，无中间产出时返回 False，由调用方回退。

    Returns:
        True 表示成功持久化中间产出；False 表示无可持久化内容或已由
        persist_failed_turn 写过（调用方应回退/跳过）。
    """
    tool_msgs = partial_result.get("tool_turn_messages") or []
    if not tool_msgs:
        return False
    conv = getattr(agent, "_conversation", None)
    if conv is None or not hasattr(conv, "append_turn"):
        return False
    user_msg = _job_user_message(agent)
    if not user_msg:
        return False
    # 幂等：若历史末尾已是失败 turn（persist_failed_turn 已写过
    # user + [ERROR]），跳过，避免重复追加同一条 user 指令。
    try:
        existing = await conv.get_history(agent.id, agent.project_id)
    except Exception:
        existing = []
    if _same_failed_turn_already_persisted(existing, user_msg):
        return False
    # 只保留可回传的角色消息（assistant/tool/user），丢弃无关字段。
    partial = [
        m for m in tool_msgs
        if isinstance(m, dict) and m.get("role") in ("assistant", "tool", "user")
    ]
    if not partial:
        return False
    # 断流标记统一用 FAILED_TURN_MARKER 前缀，保证 history_ends_with_
    # failed_turn / attach_failed_turn_hint / 幂等检查都能识别。
    # 注意：不再重嵌 streamer 的 content —— error result 的 content 是
    # 已完成轮累积文本（已逐轮以 assistant 消息进入 tool_turn_messages），
    # 重嵌会整段重复（tool_loop FIX(text-acc) 同源问题）。
    partial.append({
        "role": "assistant",
        "content": (
            f"{FAILED_TURN_MARKER} [TURN INTERRUPTED] 本回合被断流打断，"
            "以上为已完成的中间产出；剩余工作请在下一轮继续。"
        ),
    })
    from hiveweave.services.vision import messages_without_images

    await conv.append_turn(
        agent.id,
        agent.project_id,
        messages_without_images(
            [{"role": "user", "content": user_msg}] + partial
        ),
    )
    return True


async def handle_empty_response(
    agent: Any,
    result: dict,
    current_messages: list[dict],
) -> bool:
    """空响应处理。

    对齐 Elixir agent.ex:587 handle_info({ref, {:empty, ...}})。

    - retry_count + 1
    - 如果 > MAX_EMPTY_RETRIES: 升级上级，返回 False
    - 否则: 退避 [5s, 15s, 45s]，返回 True（重试）

    Returns:
        True = 重试, False = 已升级上级（退出循环）
    """
    agent.empty_retry_count += 1
    retry_count = agent.empty_retry_count

    log.warning(
        "empty_response",
        agent_id=agent.id,
        retry_count=retry_count,
        tool_calls=len(result.get("tool_calls", [])),
    )

    if retry_count > MAX_EMPTY_RETRIES:
        # 升级上级
        await agent._escalate_empty_response()
        return False

    # 退避
    delay_idx = min(retry_count - 1, len(EMPTY_RETRY_DELAYS) - 1)
    delay_s = EMPTY_RETRY_DELAYS[delay_idx] / 1000.0

    log.info(
        "empty_retry_backoff",
        agent_id=agent.id,
        retry_count=retry_count,
        delay_s=delay_s,
    )

    await asyncio.sleep(delay_s)
    return True

async def escalate_empty_response(agent: Any) -> None:
    """空响应超限，升级到上级。

    对齐 Elixir agent.ex:610 escalate_empty/1。

    流程:
    1. 清理 streaming placeholder（防止僵尸消息）
    2. 标记 pending inbox 已读（避免重复触发）
    3. 通知上级 agent
    4. 状态 → idle
    """
    log.warning(
        "empty_escalate",
        agent_id=agent.id,
        retry_count=agent.empty_retry_count,
        msg="escalating to superior after max empty retries",
    )

    # BUG-038: 清理 streaming placeholder — 其他退出路径都清理了，
    # 但 _escalate_empty_response 曾遗漏，导致 is_streaming=1 僵尸消息
    try:
        await agent._finalize_streaming_turn(
            content=(
                getattr(agent, "_streaming_text_acc", "")
                or "[空响应超限，已升级上级处理]"
            ),
        )
    except Exception as e:
        log.warning("empty_escalate_streaming_cleanup_failed",
                    agent_id=agent.id, error=str(e))
    agent._streaming_text_acc = ""
    try:
        await persist_failed_turn(agent, "[空响应超限，已升级上级处理]")
    except Exception as e:
        log.warning("persist_failed_turn_on_empty_failed", error=str(e))

    # Selective ACK — spare ask/escalation/review-critical (same as give-up).
    if agent.pending_inbox_msg_ids:
        await agent._ack_inbox_on_give_up(list(agent.pending_inbox_msg_ids))
        agent.pending_inbox_msg_ids = None

    # 通知上级
    superior = await agent._org.get_superior(agent.id)
    if superior:
        superior_id = superior["id"]
        agent_name = agent.config.get("name", agent.id)
        await agent._inbox.send_message(
            from_agent_id=agent.id,
            to_agent_id=superior_id,
            message=(
                f"[ESCALATION] Subordinate {agent_name} has produced "
                f"empty responses {agent.empty_retry_count} times. "
                f"They may be stuck. Please check on them."
            ),
            message_type="escalation",
            priority="urgent",
        )
        # 触发上级
        from hiveweave.agents.trigger import trigger_coordinator

        await trigger_coordinator(superior_id)
    else:
        log.error(
            "empty_escalate_no_superior",
            agent_id=agent.id,
            msg="no superior to escalate to",
        )

    agent._cancel_safety_timer()
    agent._reset_to_idle()

async def handle_error(
    agent: Any, error: Exception, partial_result: dict | None = None
) -> None:
    """错误处理。

    对齐 Elixir agent.ex:644 handle_info({:DOWN, ref, :process, ...})。

    BUG-032 修复: 先广播 error 事件再 reset_to_idle。之前顺序导致
    前端先收到 status→idle 再收到 error，错误信息可能因 streamDraft
    已被清理而无法展示。参考 OpenCode SSE 错误模式：错误事件作为终止
    信号先于状态变更到达。
    """
    error_msg = str(error)
    error_type = type(error).__name__

    # E5: 断流类打断（SSL EOF / stream idle / tool-loop stalled …）置位降级
    # 标志——被打断后「续跑重验 vs 就地收口」的选择需要一个标记来拦截
    # waiver 捷径（复盘：终验被三连打断后选择 waiver 提交收口）。
    # 审计修正：置位必须在 handle_error 主通道（SSL EOF 正是走这里），
    # _is_interruption_break 才是断流判定唯一入口。
    if _is_interruption_break(error):
        mark_degraded(agent.id)

    # ── Durable Run Ledger: mark run errored ──
    _run_id = getattr(agent, "_current_run_id", None)
    if _run_id:
        try:
            await agent._run_ledger.error_run(
                agent_id=agent.id,
                run_id=_run_id,
                error_reason=f"{error_type}: {error_msg}",
            )
        except Exception as e:
            log.debug("run_ledger.error_run_failed", error=str(e))

    log.error(
        "llm_error",
        agent_id=agent.id,
        error=error_msg,
        error_type=error_type,
    )

    # 写 work_log — 确保错误在监控面板可见
    try:
        await agent._work_log.write_work_log(
            agent.project_id, agent.id, None,
            "error",
            f"[{error_type}] {error_msg}"[:140],
            details={"error_type": error_type, "error": error_msg[:500]},
        )
    except Exception:
        pass

    # 写 agent_events — 监控面板依赖此表
    try:
        from hiveweave.services.event_audit import event_audit
        await event_audit.log(
            project_id=agent.project_id,
            agent_id=agent.id,
            event_type=f"llm_error.{error_type}",
            payload={"error": error_msg[:500], "error_type": error_type},
        )
    except Exception:
        pass

    # 发送 error 事件（前端 streamChat 等待此事件停止 loading）
    # 必须在 _reset_to_idle 之前发送，确保前端先处理错误再看到 idle 状态
    agent._broadcast_stream_event({
        "type": "error",
        "message": error_msg,
        "errorType": error_type,
        "agentId": agent.id,
    })

    # 广播健康事件 — LLM 调用出错 → health="error"（message 截断 200 字符）
    agent._broadcast_agent_health("error", error_msg[:200])

    # 保存错误消息到 DB — 更新 streaming placeholder 而非插入新消息
    is_trigger = bool(agent.pending_inbox_msg_ids)
    try:
        if agent._streaming_msg_id:
            await agent._finalize_streaming_turn(
                content=f"[ERROR] {error_msg}",
            )
        else:
            await agent._chat_msg.save_message(
                {
                    "agent_id": agent.id,
                    "role": "assistant",
                    "content": f"[ERROR] {error_msg}",
                    "is_streaming": False,
                    "is_background": True if is_trigger else False,
                }
            )
    except Exception as e:
        log.error("error_save_failed",
                  agent_id=agent.id, save_error=str(e))
        try:
            await agent._finalize_streaming_turn()
        except Exception:
            pass

    # 断流类打断且携带已产出的中间轮消息时，持久化完整中间产出
    # （参考 DSH 事件溯源：失败时已 durable 的内容也保留，resume 时
    # 模型完整读到中间轮，不再只留 user+[ERROR] 导致中间产出丢失）。
    # 注意：persist_partial_turn 在无中间产出/已写过失败标记时返回
    # False，此时必须回退 persist_failed_turn，保证「原始指令仍保留」
    # 的既有语义不被静默绕过（审计修复）。
    if _is_interruption_break(error) and partial_result is not None:
        _persisted = False
        try:
            _persisted = await persist_partial_turn(agent, partial_result)
        except Exception as e:
            log.warning("persist_partial_turn_failed", error=str(e))
        if not _persisted:
            try:
                await persist_failed_turn(agent, f"[ERROR] {error_msg}")
            except Exception as e2:
                log.warning(
                    "persist_failed_turn_on_error_failed", error=str(e2)
                )
    else:
        try:
            await persist_failed_turn(agent, f"[ERROR] {error_msg}")
        except Exception as e:
            log.warning("persist_failed_turn_on_error_failed", error=str(e))

    # 连续错误计数 — 超过阈值后 ACK inbox，不再 resume
    # 429 / rate-limit: 分级 — soft 冷却 / hard 配额 park（TEST20 P0-B）。
    # E7 审计修正：容量判定独立于 rate-limit 门禁——非 429 文案（HTTP 500
    # body / 200-body 包 GoUsageLimitError）同样必须进门禁，否则组织级
    # 暂停永远不触发，回到连环撞墙。
    from hiveweave.llm.retry import is_window_quota_error

    is_window_capacity = is_window_quota_error(error_msg)
    if is_rate_limit_error(error) or is_window_capacity:
        inbox_ids = list(agent.pending_inbox_msg_ids or [])
        headers: dict = {}
        try:
            from hiveweave.llm.retry import RetryableError, parse_quota_reset

            if isinstance(error, RetryableError):
                headers = dict(error.headers or {})
            quota = parse_quota_reset(headers)
        except Exception:
            quota = {
                "retry_after_s": None,
                "reset_at_epoch": None,
                "is_daily_quota": False,
            }

        retry_after_s = quota.get("retry_after_s")
        is_daily = bool(quota.get("is_daily_quota"))
        reset_at = quota.get("reset_at_epoch")

        # E7: 容量错误（daily_quota / GoUsageLimitError）→ 组织级降速 + park，
        # 不进 429 退避阶梯（秒级退避救不了窗口级配额，白撞只会烧预算）。
        # 审计修正：触发项目级暂停用「窄判 is_window_quota_error」（容量词 +
        # 窗口重置信号），避免把普通每分钟限流行判定成 1 小时组织暂停。
        is_capacity = is_window_quota_error(error_msg, is_daily=is_daily)
        if is_capacity:
            reset_at_epoch = float(reset_at) if reset_at else None
            try:
                await broadcast_project_capacity_pause(
                    agent.project_id,
                    reset_at_epoch,
                    source_agent_id=agent.id,
                )
            except Exception as e:
                log.debug(
                    "project_capacity_broadcast_error", error=str(e)
                )
            await agent._park_after_quota_exhausted(
                inbox_ids=inbox_ids,
                error_msg=error_msg,
                reset_at_epoch=reset_at_epoch,
                reason="daily_quota" if is_daily else "capacity",
            )
            agent._cancel_safety_timer()
            await agent._go_idle()
            return

        # No header → count soft streak with backoff ladder
        if retry_after_s is None:
            agent._rate_limit_streak = getattr(agent, "_rate_limit_streak", 0) + 1
            idx = min(
                agent._rate_limit_streak - 1,
                len(RATE_LIMIT_BACKOFF_STEPS_S) - 1,
            )
            cooldown = RATE_LIMIT_BACKOFF_STEPS_S[idx]
            if agent._rate_limit_streak >= RATE_LIMIT_SOFT_STREAK_ESCALATE:
                await agent._park_after_quota_exhausted(
                    inbox_ids=inbox_ids,
                    error_msg=error_msg,
                    reset_at_epoch=None,
                    reason="rate_limit_streak",
                )
                agent._cancel_safety_timer()
                await agent._go_idle()
                return
        else:
            cooldown = max(float(retry_after_s), RATE_LIMIT_RESUME_COOLDOWN_S)
            if is_daily or cooldown > RATE_LIMIT_SOFT_MAX_S:
                await agent._park_after_quota_exhausted(
                    inbox_ids=inbox_ids,
                    error_msg=error_msg,
                    reset_at_epoch=float(reset_at) if reset_at else None,
                    reason="daily_quota",
                )
                agent._cancel_safety_timer()
                await agent._go_idle()
                return
            agent._rate_limit_streak = getattr(agent, "_rate_limit_streak", 0) + 1

        if inbox_ids:
            await agent._write_resume_checkpoint(
                reason=f"rate_limit:{error_type}",
                inbox_ids=inbox_ids,
            )
            agent.pending_inbox_msg_ids = None
        agent._arm_resume_cooldown(cooldown)
        # TEST18 P0-5: AccountRateLimitExceeded is account-wide — cool the
        # whole project so peers don't keep stampeding the same key.
        if is_account_rate_limit(error):
            try:
                broadcast_project_rate_limit(
                    agent.project_id,
                    cooldown,
                    source_agent_id=agent.id,
                )
            except Exception as e:
                log.debug(
                    "project_rate_limit_broadcast_error",
                    error=str(e),
                )
        log.warning(
            "llm_rate_limit_deferred",
            agent_id=agent.id,
            cooldown_s=cooldown,
            rate_limit_streak=getattr(agent, "_rate_limit_streak", 0),
            is_daily_quota=is_daily,
            is_account_limit=is_account_rate_limit(error),
            consecutive_errors=agent._consecutive_errors,
            inbox_left_unread=len(inbox_ids),
        )
        agent._cancel_safety_timer()
        await agent._go_idle()
        return

    agent._rate_limit_streak = 0
    agent._consecutive_errors += 1
    is_total_timeout = (
        "请求总超时" in error_msg
        or "total timeout" in error_msg.lower()
        or "stream_total_timeout" in error_msg.lower()
    )
    if is_total_timeout:
        agent._stream_timeout_streak += 1
        # BUG-8: per-agent streak (park at >=2). Global telemetry count in
        # streamer is process-wide and must not be read as this streak.
        log.warning(
            "stream_timeout_agent_streak",
            agent_id=agent.id,
            agent_streak=agent._stream_timeout_streak,
            will_park=agent._stream_timeout_streak >= 2,
        )
    else:
        agent._stream_timeout_streak = 0

    inbox_ids = list(agent.pending_inbox_msg_ids or [])

    # TEST4: ≥2 consecutive stream total timeouts → park waiting + escalate
    if is_total_timeout and agent._stream_timeout_streak >= 2:
        await agent._park_after_stream_timeouts(
            inbox_ids=inbox_ids, error_msg=error_msg
        )
        agent._cancel_safety_timer()
        await agent._go_idle()
        return

    if inbox_ids and agent._consecutive_errors <= agent._CONSECUTIVE_ERROR_MAX:
        # 未超阈值: 保留未读，冷却后 resume
        await agent._write_resume_checkpoint(
            reason=f"llm_error:{error_type}",
            inbox_ids=inbox_ids,
        )
        agent._arm_resume_cooldown(ERROR_RESUME_COOLDOWN_S)
        agent.pending_inbox_msg_ids = None
        log.warning(
            "llm_error_inbox_left_unread",
            agent_id=agent.id,
            inbox_left_unread=len(inbox_ids),
            cooldown_s=ERROR_RESUME_COOLDOWN_S,
            consecutive_errors=agent._consecutive_errors,
        )
    elif inbox_ids and agent._consecutive_errors > agent._CONSECUTIVE_ERROR_MAX:
        # 超过阈值: 选择性 ACK（保留待审/升级类）+ 账本再挂 + 升级上级
        agent._arm_resume_suppressed()
        try:
            await agent._ack_inbox_on_give_up(inbox_ids)
        except Exception as ack_err:
            log.error("inbox_ack_failed", agent_id=agent.id, error=str(ack_err))
        agent.pending_inbox_msg_ids = None
        await agent._escalate_turn_interruption(reason=f"llm_error:{error_type}")
    elif agent._consecutive_errors > agent._CONSECUTIVE_ERROR_MAX:
        agent._arm_resume_suppressed()
        try:
            await agent._inject_ledger_review_wake()
        except Exception as e:
            log.debug("ledger_rewake_on_give_up_failed", error=str(e))
        await agent._escalate_turn_interruption(reason=f"llm_error:{error_type}")
    else:
        # TEST_DSH_24 视界事故：续跑轮断流（无未读 inbox）时原三条分支全不
        # 进 → 直接 _go_idle 零补偿，名下开放任务裸奔到 10min watchdog。
        # 补法：按 ADR-001 闭式判定查名下开放工作，有活则挂 ~45s 补偿唤醒
        # （复用 _arm_interrupted_resume，秒级恢复，watchdog 退为兜底）。
        # 无活（真收工后断流）不挂——保持安静。
        try:
            from hiveweave.services.task import TaskService

            _open_refs = [
                str(t.get("id") or "")[:8]
                for t in (
                    await TaskService().get_open_work_obligations(
                        agent.project_id, agent.id
                    )
                    or []
                )
                if t.get("assignee_id") == agent.id and t.get("id")
            ]
        except Exception as e:
            log.warning(
                "llm_error_open_work_scan_failed",
                agent_id=agent.id,
                error=str(e),
            )
            _open_refs = []
        if _open_refs:
            agent._arm_interrupted_resume(_open_refs)
            log.warning(
                "llm_error_open_work_resume_armed",
                agent_id=agent.id,
                task_refs=_open_refs[:8],
                consecutive_errors=agent._consecutive_errors,
            )

    agent._cancel_safety_timer()
    await agent._go_idle()

async def park_after_quota_exhausted(
    agent: Any,
    *,
    inbox_ids: list[str],
    error_msg: str,
    reset_at_epoch: float | None,
    reason: str = "daily_quota",
) -> None:
    """Park agent until quota reset — stop doomed 429 retry loops (TEST20 P0-B)."""
    import time as _time

    from hiveweave.services.turn_result import WaitingOnItem
    from hiveweave.services.wait_contract import wait_contract_service

    agent.disposition = "waiting_human"
    agent._arm_resume_suppressed()
    agent._rate_limit_streak = 0

    note = "LLM quota exhausted"
    if reset_at_epoch:
        local = _time.strftime(
            "%H:%M", _time.localtime(float(reset_at_epoch))
        )
        note = f"LLM quota exhausted; resume after {local}"
        # Arm cooldown until reset so early wakes stay blocked
        delay = max(0.0, float(reset_at_epoch) - _time.time())
        if delay > 0:
            agent._arm_resume_cooldown(delay)

    try:
        await wait_contract_service.replace_waits(
            agent.project_id,
            agent.id,
            [
                WaitingOnItem(
                    kind="timer",
                    ref="quota_reset",
                    note=note[:200],
                )
            ],
            phase="waiting",
        )
    except Exception as e:
        log.warning(
            "quota_wait_persist_failed",
            agent_id=agent.id,
            error=str(e),
        )

    if inbox_ids:
        try:
            await agent._write_resume_checkpoint(
                reason=f"quota_exhausted:{reason}",
                inbox_ids=inbox_ids,
            )
        except Exception:
            pass
        # Leave unread so resume after reset reprocesses them
        agent.pending_inbox_msg_ids = None

    agent_name = agent.config.get("name", agent.id)
    reset_blob = (
        _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(reset_at_epoch)))
        if reset_at_epoch
        else "(unknown — check provider dashboard)"
    )
    try:
        await agent._broadcast_agent_health(
            "error",
            f"quota exhausted until {reset_blob}",
        )
    except Exception:
        pass
    try:
        superior = await agent._org.get_superior(agent.id)
        if superior:
            await agent._inbox.send_message(
                from_agent_id=agent.id,
                to_agent_id=superior["id"],
                message=(
                    f"[QUOTA EXHAUSTED] {agent_name} parked after rate-limit "
                    f"({reason}). Resume at {reset_blob}. "
                    f"Swap to a paid key / Ark pool or wait. "
                    f"Last error: {error_msg[:120]}"
                ),
                message_type="system",
                priority="urgent",
                wake=False,
            )
    except Exception as e:
        log.warning("quota_escalate_failed", agent_id=agent.id, error=str(e))
    try:
        # Notify user channel when possible
        await agent._inbox.send_message(
            from_agent_id=agent.id,
            to_agent_id="user",
            message=(
                f"[QUOTA EXHAUSTED] Project LLM quota hit. "
                f"Agents parked until {reset_blob}. "
                f"Change model key in Settings or wait for reset."
            ),
            message_type="system",
            priority="urgent",
            wake=False,
        )
    except Exception:
        pass
    log.warning(
        "llm_quota_parked",
        agent_id=agent.id,
        reason=reason,
        reset_at=reset_blob,
    )

async def park_after_stream_timeouts(
    agent: Any, *, inbox_ids: list[str], error_msg: str
) -> None:
    """After consecutive stream total timeouts: park waiting + wake parent.

    TEST21 M5: payload lists assignee's open tasks, marks owner_parked,
    and escalates with ``[PARKED WITH TASKS]`` (facts + suggested actions).
    """
    from hiveweave.services.turn_result import WaitingOnItem
    from hiveweave.services.wait_contract import wait_contract_service

    agent.disposition = "waiting_agent"
    agent._arm_resume_suppressed()
    agent._stream_timeout_streak = 0

    open_tasks: list[dict] = []
    try:
        from hiveweave.services.task import TaskService

        ts = TaskService()
        for t in (
            await ts.list_tasks(agent.project_id, assignee_id=agent.id) or []
        ):
            st = t.get("status")
            if st in (
                "created",
                "claimed",
                "running",
                "submitted",
                "reviewing",
                "rework",
                "blocked",
                "approved",
            ):
                open_tasks.append(t)
        if open_tasks:
            await ts.set_owner_parked(
                agent.project_id,
                [str(t["id"]) for t in open_tasks if t.get("id")],
                parked=True,
            )
    except Exception as e:
        log.warning(
            "stream_timeout_park_tasks_failed",
            agent_id=agent.id,
            error=str(e),
        )

    try:
        await wait_contract_service.replace_waits(
            agent.project_id,
            agent.id,
            [
                WaitingOnItem(
                    kind="timer",
                    ref="stream_total_timeout_recovery",
                    note="Parked after consecutive stream total timeouts",
                )
            ],
            phase="waiting",
        )
    except Exception as e:
        log.warning(
            "stream_timeout_wait_persist_failed",
            agent_id=agent.id,
            error=str(e),
        )

    if inbox_ids:
        try:
            await agent._ack_inbox_on_give_up(list(inbox_ids))
        except Exception:
            pass
    agent.pending_inbox_msg_ids = None

    agent_name = agent.config.get("name", agent.id)
    lines: list[str] = []
    for t in open_tasks[:12]:
        tid = str(t.get("id") or "")[:8]
        st = t.get("status") or "?"
        title = (t.get("title") or "").split("\n")[0][:40]
        hint = {
            "running": "hold / reassign / release claim",
            "claimed": "hold / reassign / release claim",
            "submitted": "review or wait for recovery",
            "reviewing": "review or wait for recovery",
            "approved": "merge when ready",
            "rework": "hold / reassign",
            "blocked": "unblock or reassign",
        }.get(str(st), "triage")
        lines.append(f"- {tid} [{st}] {title} → {hint}")
    task_blob = "\n".join(lines) if lines else "(no open assignee tasks)"
    try:
        superior = await agent._org.get_superior(agent.id)
        if superior:
            await agent._inbox.send_message(
                from_agent_id=agent.id,
                to_agent_id=superior["id"],
                message=(
                    f"[PARKED WITH TASKS] {agent_name} hit consecutive "
                    f"stream total timeouts and is parked waiting.\n"
                    f"Open tasks (stall nudges paused via owner_parked):\n"
                    f"{task_blob}\n"
                    f"Assess activity before acting. "
                    f"Last error: {error_msg[:120]}"
                ),
                message_type="escalation",
                priority="urgent",
                wake=True,
            )
            from hiveweave.agents.trigger import trigger_coordinator

            await trigger_coordinator(superior["id"])
            log.warning(
                "stream_timeout_parked_escalated",
                agent_id=agent.id,
                superior_id=superior["id"],
                pending_tasks=[str(t.get("id") or "")[:8] for t in open_tasks],
            )
        else:
            log.warning(
                "stream_timeout_parked_no_superior",
                agent_id=agent.id,
            )
    except Exception as e:
        log.error(
            "stream_timeout_escalate_failed",
            agent_id=agent.id,
            error=str(e),
        )

async def handle_safety_timeout(agent: Any) -> None:
    """安全超时异步清理。

    与 _handle_error 统一的非致命中断策略（计数 + 冷却 resume + 超限放弃）:
    - 未超限: 不 ACK inbox（消息保持未读）+ RESUME CHECKPOINT + 冷却 resume
    - 连续超限: ACK inbox 放弃本轮 + 升级上级一次 —— 堵住
      「卡住中断 → 90s 冷却 → 再卡住」的无限死循环，且不再注入
      CHECKPOINT 撑大上下文让下一轮更易超时
    - 记录 work_log，便于监控/stall watchdog 关联
    """
    inbox_ids = list(agent.pending_inbox_msg_ids or [])

    # E5: safety_timeout 打断 → 置位降级标志（与 handle_error 断流同口径）。
    mark_degraded(agent.id)

    # ── Durable Run Ledger: mark run interrupted ──
    _run_id = getattr(agent, "_current_run_id", None)
    if _run_id:
        try:
            await agent._run_ledger.interrupt_run(
                agent_id=agent.id,
                run_id=_run_id,
                reason="safety_timeout",
            )
        except Exception as e:
            log.debug("run_ledger.interrupt_run_failed", error=str(e))

    # 连续中断计数 — 与 _handle_error 共用同一阈值
    agent._consecutive_errors += 1
    give_up = agent._consecutive_errors > agent._CONSECUTIVE_ERROR_MAX

    timeout_msg = (
        "[TIMEOUT] Run interrupted (stuck stream or call budget). "
        + (
            f"Gave up after {agent._consecutive_errors} consecutive "
            "interrupted turns; escalated to superior."
            if give_up
            else "Inbox left unread for resume after cooldown."
        )
    )
    if agent._streaming_msg_id:
        await agent._finalize_streaming_turn(content=timeout_msg)
    else:
        await agent._chat_msg.update_streaming_messages_done(agent.id)
        await agent._chat_msg.save_message(
            {
                "agent_id": agent.id,
                "role": "assistant",
                "content": timeout_msg,
                "is_streaming": False,
            }
        )

    try:
        await persist_failed_turn(agent, timeout_msg)
    except Exception as e:
        log.warning("persist_failed_turn_on_timeout_failed", error=str(e))

    if not give_up:
        # 未超阈值: 保留未读 + CHECKPOINT + 冷却，watcher 冷却后恢复信息链
        await agent._write_resume_checkpoint(
            reason="safety_timeout",
            inbox_ids=inbox_ids,
        )
        agent._arm_resume_cooldown(TIMEOUT_RESUME_COOLDOWN_S)
    else:
        # 超阈值放弃: 选择性 ACK + 账本再挂 + 升级上级
        agent._arm_resume_suppressed()
        if inbox_ids:
            try:
                await agent._ack_inbox_on_give_up(inbox_ids)
            except Exception as ack_err:
                log.error("inbox_ack_failed", agent_id=agent.id, error=str(ack_err))
        else:
            try:
                await agent._inject_ledger_review_wake()
            except Exception as e:
                log.debug("ledger_rewake_on_timeout_failed", error=str(e))
        try:
            await agent._work_log.write_work_log(
                agent.project_id, agent.id, None,
                "error",
                f"[safety_timeout] gave up after {agent._consecutive_errors} "
                "consecutive interrupted turns; non-critical inbox ACKed, "
                "review-critical kept; escalated",
                details={
                    "reason": "safety_timeout",
                    "inbox_ids": inbox_ids[:20],
                    "consecutive_errors": agent._consecutive_errors,
                    "resume": False,
                },
            )
        except Exception:
            pass
        await agent._escalate_turn_interruption(reason="safety_timeout")

    # Keep pending_inbox_msg_ids cleared from this turn; 未放弃时消息
    # 在 DB 保持未读，冷却结束后由 watcher 恢复信息链。
    agent.pending_inbox_msg_ids = None

    agent._cancel_safety_timer()
    await agent._go_idle()

    # 广播健康事件 — 卡住中的流 / 调用预算 → health="error"
    agent._broadcast_agent_health(
        "error", "Run interrupted (stuck stream or call budget)"
    )
    log.warning(
        "safety_timeout_gave_up" if give_up else "safety_timeout_resume_armed",
        agent_id=agent.id,
        inbox_left_unread=0 if give_up else len(inbox_ids),
        inbox_acked=len(inbox_ids) if give_up else 0,
        consecutive_errors=agent._consecutive_errors,
        cooldown_s=0.0 if give_up else TIMEOUT_RESUME_COOLDOWN_S,
    )

async def force_interrupt_stuck_stream(
    agent: Any, *, reason_detail: str = ""
) -> bool:
    """P0-3 streaming 僵尸强制中断（game_time sweep 的外部兜底）。

    「卡住中的流」= agent PROCESSING 但流式超阈值无任何事件（safety timer
    单点失效时的第二道保险）。语义等同 safety timeout，复用其恢复账：
    统一错误计数 + 冷却 resume / 超限放弃 + 升级上级（handle_safety_timeout）。
    返回是否实际执行了中断（非 PROCESSING 时 fail-open 返回 False）。
    """
    if agent.status != AgentState.PROCESSING:
        return False
    task = agent._llm_task
    log.warning(
        "streaming_zombie_force_interrupt",
        agent_id=agent.id,
        detail=reason_detail,
        has_live_llm_task=bool(task is not None and not task.done()),
    )
    if agent._cancel_reason is None:
        agent._cancel_reason = "safety_timeout"
    elif agent._cancel_reason != "safety_timeout":
        # 已有在途取消（用户 cancel / off_duty）——让既有恢复路径主导，不劫持
        return False
    if task is not None and not task.done():
        # 正常路径：CancelledError handler 见 safety_timeout →
        # handle_safety_timeout（收尾 + 复位 idle + 冷却/升级）。
        # 已取消在途（safety timer / 上一轮 sweep 已发）时勿二次 cancel：
        # handler 内有 await 点，二次 cancel 会在 except 块内再抛
        # CancelledError，中途打断 handle_safety_timeout。
        if not task.cancelling():
            task.cancel()
        return True
    # 状态脱钩（PROCESSING 但无活 LLM task —— CancelledError handler
    # 不会跑）：直接走 safety_timeout 恢复，内部 _go_idle 复位。
    await handle_safety_timeout(agent)
    return True

async def escalate_turn_interruption(agent: Any, *, reason: str) -> None:
    """连续中断超限，给上级发一次升级消息。

    每个失败 streak 只升级一次（计数恰好越限的那次）——后续连续失败仍
    选择性 ACK inbox 止血，但不重复打扰上级，避免「升级 → 上级追问 → 再失败 →
    再升级」的跨 agent 振荡。成功后计数归零，新的 streak 会再次升级。
    best-effort：升级失败只记日志，不阻断清理流程。
    """
    if agent._consecutive_errors != agent._CONSECUTIVE_ERROR_MAX + 1:
        return
    try:
        superior = await agent._org.get_superior(agent.id)
        if superior:
            agent_name = agent.config.get("name", agent.id)
            await agent._inbox.send_message(
                from_agent_id=agent.id,
                to_agent_id=superior["id"],
                message=(
                    f"[ESCALATION] Subordinate {agent_name} gave up a turn "
                    f"after {agent._consecutive_errors} consecutive "
                    f"interruptions (last: {reason}). Non-critical inbox "
                    f"was ACKed; review-critical / ask messages were kept. "
                    f"Please check on them and any submitted tasks."
                ),
                message_type="escalation",
                priority="urgent",
                wake=True,
            )
            # 触发上级
            from hiveweave.agents.trigger import trigger_coordinator

            await trigger_coordinator(superior["id"])
            log.warning(
                "interruption_escalated",
                agent_id=agent.id,
                superior_id=superior["id"],
                reason=reason,
                consecutive_errors=agent._consecutive_errors,
            )
        else:
            log.warning(
                "interruption_escalate_no_superior",
                agent_id=agent.id,
                reason=reason,
                msg="no superior to escalate to",
            )
    except Exception as e:
        log.error(
            "interruption_escalate_failed", agent_id=agent.id, error=str(e)
        )

async def handle_cancel(agent: Any) -> None:
    """用户取消处理。

    对齐 Elixir agent.ex:131 handle_cast(:cancel)。
    """
    # ── Durable Run Ledger: mark run interrupted ──
    _run_id = getattr(agent, "_current_run_id", None)
    if _run_id:
        try:
            await agent._run_ledger.interrupt_run(
                agent_id=agent.id,
                run_id=_run_id,
                reason="cancelled_by_user",
            )
        except Exception as e:
            log.debug("run_ledger.interrupt_cancel_failed", error=str(e))

    await agent._finalize_streaming_turn(content="[对话被中断]")

    # DB inbox 未读保留（用户取消不应 mark_read）。内存 claim
    # （pending_inbox_msg_ids）由 Agent.cancel() 在 await task 前释放；
    # 若当时 claim 非空，cancel 会 revive watcher 让未读再被 claim。

    agent._cancel_safety_timer()
    agent._reset_to_idle()

async def escalate_unreplied(agent: Any, unreplied_msgs: list[dict]) -> None:
    """达到提醒上限后，升级到上级。

    给上级发消息，列出下属未回复的人员和消息。
    """
    # 获取自己的 name 和 parent_id
    me = await meta_db.get_agent_by_id(agent.id)
    my_name = me.get("name", agent.id[:8]) if me else agent.id[:8]
    parent_id = me.get("parent_id") if me else None

    if not parent_id:
        log.warning("escalate_no_parent",
                    agent_id=agent.id, name=my_name,
                    unreplied_count=len(unreplied_msgs))
        return

    # unreplied_msgs 已带有 from_name（由 _check_unreplied_expect_report 解析）
    lines = []
    for m in unreplied_msgs:
        from_name = m.get("from_name") or m.get("from_agent_id", "?")[:8]
        preview = (m.get("message") or "")[:60]
        lines.append(f"  - {from_name}：{preview}")

    msg = (
        f"[ESCALATION] 你的下属 {my_name} 经过 {agent._REPLY_REMINDER_MAX} 次提醒后，"
        f"仍未回复以下 {len(unreplied_msgs)} 人的消息，请直接介入协调：\n"
        + "\n".join(lines)
    )

    try:
        from hiveweave.services.inbox import InboxService
        await InboxService().send_message(
            "system", parent_id, msg,
            message_type="system", priority="urgent")
        from hiveweave.agents.trigger import trigger_subordinate
        await trigger_subordinate(parent_id)
        log.warning("reply_escalated",
                    agent_id=agent.id, name=my_name,
                    parent_id=parent_id,
                    unreplied_count=len(unreplied_msgs))
    except Exception as e:
        log.error("escalate_failed", agent_id=agent.id, error=str(e))

async def escalate_trigger_fuse(agent: Any, pending: list[dict]) -> None:
    """Fuse tripped: notify parent, keep actionable inbox unread."""
    me = await meta_db.get_agent_by_id(agent.id)
    my_name = me.get("name", agent.id[:8]) if me else agent.id[:8]
    parent_id = me.get("parent_id") if me else None
    if not parent_id:
        return
    types = [
        (m.get("message_type") or "?") for m in (pending or [])[:8]
    ]
    msg = (
        f"[TRIGGER FUSE] {my_name} has {len(pending)} actionable inbox "
        f"message(s) that repeated wakes failed to consume "
        f"(types={types}). Inbox was NOT auto-acked — intervene."
    )
    try:
        from hiveweave.services.inbox import InboxService

        await InboxService().send_message(
            "system",
            parent_id,
            msg,
            message_type="escalation",
            priority="urgent",
            wake=True,
            idempotency_key=(
                f"trigger_fuse:{agent.id}:{int(time.time()) // 600}"
            ),
        )
        from hiveweave.agents.trigger import trigger_subordinate

        await trigger_subordinate(parent_id)
    except Exception as e:
        log.warning(
            "trigger_fuse_escalate_failed",
            agent_id=agent.id,
            error=str(e),
        )
