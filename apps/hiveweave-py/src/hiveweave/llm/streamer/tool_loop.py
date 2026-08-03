"""Tool-loop orchestration mixin."""
from __future__ import annotations

import time
from typing import Any

import structlog

from hiveweave.llm.provider import ProviderConfig

from .constants import (
    ACTIVITY_EXTEND_S,
    DEFAULT_PLACEHOLDER,
    HARD_TOTAL_TIMEOUT_S,
    MAX_TOOLS_PER_ROUND,
    NO_TEXT_HINT_MAX,
    NO_TEXT_ROUNDS_THRESHOLD,
    TOOL_LOOP_READONLY_STALL_LIMIT,
    TOOL_LOOP_STALL_LIMIT,
    TOTAL_TIMEOUT_S,
)
from .doom_loop import round_made_progress, round_was_readonly_only
from .types import DeltaCallback, ToolCallCallback

log = structlog.get_logger(__name__)


class ToolLoopMixin:
    """Tool loop main cycle for Streamer."""

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
    ) -> dict:
        """Tool loop: 流式请求 → 检查 tool_calls → 执行工具 → 重复。"""
        # 使用调用方传入的上限，回退到实例默认值
        rounds_cap = max_tool_rounds if max_tool_rounds else self.max_tool_rounds
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

        # TEST21 M4: soft/hard turn budget (activity renews soft within hard)
        loop_start = time.monotonic()
        hard_deadline = loop_start + HARD_TOTAL_TIMEOUT_S
        soft_deadline = loop_start + TOTAL_TIMEOUT_S
        budget_hint_injected = False

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
                final_text = self._strip_placeholder(text_acc) or (
                    "[TURN BUDGET] Hard turn budget exhausted. "
                    "Progress so far is kept — call commit_turn("
                    "phase='in_progress') and continue in the next wake."
                )
                if tool_history:
                    tool_turn_acc.append(
                        {"role": "assistant", "content": final_text}
                    )
                    return {
                        "status": "ok",
                        "content": final_text,
                        "thinking": thinking_acc,
                        "tool_calls": tool_history,
                        "tool_turn_messages": tool_turn_acc,
                        "rounds": round_num,
                        "usage": last_usage,
                        "budget_exhausted": True,
                    }
                await self._fire_delta(on_delta, {
                    "type": "error",
                    "content": f"请求总超时（{HARD_TOTAL_TIMEOUT_S}s）",
                })
                return self._error_result("请求总超时", loop_start)
            if (
                now_mono >= soft_deadline
                and not budget_hint_injected
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

            # 通知回调：新一轮开始（用于重置流式文本累积器）
            # BUG-7: also fire on round 0 so LLM call counters stay accurate
            if on_delta:
                await self._fire_delta(on_delta, {
                    "type": "round_start",
                    "round": round_num,
                })

            # 上下文溢出检查
            messages = self._trim_context_if_needed(messages, provider)

            # 中轮提醒: 80% 轮次时注入
            messages = self._maybe_inject_mid_round_reminder(
                messages, round_num, rounds_cap
            )

            log.info("tool_loop_round",
                     agent_id=agent_id, round=round_num,
                     msg_count=len(messages))

            # 单轮流式请求（带空响应重试）
            round_result = await self._stream_with_empty_retry(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=messages,
                tools=tools,
                on_delta=on_delta,
                round_num=round_num,
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
                    "error": round_result.get("error"),
                    "error_status": round_result.get("error_status"),
                    "error_headers": round_result.get("error_headers"),
                }

            new_text = round_result["text"] or ""
            new_thinking = round_result["thinking"] or ""
            tool_calls = round_result["tool_calls"]
            finish_reason = round_result["finish_reason"]
            last_usage = round_result.get("usage")

            combined_text = text_acc + new_text
            combined_thinking = thinking_acc + new_thinking

            log.info("round_result",
                     agent_id=agent_id, round=round_num,
                     text_len=len(new_text), tool_count=len(tool_calls),
                     finish=finish_reason)

            # 处理截断的响应
            if finish_reason in ("length", "content_filter") and tool_calls:
                # 截断的 tool_calls 可能不完整，丢弃
                log.warning("discard_incomplete_tool_calls",
                            agent_id=agent_id, round=round_num,
                            finish=finish_reason)
                real_text = self._strip_placeholder(combined_text)
                warning = f"\n\n⚠️ 响应被截断（{finish_reason}），部分工具调用可能不完整。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            if finish_reason == "length":
                log.warning("response_truncated_length", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被截断（达到最大输出长度），请继续以完成。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            if finish_reason == "content_filter":
                log.warning("content_filtered", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被内容过滤器截断。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
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
                            "error": f"Doom loop detected: tool '{doom}' called "
                                     f"{limit}+ times with same args (after warning)",
                        }

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
                duplicate_ids: set[str] = set()
                if on_tool_call is None:
                    log.error("no_tool_executor", agent_id=agent_id)
                    tool_results = [
                        {"role": "tool", "content": "[No tool executor]",
                         "tool_call_id": tc["id"]}
                        for tc in tool_calls
                    ]
                    error_ids = {tc["id"] for tc in tool_calls}
                else:
                    tool_results, error_ids, duplicate_ids, end_turn = (
                        await self._execute_tools(
                            agent_id=agent_id,
                            tool_calls=tool_calls,
                            on_tool_call=on_tool_call,
                            on_delta=on_delta,
                            poll_turn_counts=poll_turn_counts,
                        )
                    )
                    # duplicate 信号：工具返回 duplicate=True 表示本次调用无新
                    # 效果（如 commit_turn 同参已接受过）。强制 +1 计数，让下一轮
                    # _detect_doom_loop 更快触顶。error_ids 保留用于日志/观测。
                    if duplicate_ids:
                        doom_tracker["count"] = (
                            doom_tracker.get("count", 0) + 1
                        )

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
                        "end_turn": True,
                    }

                # DESIGN-2: stall counter — no mutating progress → force outer loop
                # Pure-readonly polling uses a higher limit (dogfood retune).
                if round_made_progress(
                    tool_calls,
                    error_ids=error_ids,
                    duplicate_ids=duplicate_ids,
                ):
                    stall_count = 0
                    readonly_stall_count = 0
                    # TEST21 M4: activity renews soft budget within hard cap
                    soft_deadline = min(
                        time.monotonic() + ACTIVITY_EXTEND_S, hard_deadline
                    )
                elif round_was_readonly_only(
                    tool_calls,
                    error_ids=error_ids,
                    duplicate_ids=duplicate_ids,
                ):
                    readonly_stall_count += 1
                    stall_count = 0
                else:
                    stall_count += 1
                    readonly_stall_count = 0
                stalled = (
                    stall_count >= TOOL_LOOP_STALL_LIMIT
                    or readonly_stall_count >= TOOL_LOOP_READONLY_STALL_LIMIT
                )
                if stalled:
                    log.warning(
                        "tool_loop_stall",
                        agent_id=agent_id,
                        round=round_num,
                        stall_count=stall_count,
                        readonly_stall_count=readonly_stall_count,
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        telemetry.tool_loop_stall(
                            agent_id,
                            stall_count=max(stall_count, readonly_stall_count),
                        )
                    except Exception:
                        pass
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[STALL BREAK] {stall_count} consecutive tool-loop "
                            "rounds made no progress (only readonly / failed / "
                            "duplicate tools). Stop polling. Call commit_turn "
                            "now (waiting/blocked/done_slice) or change approach "
                            "— do not repeat the same readonly loop."
                        ),
                    })
                    summary = await self._make_max_rounds_summary(
                        agent_id, provider, messages, on_delta,
                        reason="stall_break",
                    )
                    final_text = self._strip_placeholder(summary)
                    if not final_text:
                        final_text = (
                            "[STALL BREAK] No progress for "
                            f"{stall_count} rounds — turn ended."
                        )
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
                        "stall_break": True,
                    }

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
                            )
                            # FIX(text-acc): 同 max_rounds 路径，只用 summary
                            final_text = self._strip_placeholder(summary)
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
                }

            # 有真实文本 — 剥离占位符，结束
            # FIX(text-acc): 只用最终轮的文本 (new_text)，不拼接中间轮。
            # 中间轮文本已通过 line 777 的 per-round assistant 消息保存在
            # tool_turn_acc 中，无需在最终消息中重复。之前的 combined_text
            # 会拼接所有轮的文本，导致同一分析语句在最终消息中重复 3-5 次，
            # 进而污染 conversation_turns 和下一轮的 LLM 上下文。
            final_text = self._strip_placeholder(new_text)
            final_msg = {"role": "assistant", "content": final_text}
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
            }

        # 达到最大轮次 — 做一次无工具的总结调用
        log.warning("max_rounds_reached",
                    agent_id=agent_id,
                    max_rounds=rounds_cap)
        summary = await self._make_max_rounds_summary(
            agent_id, provider, messages, on_delta,
            reason="max_rounds",
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
        }

