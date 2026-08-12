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
    broadcast_project_rate_limit,
    is_account_rate_limit,
    is_rate_limit_error,
)

log = structlog.get_logger(__name__)


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

async def handle_error(agent: Any, error: Exception) -> None:
    """错误处理。

    对齐 Elixir agent.ex:644 handle_info({:DOWN, ref, :process, ...})。

    BUG-032 修复: 先广播 error 事件再 reset_to_idle。之前顺序导致
    前端先收到 status→idle 再收到 error，错误信息可能因 streamDraft
    已被清理而无法展示。参考 OpenCode SSE 错误模式：错误事件作为终止
    信号先于状态变更到达。
    """
    error_msg = str(error)
    error_type = type(error).__name__

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

    # 连续错误计数 — 超过阈值后 ACK inbox，不再 resume
    # 429 / rate-limit: 分级 — soft 冷却 / hard 配额 park（TEST20 P0-B）
    if is_rate_limit_error(error):
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
      「10min 超时 → 90s 冷却 → 再超时」的无限死循环，且不再注入
      CHECKPOINT 撑大上下文让下一轮更易超时
    - 记录 work_log，便于监控/stall watchdog 关联
    """
    inbox_ids = list(agent.pending_inbox_msg_ids or [])

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
        "[TIMEOUT] LLM call exceeded 10 minute safety limit. "
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

    # 广播健康事件 — LLM 调用 10 分钟安全超时 → health="error"
    agent._broadcast_agent_health(
        "error", "LLM call exceeded 10 minute safety limit"
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
