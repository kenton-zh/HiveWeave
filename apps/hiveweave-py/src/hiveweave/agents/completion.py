"""Turn completion / exit-gate handling.

Extracted from agent.py — behavior-preserving mechanical split (P1b).
Module-level functions take ``agent`` as first arg; Agent methods are thin wrappers.

MUST NOT top-level import hiveweave.agents.trigger — lazy import inside functions only.
"""

from __future__ import annotations

import json
from typing import Any, cast

import structlog

from hiveweave.agents.constants import (
    STALL_BREAK_WINDOW_MS,
    STALL_BREAK_PARK_THRESHOLD,
    STALL_BREAK_RECENT_OK_MS,
)
from hiveweave.agents.helpers.stall import (
    _stall_break_ledger,
    _turn_has_substantial_progress,
    _recent_successful_run_ms,
)

log = structlog.get_logger(__name__)


async def handle_completion(
    agent: Any,
    result: dict,
    message: str,
    opts: dict,
) -> None:
    """正常完成处理。

    对齐 Elixir agent.ex:553 handle_info({ref, {:ok, ...}})。

    流程:
    1. 取消安全定时器
    2. 保存消息到 chat_messages + conversation store
    3. 标记 inbox 已读
    4. 状态 → idle
    5. 自检 re-trigger
    """
    content = result.get("content", "")
    thinking = result.get("thinking")
    tool_calls = result.get("tool_calls", [])

    # ── Durable Run Ledger: mark run completed ──
    _run_id = getattr(agent, "_current_run_id", None)
    if _run_id:
        try:
            summary = (content or "")[:200]
            await agent._run_ledger.complete_run(
                agent_id=agent.id,
                run_id=_run_id,
                result_summary=summary,
            )
        except Exception as e:
            log.debug("run_ledger.complete_run_failed", error=str(e))
    tool_turn_messages = result.get("tool_turn_messages", [])

    log.info(
        "llm_completion",
        agent_id=agent.id,
        content_len=len(content),
        tool_calls=len(tool_calls),
        rounds=result.get("rounds", 0),
        usage=result.get("usage"),
    )

    # 1. 先写 work_log（在 update_message 之前，确保监控有数据）
    # BUG-026 修复：自动写 work_log，确保前端 Logs tab 有内容。
    # 放在 update_message 之前——update_message 可能因类型问题崩溃
    # （如 thinking 意外为 dict），work_log 不应被其连累。
    try:
        summary_src = content if content else message
        summary = (summary_src or "").strip().replace("\n", " ")[:140]
        if not summary:
            summary = "(empty response)"
        log_type = "completion" if content else "discussion"
        details: dict | None = None
        if tool_calls:
            names = sorted({
                tc.get("function", {}).get("name", "?")
                for tc in tool_calls
                if isinstance(tc, dict)
            })
            details = {
                "tool_calls": names,
                "rounds": result.get("rounds", 0),
            }
        await agent._work_log.write_work_log(
            agent.project_id, agent.id, None, log_type, summary,
            details=details,
        )
    except Exception as e:
        log.warning("auto_work_log_failed", agent_id=agent.id, error=str(e))

    # 2. 保存 assistant 消息到 chat_messages
    # 更新先前保存的 streaming placeholder，而非插入新消息。
    # 用 try/except 包裹——保存失败不应导致整个 completion 崩溃。
    # 失败了就降级保存一条简单消息 + 注入对话反馈，让 AI 知道格式有问题。
    is_trigger = opts.get("trigger", False)
    _save_failed = False
    _save_error_msg = ""
    try:
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else "[]"
        )
        if agent._streaming_msg_id:
            cleared = await agent._finalize_streaming_turn(
                content=content,
                thinking=thinking if thinking is not None else None,
                tool_calls_json=tool_calls_json,
            )
            if not cleared:
                _save_failed = True
                _save_error_msg = "finalize_streaming_turn failed"
        else:
            await agent._chat_msg.save_message(
                {
                    "agent_id": agent.id,
                    "role": "assistant",
                    "content": content,
                    "thinking": thinking,
                    "tool_calls": tool_calls_json,
                    "is_streaming": False,
                    "is_background": True if is_trigger else False,
                }
            )
    except Exception as e:
        _save_failed = True
        _save_error_msg = str(e)
        log.error("completion_save_failed",
                  agent_id=agent.id, error=_save_error_msg)
        try:
            await agent._finalize_streaming_turn(
                content=content[:500] if content else "(empty)",
            )
        except Exception:
            pass  # 尽力了

    # 3. 追加到 conversation store
    # user message + tool turn messages (assistant+tool pairs) + final assistant
    turn_messages: list[dict] = [{"role": "user", "content": message}]
    # 如果消息保存失败，注入错误反馈让 AI 意识到问题
    if _save_failed:
        turn_messages.append({
            "role": "tool",
            "tool_call_id": "_save_message",
            "content": (
                f"SYSTEM ERROR: Your last response was produced successfully "
                f"(content length: {len(content)}, tool calls: {len(tool_calls)}), "
                f"but saving it to the database failed with: {_save_error_msg}. "
                f"This is a platform bug (type mismatch in message field), NOT your fault. "
                f"The user can still see your response in the conversation history. "
                f"Continue your work as normal."
            ),
        })
    turn_messages.extend(tool_turn_messages)
    # Do not persist base64 screenshots into conversation history —
    # they are for the in-flight tool loop only; next turn can re-screenshot.
    from hiveweave.services.vision import messages_without_images

    await agent._conversation.append_turn(
        agent.id, agent.project_id, messages_without_images(turn_messages)
    )

    # 3. Turn exit gates — validate only; scheduler decides continue/park
    from hiveweave.db import meta as meta_db
    from hiveweave.services.turn_exit import (
        ExitContext,
        collect_unreplied_asks,
        evaluate_turn_exit,
    )
    from hiveweave.services.turn_session import pop_pending_turn_result

    # UNREPLIED / ACK scope = ids shown this turn only (pending_inbox_msg_ids).
    # Mid-turn arrivals are NOT injected into the live streamer — merging them
    # into obligations or ACK would silently drop messages the model never saw.
    pending_msgs: list[dict] = []
    if agent.pending_inbox_msg_ids:
        all_pending = await agent._inbox.get_pending_messages(agent.id)
        id_set = set(agent.pending_inbox_msg_ids)
        pending_msgs = [m for m in all_pending if m["id"] in id_set]

    name_by_id: dict[str, str] = {}
    exempt_senders: set[str] = set()
    from hiveweave.services.wake_policy import is_user_sender

    for m in pending_msgs:
        fid = m.get("from_agent_id") or ""
        if not fid:
            continue
        ag = await meta_db.get_agent_by_id(fid)
        if fid not in name_by_id:
            name_by_id[fid] = ag.get("name", fid[:8]) if ag else fid[:8]
        # 豁免边界（结构化判定，不猜文案）：
        # - user/system 发送方：回复通道是 assistant 输出本身
        # - 发送方已归档/不存在：回复义务随其消亡，不得死锁退出门禁
        if (
            is_user_sender(fid)
            or fid == "system"
            or ag is None
            or (ag.get("status") or "") == "archived"
        ):
            exempt_senders.add(fid)

    # 本 turn 成功送达的收件人（inbox 落库 = send_message 成功的 DB 证据）
    # turn_started_ms 只用于 reply 窗口扫描，不用于 mid-turn 并入/ACK
    # （mid-turn 到达本 turn 未展示，不得进义务或 ACK —— 见上方 pending_msgs 范围）。
    sent_to: set[str] = set()
    replied_contracts: set[str] = set()
    turn_started_ms = int((agent.current_job or {}).get("started_at") or 0)
    if turn_started_ms:
        try:
            # TEST10 修复: 判定窗口从「本 turn 开始后」扩展到「最老待回复
            # 消息到达之后」。此前 sent_to / replied_contracts 都只扫本
            # turn —— agent 在上一 turn 回复了 ask（无论是否带 reply_to）
            # 都不算数，合约/回复义务跨 turn 永远关闭不了 → gate 死锁。
            reply_window_ms = turn_started_ms
            expect_ts = [
                m.get("created_at")
                for m in pending_msgs
                if m.get("expect_report")
            ]
            expect_ts = [
                t for t in expect_ts if isinstance(t, (int, float)) and t
            ]
            if expect_ts:
                reply_window_ms = min(
                    reply_window_ms,
                    cast(int, min([cast(int, t) for t in expect_ts])),
                )
            sent_to = await agent._inbox.get_sent_recipients_since(
                agent.id, reply_window_ms
            )
            import time as _time

            since_30min = int(_time.time() * 1000) - 30 * 60 * 1000
            sent_30min = await agent._inbox.get_sent_recipients_since(
                agent.id, since_30min
            )
            sent_to = set(sent_to) | set(sent_30min)
            replied_contracts = await agent._inbox.get_replied_contracts_since(
                agent.id, reply_window_ms
            )
        except Exception as e:
            log.debug("reply_gate_sent_lookup_failed", error=str(e))

    unreplied_asks = collect_unreplied_asks(
        pending_msgs,
        tool_calls,
        name_by_id,
        extra_replied_to=sent_to,
        exempt_senders=exempt_senders,
        replied_contracts=replied_contracts,
    )

    # TEST11 #1a: evidence for WAIT_WITHOUT_ASK
    outbound_ask_refs: set[str] = set()
    try:
        outbound_ask_refs = await agent._inbox.get_outstanding_ask_recipients(
            agent.id
        )
    except Exception as e:
        log.debug("outbound_ask_refs_failed", error=str(e))
    # Enrich messaged_refs with display names for ref matching
    messaged_refs = set(sent_to)
    for aid in list(sent_to):
        if aid in name_by_id:
            messaged_refs.add(name_by_id[aid])
    for aid in list(outbound_ask_refs):
        if aid not in name_by_id:
            try:
                ag = await meta_db.get_agent_by_id(aid)
                if ag and ag.get("name"):
                    name_by_id[aid] = ag["name"]
            except Exception:
                pass
        if aid in name_by_id:
            outbound_ask_refs.add(name_by_id[aid])

    # ── P1 escape valve(TEST10): 连续 N 次被 UNREPLIED_ASKS 阻塞后强制降级 ──
    # 防止 ask 合约因 LLM 不理解 reply_to 参数而永久死锁。
    _ESCAPE_VALVE_THRESHOLD = 5
    if unreplied_asks:
        agent._unreplied_asks_streak += 1
        # TEST10 修复: streak 此前只存在内存、随 run 结束清零，跨 run 永远
        # 到不了阈值。这里叠加 DB 证据（近 30 分钟 commit_turn 被
        # UNREPLIED_ASKS 拒绝的累计次数），实现跨 run 累计。
        db_streak = await agent._count_recent_ask_gate_rejections()
        effective_streak = max(agent._unreplied_asks_streak, db_streak)
        if effective_streak >= _ESCAPE_VALVE_THRESHOLD:
            # Close reply contracts (not just mark_read) — P1a pre_check
            # is contract-based; mark_read alone left commit_turn hard-rejecting.
            force_ids = [m["id"] for m in unreplied_asks if m.get("id")]
            waive_items = [
                {
                    "contract_id": m.get("reply_contract_id"),
                    "to_agent_id": m.get("from_agent_id"),
                }
                for m in unreplied_asks
                if m.get("reply_contract_id") and m.get("from_agent_id")
            ]
            waived = 0
            if waive_items:
                try:
                    waived = await agent._inbox.waive_reply_contracts(
                        agent.id,
                        waive_items,
                        reason="escape_valve",
                    )
                except Exception as e:
                    log.debug("escape_valve_waive_failed", error=str(e))
            if force_ids:
                try:
                    await agent._inbox.mark_read_by_ids(agent.id, force_ids)
                except Exception:
                    pass
            log.warning(
                "unreplied_asks_escape_valve",
                agent_id=agent.id,
                streak=effective_streak,
                db_streak=db_streak,
                force_closed=len(force_ids),
                contracts_waived=waived,
                senders=[m.get("from_name", "?") for m in unreplied_asks[:5]],
            )
            senders = [
                m.get("from_name") or m.get("from_agent_id", "?")[:12]
                for m in unreplied_asks[:5]
            ]
            await agent._persist_gate_notice(
                "REPLY CONTRACT ESCAPE VALVE",
                (
                    f"连续 {effective_streak} 次被 UNREPLIED_ASKS 阻塞后，"
                    f"平台已强制关闭 {waived} 个 reply contract / "
                    f"{len(force_ids)} 条消息。"
                    f"发件人: {', '.join(senders) or '(none)'}。"
                ),
                footer=(
                    "这不是你成功回复了——是平台防止死锁的降级。"
                    "下次收到 ask/expect_report 请用 send_message/ask_agent "
                    "正确指向发件人后再 commit_turn。"
                ),
            )
            unreplied_asks = []
            agent._unreplied_asks_streak = 0
    else:
        agent._unreplied_asks_streak = 0

    open_obligations: list[dict] = []
    try:
        from hiveweave.services.task import TaskService

        open_obligations = await TaskService().get_actionable_obligations(
            agent.project_id, agent.id
        )
    except Exception as e:
        log.warning(
            "turn_exit_obligations_failed",
            agent_id=agent.id,
            error=str(e),
        )

    tasks_advanced = agent._task_ids_advanced_this_turn(tool_calls)
    worktree_uncommitted = False
    try:
        from hiveweave.services.turn_exit import agent_worktree_has_uncommitted

        worktree_uncommitted = await agent_worktree_has_uncommitted(
            agent.id, agent.project_id
        )
    except Exception as e:
        log.debug("turn_exit_worktree_check_failed", error=str(e))

    exit_decision = evaluate_turn_exit(
        ExitContext(
            agent_id=agent.id,
            project_id=agent.project_id,
            tool_calls=tool_calls,
            pending_inbox_msgs=pending_msgs,
            unreplied_asks=unreplied_asks,
            open_task_obligations=open_obligations,
            tasks_advanced=tasks_advanced,
            messaged_refs=messaged_refs,
            outbound_ask_refs=outbound_ask_refs,
            name_by_id=name_by_id,
            worktree_uncommitted=worktree_uncommitted,
        )
    )

    gate_retrigger_hint: str | None = None
    continue_slice = False
    carry_inbox_ids: list[str] | None = None
    budget_exhausted = bool(result.get("budget_exhausted"))
    # NEW-1 / TEST18: phase is only set on exit_decision.ok; public tail
    # elif (phase == "in_progress") must not UnboundLocalError on park/exhaust.
    phase: str | None = None

    # Progress fingerprint for no-progress circuit breaker
    fp = agent._compute_progress_fingerprint(
        open_obligations, tool_calls, tasks_advanced
    )
    if agent._progress_fingerprint == fp:
        agent._no_progress_streak += 1
    else:
        agent._no_progress_streak = 0
        agent._progress_fingerprint = fp

    if not exit_decision.ok:
        unreplied_ids = {m["id"] for m in unreplied_asks}
        if agent.pending_inbox_msg_ids:
            no_reply_ids = [
                mid
                for mid in agent.pending_inbox_msg_ids
                if mid not in unreplied_ids
            ]
            if no_reply_ids:
                await agent._inbox.mark_read_by_ids(agent.id, no_reply_ids)

        if exit_decision.should_park or (
            "OPEN_TASKS_UNDECLARED" in exit_decision.violations
            and not exit_decision.should_repair
        ):
            # Ledger mismatch → park on real books, do not re-run LLM
            pop_pending_turn_result(agent.id)
            agent._turn_gate_count = 0
            agent.disposition = exit_decision.disposition or "runnable"
            if open_obligations:
                agent.disposition = "runnable"
            # Persist WHY for next wake (no auto-retrigger)
            await agent._persist_gate_notice(
                "TURN EXIT PARKED",
                exit_decision.hint
                or f"gates={exit_decision.violations}",
                footer=(
                    "系统已按真实账本停泊，本轮不再自动续跑。"
                    "下一外部事件到来时请按上述 GATE 推进，"
                    "或用 phase=waiting/blocked/in_progress 正确声明状态。"
                ),
            )
            log.warning(
                "turn_exit_parked",
                agent_id=agent.id,
                violations=exit_decision.violations,
                disposition=agent.disposition,
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.turn_exit_gate(
                    agent.id,
                    exit_decision.violations,
                    "park",
                    gate_round=agent._turn_gate_count,
                )
            except Exception:
                pass
            if agent.pending_inbox_msg_ids and not unreplied_asks:
                await agent._inbox.mark_read_by_ids(
                    agent.id, agent.pending_inbox_msg_ids
                )
            agent.pending_inbox_msg_ids = None
        elif (
            exit_decision.should_repair
            and agent._turn_gate_count < agent._TURN_GATE_MAX
        ):
            agent._turn_gate_count += 1
            gate_retrigger_hint = exit_decision.hint
            carry_inbox_ids = list(agent.pending_inbox_msg_ids or [])
            # Keep unreplied ask ids for the repair turn
            if unreplied_asks:
                carry_inbox_ids = list(
                    {*(carry_inbox_ids or []), *(m["id"] for m in unreplied_asks)}
                )
            # FIX(dup-hint): 不再直接 append_turn — retrigger_for_turn_gate
            # 调用 chat(hint) 时 hint 会作为 user 消息正常保存。之前这里
            # 额外 append 了一次，导致同一条 [TURN EXIT BLOCKED] 在
            # conversation_turns 中出现两份（一份来自这里，一份来自 chat()）。
            log.info(
                "turn_exit_repair",
                agent_id=agent.id,
                violations=exit_decision.violations,
                gate_round=agent._turn_gate_count,
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.turn_exit_gate(
                    agent.id,
                    exit_decision.violations,
                    "repair",
                    gate_round=agent._turn_gate_count,
                )
            except Exception:
                pass
            # Do not clear pending_inbox_msg_ids yet — carried into opts
        else:
            if unreplied_asks:
                await agent._escalate_unreplied(unreplied_asks)
            if agent.pending_inbox_msg_ids:
                await agent._inbox.mark_read_by_ids(
                    agent.id, agent.pending_inbox_msg_ids
                )
            pop_pending_turn_result(agent.id)
            agent._turn_gate_count = 0
            agent._reply_reminder_count = 0
            agent.disposition = "blocked"
            await agent._persist_gate_notice(
                "TURN EXIT BLOCKED — GATE EXHAUSTED",
                exit_decision.hint
                or f"gates={exit_decision.violations}",
                footer=(
                    "修复次数已用尽，disposition=blocked。"
                    "上级可能已收到升级。下次唤醒时请先处理上述 GATE，"
                    "再 commit_turn。"
                ),
            )
            log.warning(
                "turn_exit_gate_exhausted",
                agent_id=agent.id,
                violations=exit_decision.violations,
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.turn_exit_gate(
                    agent.id,
                    exit_decision.violations,
                    "exhausted",
                    gate_round=agent._turn_gate_count,
                )
            except Exception:
                pass
            agent.pending_inbox_msg_ids = None
    else:
        # ACK only what this turn latched/showed — never mid-turn arrivals.
        ack_ids: list[str] = list(agent.pending_inbox_msg_ids or [])
        if ack_ids:
            await agent._inbox.mark_read_by_ids(agent.id, ack_ids)
        agent.pending_inbox_msg_ids = None
        agent._turn_gate_count = 0
        agent._reply_reminder_count = 0
        # Do NOT reset _task_reminder_count here — that would defeat the
        # agent.turn.after nudge cap and allow infinite [TASK ADVANCE] loops.
        pop_pending_turn_result(agent.id)
        agent.disposition = exit_decision.disposition or "runnable"

        # Empty done_slice/waiting streak — consecutive hollow exits park hard (TEST4)
        # Also covers phase="waiting": CEO repeatedly get_tasks→commit_turn(waiting)
        # with no substantive work should be detected as empty, not just done_slice.
        phase = (
            exit_decision.turn_result.phase
            if exit_decision.turn_result
            else None
        )
        if phase in ("done_slice", "waiting") and agent._is_empty_done_slice_turn(
            tool_calls
        ):
            agent._empty_done_slice_streak += 1
        else:
            agent._empty_done_slice_streak = 0

        # P1: persist / clear Wait Contracts from accepted TurnResult
        try:
            from hiveweave.services.wait_contract import wait_contract_service

            tr = exit_decision.turn_result
            if tr and tr.phase in ("waiting", "blocked") and tr.waiting_on:
                await wait_contract_service.replace_waits(
                    agent.project_id,
                    agent.id,
                    tr.waiting_on,
                    phase=tr.phase,
                    obligations=open_obligations,
                )
            else:
                await wait_contract_service.clear_waits(
                    agent.project_id, agent.id
                )
        except Exception as e:
            log.warning(
                "wait_contract_persist_failed",
                agent_id=agent.id,
                error=str(e),
            )

        # No-progress fault
        if agent._no_progress_streak >= 2 and open_obligations:
            agent.disposition = "blocked"
            log.warning(
                "faulted_no_progress",
                agent_id=agent.id,
                streak=agent._no_progress_streak,
                fingerprint=fp[:16] if fp else None,
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.agent_no_progress(
                    agent.id, streak=agent._no_progress_streak
                )
            except Exception:
                pass
        elif agent._empty_done_slice_streak >= 2:
            # Two hollow done_slices → stay complete, no auto-resume
            agent.disposition = "complete"
            continue_slice = False
            log.info(
                "empty_done_slice_parked",
                agent_id=agent.id,
                streak=agent._empty_done_slice_streak,
            )
        else:
            # At most one more slice if obligations remain AND fingerprint moved
            # and phase was in_progress (declaration only — scheduler decides)
            if (
                phase == "in_progress"
                and open_obligations
                and agent._no_progress_streak == 0
                and agent._slice_budget > 0
            ):
                continue_slice = True
                agent._slice_budget -= 1

        log.info(
            "turn_exit_ok",
            agent_id=agent.id,
            phase=phase,
            disposition=agent.disposition,
            continue_slice=continue_slice,
            slice_budget=agent._slice_budget,
            empty_done_slice_streak=agent._empty_done_slice_streak,
        )
        try:
            from hiveweave.services.telemetry import telemetry

            telemetry.turn_exit_gate(
                agent.id,
                [],
                "ok",
                gate_round=agent._turn_gate_count,
            )
        except Exception:
            pass

    # 成功完成 → 清除 resume 冷却 + 重置连续错误计数 + 解除 give-up latch
    agent._resume_cooldown_until = 0.0
    agent._consecutive_errors = 0
    agent._stream_timeout_streak = 0
    agent._rate_limit_streak = 0
    agent._clear_resume_suppressed(reason="turn_ok")
    # TEST21 M5: recovered → resume task-stall nudges
    try:
        from hiveweave.services.task import TaskService

        await TaskService().clear_owner_parked_for_agent(
            agent.project_id, agent.id
        )
    except Exception as e:
        log.debug("clear_owner_parked_on_ok_failed", error=str(e))

    # 3.5 持久化裁剪旧工具输出（OpenCode prune 模式）
    try:
        await agent._conversation.prune_persisted(agent.id, agent.project_id)
    except Exception as e:
        log.warning("prune_persisted_failed", agent_id=agent.id, error=str(e))

    # 4. 状态 → idle (先取消 safety timer，再 reset；残留 streaming 再 finalize 一次)
    agent._cancel_safety_timer()
    await agent._go_idle()

    # 5. 发送 done 事件（前端 streamChat 等待此事件停止 loading）
    agent._broadcast_stream_event({
        "type": "done",
        "content": content,
        "agentId": agent.id,
        "disposition": agent.disposition,
    })

    # 5.5 广播健康事件 — 成功完成一轮 LLM 调用 → health="ok"
    agent._broadcast_agent_health("ok")

    # 6. Process queued user messages (sent while agent was busy)
    await agent._drain_message_queue()

    # 7. Lifecycle hook agent.turn.after (task-advance nudge, etc.)
    turn_after_hint: str | None = None
    try:
        from hiveweave.hooks import AGENT_TURN_AFTER, hooks

        hook_out: dict = {"hint": None, "skip_reason": None}
        from hiveweave.services.turn_session import is_task_advance_deferred

        await hooks.run(
            AGENT_TURN_AFTER,
            {
                "agent_id": agent.id,
                "project_id": agent.project_id,
                "tool_calls": tool_calls,
                "open_obligations": open_obligations,
                "tasks_advanced": list(tasks_advanced),
                "phase": (
                    exit_decision.turn_result.phase
                    if exit_decision.turn_result
                    else None
                ),
                "disposition": agent.disposition,
                "exit_ok": exit_decision.ok,
                "gate_repairing": bool(gate_retrigger_hint),
                "continue_slice": continue_slice,
                "deferred": is_task_advance_deferred(agent.id),
                "reminder_count": agent._task_reminder_count,
                "reminder_max": agent._TASK_REMINDER_MAX,
            },
            hook_out,
        )
        raw_hint = hook_out.get("hint")
        if isinstance(raw_hint, str) and raw_hint.strip():
            turn_after_hint = raw_hint.strip()
        elif hook_out.get("skip_reason") in (
            "no_obligations",
            "all_advanced",
            "deferred",
        ):
            agent._task_reminder_count = 0
    except Exception as e:
        log.warning(
            "agent_turn_after_hook_failed",
            agent_id=agent.id,
            error=str(e),
        )

    # 8. P0-3: Cross-turn stall break ledger — 2nd STALL BREAK parks + escalates
    # TEST21 M6: forgive breaks on turns with substantial progress; before
    # parking re-check recent successful run; escalation messages state
    # facts only (no reassign/dismiss prescription).
    _stall_parked = False
    if result.get("stall_break"):
        import time as _time

        now_ms = int(_time.time() * 1000)
        agent._last_stall_break_ms = now_ms
        had_progress = _turn_has_substantial_progress(
            tool_calls, tasks_advanced
        )
        if had_progress:
            log.info(
                "stall_break_forgiven",
                agent_id=agent.id,
                reason="substantial_progress",
            )
            try:
                from hiveweave.services.telemetry import telemetry

                telemetry.stall_break_forgiven(agent.id, reason="progress")
            except Exception:
                pass
        else:
            breaks = _stall_break_ledger.setdefault(agent.id, [])
            # Prune old entries outside the window
            breaks[:] = [
                t for t in breaks if now_ms - t < STALL_BREAK_WINDOW_MS
            ]
            breaks.append(now_ms)
            if len(breaks) >= STALL_BREAK_PARK_THRESHOLD:
                recent_ok = await _recent_successful_run_ms(
                    agent.id, exclude_after_ms=now_ms
                )
                recent_ok_alive = (
                    recent_ok is not None
                    and now_ms - recent_ok < STALL_BREAK_RECENT_OK_MS
                )
                if recent_ok_alive:
                    log.info(
                        "stall_break_park_deferred",
                        agent_id=agent.id,
                        reason="recent_successful_run",
                        recent_ok_ms=recent_ok,
                        break_count=len(breaks),
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        telemetry.stall_break_forgiven(
                            agent.id, reason="recent_ok_run"
                        )
                    except Exception:
                        pass
                    # Drop the just-appended break so we don't immediately
                    # re-trip on the next empty spin after a real run.
                    if breaks:
                        breaks.pop()
                else:
                    _stall_parked = True
                    agent.disposition = "blocked"
                    log.warning(
                        "stall_break_parked",
                        agent_id=agent.id,
                        break_count=len(breaks),
                        window_ms=STALL_BREAK_WINDOW_MS,
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        telemetry.stall_break_parked(agent.id)
                    except Exception:
                        pass
                    # TEST21 M5: mute task-stall while parked
                    try:
                        from hiveweave.services.task import TaskService

                        ts = TaskService()
                        open_ids = [
                            str(t["id"])
                            for t in (
                                await ts.list_tasks(
                                    agent.project_id, assignee_id=agent.id
                                )
                                or []
                            )
                            if t.get("status")
                            in (
                                "created",
                                "claimed",
                                "running",
                                "submitted",
                                "reviewing",
                                "rework",
                                "blocked",
                                "approved",
                            )
                            and t.get("id")
                        ]
                        if open_ids:
                            await ts.set_owner_parked(
                                agent.project_id, open_ids, parked=True
                            )
                    except Exception as e:
                        log.debug(
                            "stall_break_owner_parked_failed",
                            error=str(e),
                        )
                    # Escalate to org parent — facts only, no prescription
                    try:
                        from hiveweave.db import meta as _meta_db
                        from hiveweave.services.inbox import InboxService

                        agent_row = await _meta_db.get_agent_by_id(agent.id)
                        parent_id = (agent_row or {}).get("parent_id")
                        if parent_id:
                            agent_name = (
                                (agent_row or {}).get("name") or agent.id[:12]
                            )
                            recent_note = (
                                f"last successful run at {recent_ok}"
                                if recent_ok
                                else "no recent successful run on record"
                            )
                            pending_ids: list[str] = []
                            try:
                                from hiveweave.services.task import TaskService

                                for t in (
                                    await TaskService().list_tasks(
                                        agent.project_id,
                                        assignee_id=agent.id,
                                    )
                                    or []
                                ):
                                    if t.get("status") in (
                                        "created",
                                        "claimed",
                                        "running",
                                        "submitted",
                                        "reviewing",
                                        "rework",
                                        "blocked",
                                    ):
                                        pending_ids.append(
                                            str(t.get("id") or "")[:8]
                                        )
                            except Exception:
                                pass
                            task_blob = (
                                ", ".join(pending_ids[:12]) or "(none)"
                            )
                            await InboxService().send_message(
                                from_agent_id="system",
                                to_agent_id=parent_id,
                                message=(
                                    f"[AGENT STUCK] {agent_name} "
                                    f"({agent.id[:12]}) hit STALL BREAK "
                                    f"{len(breaks)} times in "
                                    f"{STALL_BREAK_WINDOW_MS // 60000}min. "
                                    f"Agent parked (disposition=blocked). "
                                    f"{recent_note}. "
                                    f"Open tasks: {task_blob}. "
                                    f"Assess activity before acting — "
                                    f"signal may be stale."
                                ),
                                message_type="escalation",
                                priority="urgent",
                                wake=True,
                            )
                            from hiveweave.agents.trigger import (
                                trigger_subordinate,
                            )

                            await trigger_subordinate(parent_id)
                    except Exception as e:
                        log.warning(
                            "stall_break_escalate_failed",
                            agent_id=agent.id,
                            error=str(e),
                        )
                    # Clear the ledger after parking
                    _stall_break_ledger.pop(agent.id, None)

    # 9. Repair once OR one progress slice OR hook nudge — never unlimited
    if _stall_parked:
        pass  # Parked — do not retrigger
    elif gate_retrigger_hint:
        await agent._retrigger_for_turn_gate(
            gate_retrigger_hint, inbox_msg_ids=carry_inbox_ids
        )
    elif budget_exhausted:
        await agent._retrigger_for_turn_gate(
            "[TURN BUDGET] Previous slice hit the turn budget after "
            "productive work. commit_turn(phase='in_progress') if needed, "
            "then continue from where you left off.",
            inbox_msg_ids=None,
        )
    elif continue_slice:
        await agent._retrigger_for_turn_gate(
            "[TURN CONTINUE] You still have actionable obligations and made "
            "progress this slice. Continue once more, then commit_turn "
            "(prefer waiting/done_slice when idle on the user).",
            inbox_msg_ids=None,
        )
    elif (
        phase == "in_progress"
        and open_obligations
        and agent._no_progress_streak == 0
        and bool(tool_calls)
    ):
        # TEST6 evening P2-5: slice budget spent but still productive —
        # arm deferred wake (scheduler), do not burn same-turn slices.
        agent._arm_productive_continue()
    elif turn_after_hint:
        agent._task_reminder_count += 1
        await agent._retrigger_for_open_tasks(turn_after_hint)
    else:
        await agent._maybe_self_retrigger()
