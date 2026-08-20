"""Inbox watcher loop and revival.

Extracted from agent.py — behavior-preserving mechanical split (P1).
Module-level functions take ``agent`` as first arg; Agent methods are thin wrappers.

MUST NOT top-level import hiveweave.agents.trigger — lazy import inside functions only.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from hiveweave.agents.types import AgentState

log = structlog.get_logger(__name__)


async def inbox_watcher_loop(agent: Any) -> None:
    """BUG-010 修复：后台轮询 inbox，未读时触发 trigger_subordinate。

    间隔 5s（与前端的 chat polling 节流一致），避免过度空转。
    只在 idle 状态触发；processing 状态由 trigger 自己 skip。

    BUG-010 增强：如果 trigger 返回后 agent 仍 idle 且仍有 pending
    inbox 消息，说明 trigger 静默跳过了（e.g. auto-start 失败），
    使用指数退避重试 [5s, 15s, 45s]。
    """
    INTERVAL_S = 5.0
    RETRY_DELAYS = [5.0, 15.0, 45.0]  # 指数退避（秒）
    # TEST6 P1-1: after N ineffective triggers, ACK remaining pending
    # instead of infinite 45s busy-wait (watcher/trigger口径分裂时必触)。
    TRIGGER_FAIL_FUSE = 5
    trigger_fail_count = 0
    # 启动后等 1s 再开始（避开与 trigger.py 的 100ms 起步冲突）
    await asyncio.sleep(1.0)
    while not agent._stop_watcher:
        try:
            # 仅当明确 is_started=0 时跳过；查不到项目则 fail-open 继续轮询
            try:
                from hiveweave.services.project_lifecycle import (
                    project_known_off_duty,
                )

                if await project_known_off_duty(agent.project_id):
                    await asyncio.sleep(INTERVAL_S)
                    continue
            except Exception:
                pass
            if agent.status == AgentState.IDLE:
                if agent._resume_suppressed:
                    latch_opts: dict = {"trigger": True}
                    try:
                        from hiveweave.agents.trigger import wake_source_for_pending

                        peek = await agent._inbox.get_pending_messages(agent.id)
                        src = await wake_source_for_pending(
                            peek,
                            project_id=getattr(agent, "project_id", None),
                            waiter_agent_id=agent.id,
                        )
                        if src != "trigger":
                            latch_opts["source"] = src
                            if src == "task":
                                latch_opts["message_type"] = "task"
                            if src == "wait_satisfied":
                                latch_opts["clear_waits"] = False
                    except Exception:
                        pass
                    # 被动衰减检查：30min 过期则清除锁存器，落入正常处理
                    if agent.try_clear_resume_suppressed(opts=latch_opts):
                        log.debug(
                            "inbox_watcher_suppressed_skip",
                            agent_id=agent.id,
                        )
                        await asyncio.sleep(INTERVAL_S)
                        continue
                if agent._in_resume_cooldown():
                    log.debug(
                        "inbox_watcher_cooldown_skip",
                        agent_id=agent.id,
                        cooldown_remaining_s=round(
                            agent._resume_cooldown_until - time.monotonic(), 1
                        ),
                    )
                    await asyncio.sleep(INTERVAL_S)
                    continue
                from hiveweave.services.inbox import filter_actionable_pending

                pending_raw = await agent._inbox.get_pending_messages(agent.id)
                pending = filter_actionable_pending(pending_raw)
                fyi_only = [
                    m for m in (pending_raw or [])
                    if m.get("id")
                    and m["id"] not in {p.get("id") for p in pending}
                ]
                # Only FYI task_event left → ACK and skip (match trigger口径)
                if fyi_only and not pending:
                    try:
                        await agent._inbox.mark_read_by_ids(
                            agent.id,
                            [str(m["id"]) for m in fyi_only],
                        )
                        log.info(
                            "inbox_watcher_acked_fyi_task_events",
                            agent_id=agent.id,
                            count=len(fyi_only),
                        )
                    except Exception as e:
                        log.warning(
                            "inbox_watcher_ack_fyi_failed",
                            agent_id=agent.id,
                            error=str(e),
                        )
                    await asyncio.sleep(INTERVAL_S)
                    continue
                if pending:
                    log.info(
                        "inbox_watcher_found_pending",
                        agent_id=agent.id,
                        count=len(pending),
                        trigger_fail_count=trigger_fail_count,
                    )
                    # 延迟导入避免循环
                    from hiveweave.agents.trigger import (
                        is_coordinator,
                        trigger_coordinator,
                        trigger_subordinate,
                    )
                    role = agent.config.get("role", "")
                    if is_coordinator(role):
                        await trigger_coordinator(agent.id)
                    else:
                        await trigger_subordinate(agent.id)

                    # BUG-010 增强：短暂等待后检查 trigger 是否真的
                    # 启动了处理。如果 idle 且仍有 pending，说明 trigger
                    # 静默跳过（e.g. agent 不在 manager 中且 auto-start 失败）。
                    await asyncio.sleep(2.0)
                    still_raw = await agent._inbox.get_pending_messages(agent.id)
                    still_pending = filter_actionable_pending(still_raw)
                    still_fyi = [
                        m for m in (still_raw or [])
                        if m.get("id")
                        and m["id"] not in {p.get("id") for p in still_pending}
                    ]
                    if still_fyi and not still_pending:
                        try:
                            await agent._inbox.mark_read_by_ids(
                                agent.id,
                                [str(m["id"]) for m in still_fyi],
                            )
                        except Exception:
                            pass
                        trigger_fail_count = 0
                    elif still_pending and agent.status == AgentState.IDLE:
                        trigger_fail_count += 1
                        if trigger_fail_count >= TRIGGER_FAIL_FUSE:
                            # NEVER ACK actionable pending (user_message /
                            # ask / expect_report / review). Silent mark_read
                            # was starving obligations worse than busy-wait.
                            # Escalate + red-box; keep unread; long backoff.
                            log.error(
                                "inbox_watcher_trigger_fuse_escalated",
                                agent_id=agent.id,
                                pending_count=len(still_pending),
                                trigger_fail_count=trigger_fail_count,
                                pending_types=[
                                    (m.get("message_type") or "?")
                                    for m in still_pending[:8]
                                ],
                            )
                            try:
                                agent._broadcast_agent_health(
                                    "error",
                                    "trigger_fuse: pending inbox not "
                                    "consumed after repeated wake failures",
                                )
                            except Exception:
                                pass
                            try:
                                await agent._escalate_trigger_fuse(
                                    still_pending
                                )
                            except Exception as e:
                                log.warning(
                                    "inbox_watcher_fuse_escalate_failed",
                                    agent_id=agent.id,
                                    error=str(e),
                                )
                            trigger_fail_count = 0
                            await asyncio.sleep(INTERVAL_S * 6)
                            continue
                        delay = (
                            RETRY_DELAYS[min(trigger_fail_count - 1, len(RETRY_DELAYS) - 1)]
                            if trigger_fail_count <= len(RETRY_DELAYS)
                            else RETRY_DELAYS[-1]
                        )
                        log.warning(
                            "inbox_watcher_trigger_ineffective",
                            agent_id=agent.id,
                            pending_count=len(still_pending),
                            trigger_fail_count=trigger_fail_count,
                            retry_delay_s=delay,
                        )
                        # 退避重试 — 不 sleep interval，用退避延迟
                        try:
                            await asyncio.sleep(delay)
                        except asyncio.CancelledError:
                            break
                        continue
                    else:
                        # trigger 成功，重置失败计数
                        trigger_fail_count = 0
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(
                "inbox_watcher_error",
                agent_id=agent.id,
                error=str(e),
            )
        # 用 sleep 替代固定 wait，便于 cancel
        try:
            await asyncio.sleep(INTERVAL_S)
        except asyncio.CancelledError:
            break


def ensure_watcher_alive(agent: Any) -> None:
    """确保 inbox watcher 存活；被 cancel() 杀掉后复活它。

    cancel() 置 _stop_watcher=True 并取消 watcher 协程，但 Agent 对象
    仍留在 manager 中可被再次激活（chat/trigger；deactivate→activate
    时 supervisor 跳过已存在实例）。不复活的话 watcher 永久死亡，
    agent 读不到同伴消息（"失联"），直到进程重启。

    幂等：watcher 存活时直接返回，不重复启动。同步方法（检查与赋值
    之间无 await），与 cancel() 在同一事件循环内不会交错；
    cancel() 置标志 + cancel task 之间同样无 await，即时停止语义不变。
    """
    if not hasattr(agent, "_stop_watcher"):
        # object.__new__(Agent) 裸实例（测试 double，未走 __init__）—
        # watcher 不在其生命周期内，跳过
        return
    task = agent._inbox_watcher_task
    if task is not None and not task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有 running loop（e.g. 测试场景）— 跳过，下次激活再试
        return
    agent._stop_watcher = False
    agent._inbox_watcher_task = loop.create_task(
        agent._inbox_watcher_loop(),
        name=f"agent-{agent.id}-inbox-watcher",
    )
    log.info("inbox_watcher_started", agent_id=agent.id)
