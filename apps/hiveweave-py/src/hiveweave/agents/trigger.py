"""Trigger functions — trigger_subordinate / trigger_coordinator + build_trigger_context.

契约 04: 多 Agent 编排 (trigger 部分)
- trigger_subordinate(agent_id): 触发下属 executor 处理待处理内容
- trigger_coordinator(agent_id): 触发 coordinator（仅当有未读消息时）
- build_trigger_context(agent, trigger_type): 构建触发上下文消息
  - Pending Tasks block（handoffs）
  - Rework block（被拒绝的工作）
  - Messages block（inbox 消息）
  - Subordinate Logs block（coordinator 专属）
  - Report Required block（coordinator 专属，unreported handoffs）

移植自 Elixir agent.ex: trigger_subordinate/1, trigger_coordinator/1,
build_trigger_context/2, run_triggered_agent/2, do_trigger/2。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog


from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService
from hiveweave.services.org import OrgService

if TYPE_CHECKING:
    from hiveweave.agents.agent import Agent

log = structlog.get_logger(__name__)

# ── 常量（契约 04）──────────────────────────────────────────

TRIGGER_DELAY_MS = 100
"""触发前延迟，等 DB 写入落盘（workaround，对齐 Elixir agent.ex:179）。"""

CHAT_CALL_TIMEOUT_MS = 30_000
"""trigger 调用 chat 的超时（对齐 Elixir agent.ex:264 GenServer.call 30_000）。"""

SELF_RETRIGGER_DELAY_MS = 500
"""自检 retrigger 前的延迟（对齐 Elixir agent.ex:900 Process.sleep(500)）。"""

# Background wake=0 messages that must still fire coordinator review (TEST3).
_TASK_GATE_PREFIXES = (
    "[TASK SUBMITTED]",
    "[REWORK REQUESTED]",
    "[TASK APPROVED]",
    "[POST-MERGE VERIFY]",
    "[SHIP READY]",
)


def _has_task_gate_messages(messages: list[dict] | None) -> bool:
    """True if any message is a task-ledger gate that needs a coordinator turn."""
    for m in messages or []:
        text = (m.get("message") or "").lstrip()
        if any(text.startswith(p) for p in _TASK_GATE_PREFIXES):
            return True
        if (m.get("message_type") or "").lower() == "task" and m.get("task_id"):
            return True
    return False

# ── 模块级服务实例 ──────────────────────────────────────────

_org_service = OrgService()
_inbox_service = InboxService()
_handoff_service = HandoffService()
# Dedup: track last goals version shown to each agent via chat_message.
# Prevents back-to-back triggers from saving the same Goals Workbook block twice.
_last_goals_msg_version: dict[str, int] = {}


# ── 辅助函数 ────────────────────────────────────────────────


async def _agent_name(agent_id: str) -> str:
    """获取 agent 花名（用于人类可读的上下文消息）。

    对齐 Elixir agent.ex:397 agent_name/1。
    """
    try:
        agent = await _org_service.get_agent(agent_id)
        if agent and agent.get("name"):
            return agent["name"]
    except Exception:
        pass
    return agent_id


def _merge_outstanding_digest_asks(
    pending: list[dict], outstanding: list[dict]
) -> list[dict]:
    """Unread pending plus open-contract asks (even if read=1).

    Dedup by ``reply_contract_id`` (fallback id). Does not mutate read flags;
    caller must ACK only the original pending ids.
    """
    seen_cids: set[str] = set()
    seen_ids: set[str] = set()
    merged: list[dict] = []
    for m in pending or []:
        merged.append(m)
        mid = m.get("id")
        if mid:
            seen_ids.add(str(mid))
        cid = m.get("reply_contract_id")
        if cid:
            seen_cids.add(str(cid))
    for m in outstanding or []:
        cid = m.get("reply_contract_id")
        mid = m.get("id")
        if cid and str(cid) in seen_cids:
            continue
        if mid and str(mid) in seen_ids:
            continue
        merged.append(m)
        if cid:
            seen_cids.add(str(cid))
        if mid:
            seen_ids.add(str(mid))
    merged.sort(key=lambda row: row.get("created_at") or 0)
    return merged


def _strip_goals_block(context: str) -> str:
    """Remove the Goals Workbook block from context to avoid duplicate display."""
    import re
    return re.sub(
        r'\n*## Goals Workbook \(updated\)\n\{[^}]*"from":\s*"[^"]*"[^}]*\}\n*',
        '', context
    ).strip()


async def _admit_trigger_wake(
    agent,
    *,
    wake_category: str | None,
    from_agent_id: str | None,
    inbox_msg_ids: list[str] | None,
) -> bool:
    """Always admit — category triage removed; any inbox may wake."""
    from hiveweave.services.wake_policy import admit_wake

    return admit_wake(
        disposition=getattr(agent, "disposition", None),
        from_agent_id=from_agent_id,
        recipient_parent_id=(getattr(agent, "config", None) or {}).get("parent_id"),
    ).ok


async def _delete_chat_message(agent_id: str, msg_id: str | None) -> None:
    if not msg_id:
        return
    try:
        from hiveweave.db import project as project_db

        await project_db.execute(
            agent_id,
            "DELETE FROM chat_messages WHERE id = ? AND agent_id = ?",
            [msg_id, agent_id],
        )
    except Exception as e:
        log.debug("trigger_digest_delete_failed", error=str(e))


def is_coordinator(role: str | None) -> bool:
    """判断角色是否为 coordinator 类型。

    对齐 Elixir agent.ex:886 coordinator?/1。
    """
    if not role:
        return False
    return role.lower() in ("ceo", "coordinator", "hr", "manager", "架构师", "经理")


# ── 公共 API ────────────────────────────────────────────────


def merge_queued_triggers(
    triggers: list[tuple[str, dict, int]],
) -> tuple[str, dict]:
    """Coalesce trigger wakes. Completions concatenate; others last-wins.

    ``wait_satisfied`` wakes use ``wake=0`` while PROCESSING, so the
    watcher never re-fetches. Last-wins would ACK the first
    ``[BASH|SUBAGENT DONE]`` unread. Union bodies + ``inbox_msg_ids``
    for those; keep last-wins for other trigger sources (ACK=seen).
    """
    if not triggers:
        return "", {}
    wait_sat = [
        item
        for item in triggers
        if (item[1] or {}).get("source") == "wait_satisfied"
    ]
    rest = [
        item
        for item in triggers
        if (item[1] or {}).get("source") != "wait_satisfied"
    ]
    if wait_sat:
        parts: list[str] = []
        ids: list[str] = []
        seen_ids: set[str] = set()
        opts = dict(wait_sat[-1][1] or {})
        for msg, extra, _ts in wait_sat:
            if msg:
                parts.append(str(msg))
            for raw in (extra or {}).get("inbox_msg_ids") or []:
                sid = str(raw)
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    ids.append(sid)
        if rest:
            last_m, last_o, _ts = rest[-1]
            if last_m:
                parts.append(str(last_m))
            for raw in (last_o or {}).get("inbox_msg_ids") or []:
                sid = str(raw)
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    ids.append(sid)
        opts["inbox_msg_ids"] = ids
        opts["source"] = "wait_satisfied"
        opts["clear_waits"] = False
        opts["merged_wakes"] = len(triggers)
        opts["trigger"] = True
        return "\n\n".join(parts), opts
    message = triggers[-1][0]
    opts = dict(triggers[-1][1] or {})
    opts["inbox_msg_ids"] = list(opts.get("inbox_msg_ids") or [])
    opts["merged_wakes"] = len(triggers)
    opts.setdefault("source", "merged_trigger")
    return message, opts


def _pending_from_ids(pending: list | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for msg in pending or []:
        if not isinstance(msg, dict):
            continue
        fid = str(msg.get("from_agent_id") or "").strip()
        if not fid:
            continue
        key = fid.lower()
        if key in seen:
            continue
        seen.add(key)
        ids.append(fid)
    return ids


async def _attach_wait_clear_senders(
    latch_opts: dict,
    pending: list | None,
    *,
    project_id: str | None,
    waiter_agent_id: str | None,
) -> None:
    """Record inbox senders that match a kind=agent wait (not first-row only)."""
    pid = (project_id or "").strip()
    waiter = (waiter_agent_id or "").strip()
    if not pid or not waiter:
        return
    try:
        from hiveweave.services.wait_contract import matching_sender_ids_for_waiter

        matched = await matching_sender_ids_for_waiter(
            pid, waiter, _pending_from_ids(pending)
        )
    except Exception:
        return
    if matched:
        latch_opts["wait_clear_sender_ids"] = matched


async def wake_source_for_pending(
    pending: list | None,
    *,
    project_id: str | None = None,
    waiter_agent_id: str | None = None,
) -> str:
    """Latch-exempt source for pending inbox, or ``trigger`` if none.

    ``message_type=offturn_completion`` is the trust gate for
    ``wait_satisfied``. Protocol prefixes
    ``[SUBAGENT DONE|FAILED]`` / ``[BASH DONE|FAILED]`` are the body
    contract, not a substitute for type. ``from=system`` plus prefix
    with type=system/normal is a normal ``trigger`` (or ``task`` if
    ``task_id`` is set). Peer + prefix without that type stays
    ``trigger``.

    A pending sender that matches an active kind=agent wait returns
    ``message_from_ref`` so admit clears only that wait.
    """
    from hiveweave.services.offturn import is_offturn_completion_text
    from hiveweave.services.wake_policy import OFFTURN_COMPLETION_MESSAGE_TYPE

    wait_ok = False
    task = False
    for msg in pending or []:
        if not isinstance(msg, dict):
            continue
        mt = (msg.get("message_type") or "").strip().lower()
        if mt == "task" or msg.get("task_id"):
            task = True
        text = msg.get("message")
        if mt == OFFTURN_COMPLETION_MESSAGE_TYPE and is_offturn_completion_text(
            text
        ):
            wait_ok = True
    if wait_ok:
        return "wait_satisfied"
    pid = (project_id or "").strip() or None
    waiter = (waiter_agent_id or "").strip() or None
    if pid and waiter:
        try:
            from hiveweave.services.wait_contract import (
                kind_agent_wait_matches_sender,
                wait_contract_service,
            )

            waits = await wait_contract_service.list_active(pid, waiter)
            for msg in pending or []:
                if not isinstance(msg, dict):
                    continue
                from_id = str(msg.get("from_agent_id") or "").strip()
                if not from_id:
                    continue
                if await kind_agent_wait_matches_sender(
                    pid, waits, from_agent_id=from_id
                ):
                    return "message_from_ref"
        except Exception:
            pass
    if task:
        return "task"
    return "trigger"


def _guard_sibling_offturn_waits(agent_id: str, latch_opts: dict) -> None:
    """Completions already cleared the matching wait ref — never wipe the rest."""
    if latch_opts.get("source") == "wait_satisfied":
        latch_opts["clear_waits"] = False


async def trigger_subordinate(agent_id: str, *, force: bool = False) -> None:
    """触发下属 executor 处理待处理内容。

    在 dispatch_task 或 rework 请求后调用。
    异步执行：延迟 100ms → 检查状态 → 构建上下文 → 调用 chat。

    对齐 Elixir agent.ex:157 trigger_subordinate/1。
    """
    await _do_trigger(agent_id, "subordinate", force=force)


async def trigger_coordinator(agent_id: str, *, force: bool = False) -> None:
    """触发 coordinator 处理待处理 inbox 消息。

    仅当 coordinator 有未读消息时才执行（避免浪费 token）。
    ``force=True`` 穿透未读守卫——看门狗穿透唤醒用（欠债必醒：
    inbox 全读但 ledger 有 creator/reviewer 义务或未解除回复契约）。

    对齐 Elixir agent.ex:168 trigger_coordinator/1。
    """
    await _do_trigger(agent_id, "coordinator", force=force)


# ── 内部实现 ────────────────────────────────────────────────


async def _do_trigger(agent_id: str, trigger_type: str, *,
                      force: bool = False) -> None:
    """触发 agent 的内部实现。

    流程（对齐 Elixir agent.ex:177 do_trigger/2）：
    1. 延迟 100ms（等 DB 写入落盘）
    2. 从 DB 获取 agent
    3. 如果 agent 已 archived → 跳过
    4. coordinator：检查是否有 pending inbox 消息，无则跳过
    5. 检查 agent 是否正在 processing → 跳过
    6. accept_pending_handoffs
    7. build_trigger_context
    8. 保存为 background user 消息
    9. 调用 chat
    """
    try:
        # 1. 延迟，等 DB 写入落盘
        await asyncio.sleep(TRIGGER_DELAY_MS / 1000.0)

        # 2. 从 DB 获取 agent
        agent_record = await _org_service.get_agent(agent_id)
        if not agent_record:
            log.warning("trigger_agent_not_found", agent_id=agent_id)
            return

        # 3. 如果 agent 已 archived → 跳过
        status = agent_record.get("status")
        if status in ("archived", "dismissed"):
            log.info("trigger_archived_skip", agent_id=agent_id, status=status)
            return

        project_id = agent_record["project_id"]

        # Bug K fix: 检查项目是否"上班"状态，未上班则跳过
        from hiveweave.db import meta as meta_db
        proj = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
        if not proj or not dict(proj).get("is_started"):
            log.info("trigger_project_not_started_skip",
                     agent_id=agent_id, project_id=project_id)
            return

        # 4. coordinator：检查是否有 pending inbox 消息
        # Also proceed when undelivered background holds task-gate notices
        # (historical wake=0 TASK SUBMITTED / REWORK — TEST3 Phase C starve).
        # force=True 穿透守卫（看门狗穿透唤醒：欠债必醒，inbox 全读也醒）。
        if trigger_type == "coordinator" and not force:
            from hiveweave.services.inbox import (
                filter_actionable_pending,
                is_fyi_task_event,
            )

            pending_raw = await _inbox_service.get_pending_messages(agent_id)
            # Filter out task_event notifications — these are FYI-only and
            # should NOT wake the coordinator (they cause busy-wait loops
            # where CEO does get_tasks → commit_turn(waiting) → repeat).
            # Only actionable messages (task submissions, rework, human chat,
            # agent-to-agent asks) should trigger a coordinator turn.
            pending = filter_actionable_pending(pending_raw)
            fyi_pending = [
                m for m in (pending_raw or []) if is_fyi_task_event(m)
            ]
            if not pending:
                # TEST6 P1-1: ACK scanned FYI so watcher stops re-triggering
                if fyi_pending:
                    try:
                        await _inbox_service.mark_read_by_ids(
                            agent_id,
                            [str(m["id"]) for m in fyi_pending if m.get("id")],
                        )
                        log.info(
                            "trigger_coordinator_acked_fyi_task_events",
                            agent_id=agent_id,
                            count=len(fyi_pending),
                        )
                    except Exception as e:
                        log.warning(
                            "trigger_coordinator_ack_fyi_failed",
                            agent_id=agent_id,
                            error=str(e),
                        )
                background = await _inbox_service.get_undelivered_background(
                    agent_id
                )
                # Also filter task_event from background check
                background = filter_actionable_pending(background)
                if not _has_task_gate_messages(background):
                    log.info(
                        "trigger_coordinator_no_messages",
                        agent_id=agent_id,
                    )
                    return
                log.info(
                    "trigger_coordinator_via_background_task_gate",
                    agent_id=agent_id,
                    background_count=len(background),
                )

        # 获取 agent task 实例
        manager = _get_agent_manager()
        agent = manager.get_agent(agent_id)
        if agent is None:
            # BUG-010 修复：agent 可能是 hire_agent API 刚创建但未
            # start 的（DB 有行但 agent_manager 没实例）。让 supervisor
            # 自动从 DB 加载并 start——下次 hire_agent 创建的 executor
            # 收到 inbox 时不会再静默。
            log.info("trigger_auto_start_begin",
                     agent_id=agent_id,
                     name=agent_record.get("name"))
            try:
                # BUG-032 修复: 通过 create_agent_callbacks 注入流式回调,
                # 确保 trigger 自动启动的 agent 也能向前端推送 stream_chunk
                # 和 status_change 事件。参考 DeepTutor StreamBus replay 模式
                # 和 OpenCode 的 SSE 统一事件流设计。
                from hiveweave.realtime.event_bus import create_agent_callbacks

                on_status, on_stream = create_agent_callbacks(
                    agent_id, agent_record["project_id"]
                )
                agent = await manager.start_agent(
                    agent_id, agent_record["project_id"], agent_record,
                    on_status_change=on_status,
                    on_stream_event=on_stream,
                )
                log.info(
                    "trigger_auto_started_agent",
                    agent_id=agent_id,
                    name=agent_record.get("name"),
                )
            except Exception as e:
                log.warning(
                    "trigger_no_agent_task",
                    agent_id=agent_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                return
        if agent is None:
                log.warning("trigger_no_agent_task", agent_id=agent_id)
                return

        # Give-up latch: task-class inbox / off-turn completion / 30min decay unlocks
        latch_opts: dict = {"trigger": True, "source": "trigger"}
        try:
            pending_for_latch = await _inbox_service.get_pending_messages(agent_id)
            src = await wake_source_for_pending(
                pending_for_latch,
                project_id=project_id,
                waiter_agent_id=agent_id,
            )
            if src != "trigger":
                latch_opts["source"] = src
                if src == "task":
                    latch_opts["message_type"] = "task"
            await _attach_wait_clear_senders(
                latch_opts,
                pending_for_latch,
                project_id=project_id,
                waiter_agent_id=agent_id,
            )
            _guard_sibling_offturn_waits(agent_id, latch_opts)
        except Exception:
            pass
        if getattr(agent, "try_clear_resume_suppressed", None):
            if agent.try_clear_resume_suppressed(latch_opts):
                log.info("trigger_suppressed_gave_up", agent_id=agent_id)
                return
        elif getattr(agent, "_resume_suppressed", False):
            log.info("trigger_suppressed_gave_up", agent_id=agent_id)
            return

        # BUG-032 修复: 防御性回调补丁。即使 agent 已在 agent_manager 中
        # (manager.get_agent 非空)，回调也可能缺失（例如通过某些冷启动路径）。
        # 参考 phoenix_adapter.py:481-487 和 DeepTutor StreamBus 的订阅保证。
        if getattr(agent, "_on_stream_event", None) is None:
            from hiveweave.realtime.event_bus import create_agent_callbacks

            on_status, on_stream = create_agent_callbacks(
                agent_id, agent_record["project_id"]
            )
            agent._on_status_change = on_status
            agent._on_stream_event = on_stream
            log.info("trigger_patch_agent_callbacks", agent_id=agent_id)

        # 5. If busy → enqueue wake (P1 single-flight) instead of drop
        if agent.status.value == "processing":
            await _handoff_service.accept_pending_handoffs(project_id, agent_id)
            result = await build_trigger_context(agent_record, trigger_type)
            if result is None:
                # Placeholder wake only — do NOT latch real inbox ids.
                # Hard invariant: never ACK messages the model did not see.
                # Unread rows stay for watcher / next real build_trigger_context.
                pending = await _inbox_service.get_pending_messages(agent_id)
                background = await _inbox_service.get_undelivered_background(
                    agent_id
                )
                pool = list(pending) + list(background)
                if not any(m.get("id") for m in pool):
                    log.info("trigger_busy_no_context", agent_id=agent_id)
                    return
                from hiveweave.services.inbox_triage import derive_wake_category

                wake_cat = derive_wake_category(pool)
                from_id = next(
                    (m.get("from_agent_id") for m in pool if m.get("from_agent_id")),
                    "system",
                )
                if not await _admit_trigger_wake(
                    agent,
                    wake_category=wake_cat,
                    from_agent_id=from_id,
                    inbox_msg_ids=[],
                ):
                    return
                await agent.enqueue_wake(
                    "[Inbox triage pending — recheck when idle]",
                    opts={
                        "trigger": True,
                        "from_agent_id": from_id,
                        "inbox_msg_ids": [],
                        "wake_category": wake_cat,
                        "source": latch_opts.get("source") or "trigger_busy_queue",
                        "message_type": latch_opts.get("message_type"),
                        "task_id": latch_opts.get("task_id"),
                        "clear_waits": latch_opts.get("clear_waits"),
                        "wait_clear_sender_ids": latch_opts.get(
                            "wait_clear_sender_ids"
                        ),
                        "is_background": True,
                    },
                )
                log.info(
                    "trigger_busy_enqueued_triage_pending",
                    agent_id=agent_id,
                    inbox_pending=0,
                    pool_unread=len(pool),
                    wake_category=wake_cat,
                )
                return
            context, inbox_msg_ids, from_agent_id, wake_category = result
            if not await _admit_trigger_wake(
                agent,
                wake_category=wake_category,
                from_agent_id=from_agent_id,
                inbox_msg_ids=inbox_msg_ids,
            ):
                return
            await agent.enqueue_wake(
                context,
                opts={
                    "trigger": True,
                    "from_agent_id": from_agent_id,
                    "inbox_msg_ids": inbox_msg_ids,
                    "wake_category": wake_category,
                    "source": latch_opts.get("source") or "trigger_busy_queue",
                    "message_type": latch_opts.get("message_type"),
                    "task_id": latch_opts.get("task_id"),
                    "clear_waits": latch_opts.get("clear_waits"),
                    "wait_clear_sender_ids": latch_opts.get(
                        "wait_clear_sender_ids"
                    ),
                    "is_background": True,
                },
            )
            log.info(
                "trigger_busy_enqueued",
                agent_id=agent_id,
                name=agent_record.get("name"),
                inbox_pending=len(inbox_msg_ids or []),
                wake_category=wake_category,
            )
            return

        # 6. Accept pending handoffs
        await _handoff_service.accept_pending_handoffs(project_id, agent_id)

        # 7. Build trigger context
        result = await build_trigger_context(agent_record, trigger_type)
        if result is None:
            log.info("trigger_no_context", agent_id=agent_id)
            return

        context, inbox_msg_ids, from_agent_id, wake_category = result

        # Admit before writing digest — avoid chat_messages pollution on skip
        if not await _admit_trigger_wake(
            agent,
            wake_category=wake_category,
            from_agent_id=from_agent_id,
            inbox_msg_ids=inbox_msg_ids,
        ):
            return

        # Do NOT mark inbox read here. ACK happens only after a successful
        # non-empty completion (agent.py). Timeout/error leave messages unread
        # so the info chain can resume; doom-loop is prevented by a cooldown
        # arm on the Agent after timeout/error.
        log.info(
            "trigger_firing",
            agent_id=agent_id,
            name=agent_record.get("name"),
            trigger_type=trigger_type,
            context_preview=context[:100],
            inbox_pending=len(inbox_msg_ids or []),
            wake_category=wake_category,
        )

        # 8. 保存为 background user 消息（去重 goals workbook block）
        # 如果连续两个 trigger 都带相同的 Goals Workbook 块，第二条前端刷屏。
        from hiveweave.services.charter import charter_service as _cs
        goals_ver = _cs.get_goals_version(project_id)
        chat_context = context
        if goals_ver and goals_ver == _last_goals_msg_version.get(agent_id):
            chat_context = _strip_goals_block(context)
        if goals_ver:
            _last_goals_msg_version[agent_id] = goals_ver

        from hiveweave.services.chat_message import ChatMessageService

        # P2 三连发：digest 写库前过 team_chat 去重（与 record_message 同规则）。
        # 窗口内重复 → 跳过写库（digest_msg_id=None），但仍正常 chat —
        # 超时重试语义不变：重试唤醒 agent，只是不重复落同一条消息。
        from hiveweave.services.team_chat import TeamChatService

        is_dup = await TeamChatService().check_and_mark(
            agent_id, from_agent_id or "system", agent_id, chat_context
        )
        digest_msg_id = None
        if is_dup:
            log.info("trigger_digest_deduped", agent_id=agent_id,
                     name=agent_record.get("name"))
        else:
            chat_msg_service = ChatMessageService()
            saved = await chat_msg_service.save_message(
                {
                    "agent_id": agent_id,
                    "role": "user",
                    "content": chat_context,
                    "is_background": True,
                    "is_read": False,
                    "is_context": True,
                    "team_from_agent_id": from_agent_id,
                    "team_to_agent_id": agent_id,
                }
            )
            digest_msg_id = saved.get("id") if isinstance(saved, dict) else None

        # 9. 调用 chat
        # 对齐 Elixir agent.ex:264:
        #   GenServer.call(name, {:chat, context, [trigger: true, ...]}, 30_000)
        # inbox_msg_ids 传递给 agent，在 LLM 产出非空输出后才标记已读
        # 传全量 context 给 LLM（goals 块保留）；同时把 strip 后的 chat_context
        # 作为 dedup_content 传给 busy 分支——若与 chat() 锁之间发生 busy 竞态，
        # busy 分支 3 秒去重按 dedup_content 命中上方已存的 digest，避免重复落库，
        # 且不至于改变 LLM 看到的输入。
        chat_result = await agent.chat(
            context,
            opts={
                "trigger": True,
                "from_agent_id": from_agent_id,
                "inbox_msg_ids": inbox_msg_ids,
                "wake_category": wake_category,
                "source": latch_opts.get("source") or "trigger",
                "message_type": latch_opts.get("message_type"),
                "task_id": latch_opts.get("task_id"),
                "clear_waits": latch_opts.get("clear_waits"),
                "wait_clear_sender_ids": latch_opts.get(
                    "wait_clear_sender_ids"
                ),
                "dedup_content": chat_context,
            },
        )

        if isinstance(chat_result, dict) and chat_result.get("skipped"):
            # Race: disposition gate denied after admit — demote + drop digest
            await _inbox_service.demote_wake(
                agent_id,
                list(inbox_msg_ids or []),
                reason=f"chat_skipped:{chat_result.get('skipped')}",
            )
            await _delete_chat_message(agent_id, digest_msg_id)
            log.info(
                "trigger_skipped_cleanup",
                agent_id=agent_id,
                skipped=chat_result.get("skipped"),
            )
            return

        if isinstance(chat_result, dict) and chat_result.get("error"):
            err = chat_result["error"]
            if err == "busy":
                log.warning(
                    "trigger_busy",
                    agent_id=agent_id,
                    msg="inbox messages left unread for retry",
                )
            elif err == "paused":
                log.warning(
                    "trigger_paused",
                    agent_id=agent_id,
                    msg="inbox messages left unread for retry",
                )
            else:
                log.warning(
                    "trigger_failed",
                    agent_id=agent_id,
                    error=err,
                    msg="inbox messages left unread for retry",
                )
    except Exception as e:
        log.error(
            "trigger_error",
            agent_id=agent_id,
            trigger_type=trigger_type,
            error=str(e),
            exc_info=True,
        )


async def build_trigger_context(
    agent: dict,
    trigger_type: str,
) -> tuple[str, list[str], str | None, str | None] | None:
    """构建触发上下文消息。

    对齐 Elixir agent.ex:288 build_trigger_context/2。

    构建的 blocks（按顺序）：
    1. Pending Tasks — 待处理的 handoffs（pending + accepted）
    2. Rework — 被拒绝的工作（inbox 中含 [REWORK REQUESTED] 的消息）
    3. Messages — 全文按时间序；reply_required 来自 expect_report / ask
    4. Background updates — wake=0 捎带
    5. Report Required — coordinator 专属，未上报的 handoffs

    不做平台侧类别/优先级 triage（留给未来 per-agent 助理模型）。

    Args:
        agent: agent DB 记录 dict（含 id, project_id, name, role, ...）
        trigger_type: "subordinate" 或 "coordinator"

    Returns:
        (context, inbox_msg_ids, from_agent_id, wake_category) 或 None
        - context: 构建的上下文消息字符串
        - inbox_msg_ids: 待处理的 inbox 消息 ID 列表（在 LLM 非空输出后标记已读）
        - from_agent_id: 第一条消息的发送者 ID（用于 team chat 显示）
        - wake_category: 最高优先级 inbox 类别（供 complete/waiting 闸门）
    """
    project_id = agent["project_id"]
    agent_id = agent["id"]

    # 获取 handoffs（仅未交付的）
    pending_handoffs = await _handoff_service.get_pending_handoffs(project_id, agent_id)
    accepted_handoffs = await _handoff_service.get_accepted_handoffs(project_id, agent_id)

    # 获取 inbox 未读消息
    inbox_messages = await _inbox_service.get_pending_messages(agent_id)

    # Ghost asks: contract still open even if completion already set read=1.
    # Fetch BEFORE complete-skip so complete + only-ghost-asks still wakes.
    # Inject into digest; do NOT ACK (keep original unread ids only).
    try:
        outstanding = await _inbox_service.get_outstanding_ask_messages(agent_id)
    except Exception as e:
        log.debug(
            "trigger_outstanding_asks_failed",
            agent_id=agent_id,
            error=str(e),
        )
        outstanding = []

    # 获取 background 消息（wake=0 的 progress/ACK，不触发 LLM 但随本次
    # 触发捎带进上下文 —— BUGFIX: 此前这类消息写入即 read=1，永不进上下文，
    # 导致"验证通过/交付完成"等证据对接收方不可见）
    background_msgs = await _inbox_service.get_undelivered_background(agent_id)

    # complete + no actionable wake=1 / handoffs / open ask contracts → skip
    manager = _get_agent_manager()
    live = manager.get_agent(agent_id) if manager else None
    if live is not None and getattr(live, "disposition", None) == "complete":
        if (
            not inbox_messages
            and not outstanding
            and not pending_handoffs
            and not accepted_handoffs
        ):
            # creator/reviewer 待审（submitted/reviewing）及 creator 待
            # merge（approved = CREATOR_MUST_MERGE）义务都构成唤醒理由
            # （与看门狗穿透口径一致——欠债必醒）；查询失败 fail-open 不跳过
            try:
                from hiveweave.services.task import TaskService

                _obs = await TaskService().get_actionable_obligations(
                    project_id, agent_id, promote=False)
                _ledger_duty = any(
                    o.get("role_hint") in ("reviewer", "creator")
                    and o.get("status") in ("submitted", "reviewing", "approved")
                    for o in _obs
                )
            except Exception:
                _ledger_duty = True
            if not _ledger_duty:
                log.info(
                    "trigger_complete_skip_background_only",
                    agent_id=agent_id,
                    background=len(background_msgs),
                )
                return None

    pending_for_ack = list(inbox_messages)
    inbox_messages = _merge_outstanding_digest_asks(
        inbox_messages, outstanding
    )

    # Chronological inbox — no category ranking / priority digest.
    # Future: per-agent assistant model may triage; platform stays dumb.
    wake_category = None
    has_digest = False
    ready_digest = None
    triage_batch_id = None

    # 分离 rework 消息和其他消息
    rework_msgs: list[dict] = []
    other_msgs: list[dict] = []
    for m in inbox_messages:
        msg_text = m.get("message") or ""
        if "[REWORK REQUESTED]" in msg_text:
            rework_msgs.append(m)
        else:
            other_msgs.append(m)

    # 获取未上报的 handoffs（coordinator 自检用）
    unreported = await _handoff_service.get_unreported_accepted_handoffs(
        project_id, agent_id
    )

    blocks: list[str] = []
    delivered_handoff_ids: list[str] = []

    # ── 1. Pending Tasks block ──
    if pending_handoffs or accepted_handoffs:
        import json as _json
        all_handoffs = pending_handoffs + accepted_handoffs
        lines: list[str] = []
        for h in all_handoffs:
            entry = {
                "from": await _agent_name(h.get("from_agent_id", "")),
                "task": h.get("summary") or "",
                "status": h.get("status") or "",
            }
            # D3: 始终显式输出 report_required 字段
            entry["report_required"] = bool(h.get("expect_report"))
            lines.append(_json.dumps(entry, ensure_ascii=False))
        blocks.append(
            "## Pending Tasks — each line is a JSON object with 'from', 'task', 'status', optional 'report_required'.\n"
            "Use submit_task(taskId, summary) to submit your work for review.\n"
            + "\n".join(lines)
        )
        delivered_handoff_ids = [h["id"] for h in all_handoffs if h.get("id")]

    # ── 2. Rework block ──
    if rework_msgs:
        import json as _json
        lines = []
        for m in rework_msgs:
            entry = {
                "from": await _agent_name(m.get("from_agent_id", "")),
                "status": "rejected",
                "content": m.get("message", ""),
            }
            lines.append(_json.dumps(entry, ensure_ascii=False))
        blocks.append(
            "## WORK REJECTED — Rework Required\n"
            + "\n".join(lines) + "\n\n"
            "You must fix the issues and call submit_task again after fixing."
        )

    # ── 3. Messages — full text, chronological; reply_required from expect_report ──
    if other_msgs:
        import json as _json
        from hiveweave.services.inbox import inbox_digest_content
        lines = []
        for m in other_msgs:
            entry = {
                "id": (m.get("id") or "")[:8],
                "from": await _agent_name(m.get("from_agent_id", "")),
                # Unwrap legacy busy-queue envelopes so digests stay single-layer
                "content": inbox_digest_content(m),
            }
            # D3: 始终显式输出 reply_required 字段（true/false），
            # 避免 false 时省略导致模型脑补幽灵义务
            entry["reply_required"] = bool(m.get("expect_report"))
            # Include reply_contract_id so the agent can reference it when replying
            if m.get("reply_contract_id"):
                entry["reply_contract_id"] = str(m["reply_contract_id"])
                entry["how_to_reply"] = (
                    f"send_message(recipients=['{entry['from']}'], "
                    f"replyTo='{m['reply_contract_id']}')"
                )
            if m.get("priority") == "urgent":
                entry["priority"] = "urgent"
            if m.get("task_id"):
                entry["task_id"] = str(m["task_id"])[:8]
            if m.get("message_type"):
                entry["message_type"] = m["message_type"]
            lines.append(_json.dumps(entry, ensure_ascii=False))
        if lines:
            blocks.append(
                "## Messages (chronological) — JSON lines; "
                "reply_required=true means you must message that sender before done_slice\n"
                + "\n".join(lines)
            )

    # ── 3b. Background updates（wake=0 捎带）──
    if background_msgs:
        import json as _json
        lines = []
        for m in background_msgs:
            entry = {
                "id": (m.get("id") or "")[:8],
                "from": await _agent_name(m.get("from_agent_id", "")),
                "content": m.get("message") or "",
            }
            lines.append(_json.dumps(entry, ensure_ascii=False))
        blocks.append(
            "## Background updates — 同事进度/回执（仅供参考，无需回复；"
            "其中可能包含你等待的交付证据）\n" + "\n".join(lines)
        )

    # ── 3.5. Goals workbook update (dirty check) ──
    # Only shown when dirty — doesn't trigger the agent on its own.
    # Queues alongside regular messages, delivered when agent is already
    # processing something else (user message or other agent's message).
    from hiveweave.services.charter import charter_service as _cs
    import json as _json
    if _cs.goals_dirty(agent_id, project_id):
        goals = await _cs.read_goals(project_id)
        if goals:
            parts = []
            obj = goals.get("objective", "")
            focus = goals.get("focus", "")
            krs = goals.get("keyResults", [])
            inv = goals.get("userInvolvement", "")
            if obj:
                parts.append(f"Objective: {obj}")
            if focus:
                parts.append(f"Focus: {focus}")
            if krs:
                kr_lines = "\n".join(
                    f"  - [{kr.get('status', '?')}] {kr.get('text', str(kr))}"
                    for kr in krs if isinstance(kr, dict)
                )
                parts.append(f"Key Results:\n{kr_lines}")
            if inv:
                parts.append(f"User Involvement: {inv}")
            content = "\n".join(parts) if parts else "(empty)"
            goals_entry = _json.dumps(
                {"from": "工作簿更新", "content": content}, ensure_ascii=False
            )
            blocks.insert(0, f"## Goals Workbook (updated)\n{goals_entry}")
            cur_ver = _cs.get_goals_version(project_id)
            await _cs.set_agent_goals_version(agent_id, cur_ver)

    # ── 4. Coordinator 专属 blocks ──
    if trigger_type == "coordinator":
        # 4a. Pending Review — creator/reviewer 名下待审任务（ledger 义务）。
        # 穿透唤醒的行动指引：inbox 可能全读（ghost ask 已 ACK、
        # [TASK SUBMITTED] 被 triage 扫掉），义务只在 ledger 里——没有
        # 这个块，被穿透唤醒的 coordinator 醒来无事可做，会再次
        # commit_turn(waiting) 进入循环。审完任务状态离开 submitted，
        # 块自动消失。
        try:
            from hiveweave.services.task import TaskService

            obs = await TaskService().get_actionable_obligations(
                project_id, agent_id, promote=False)
        except Exception as e:
            log.debug("trigger_review_obligations_failed",
                      agent_id=agent_id, error=str(e))
            obs = []
        review_pending = [
            o for o in obs
            if o.get("role_hint") in ("reviewer", "creator")
            and o.get("status") in ("submitted", "reviewing", "approved")
        ]
        if review_pending:
            import json as _json
            lines = []
            has_merge = False
            for o in review_pending[:10]:
                status = o.get("status")
                if status == "approved":
                    has_merge = True
                lines.append(_json.dumps({
                    "task_id": str(o.get("id") or "")[:8],
                    "title": (o.get("title") or "")[:60],
                    "status": status,
                    "you_are": o.get("role_hint"),
                }, ensure_ascii=False))
            guidance = (
                "Use review_task(taskId, decision='approve'/'rework') to review."
            )
            if has_merge:
                # approved = CREATOR_MUST_MERGE：审已过、账未清，还差 merge
                guidance += (
                    "\napproved tasks await YOUR git_worktree_merge"
                    " (CREATOR_MUST_MERGE) — merge them to clear the ledger."
                )
            blocks.append(
                "## Pending Review — 名下任务待办（ledger 义务，与 inbox 读态无关）\n"
                + guidance + "\n"
                + "\n".join(lines)
            )

        # 4. Report Required
        if unreported:
            blocks.append(
                f"## IMPORTANT — Report Required\n"
                f"You have {len(unreported)} task(s) with expect_report that "
                f"haven't been submitted for review. You MUST call "
                f"submit_task(taskId, summary) to submit your work for review."
            )

    # 无上下文 → 返回 None
    if not blocks:
        return None

    # 标记 handoffs 为已交付（不可逆）
    if delivered_handoff_ids:
        await _handoff_service.mark_delivered(project_id, delivered_handoff_ids)

    # 收集 inbox 消息 ID（在 LLM 非空输出后标记已读 + 已交付）
    # background 消息 ID 一并并入：mark_read_by_ids 会同时置 read=1/delivered=1，
    # 输出失败/超时不标记 → 下次触发重试捎带（与 wake 消息同一可靠性语义）
    # Ghost asks (read=1, contract still open) are digest-only — not ACK ids.
    inbox_msg_ids = [m["id"] for m in pending_for_ack if m.get("id")]
    inbox_msg_ids += [m["id"] for m in background_msgs if m.get("id")]

    # 提取第一个非空 from_agent_id（用于 team chat 显示）
    all_from_ids: list[str] = []
    for m in inbox_messages:
        fid = m.get("from_agent_id")
        if fid:
            all_from_ids.append(fid)
    for h in pending_handoffs + accepted_handoffs:
        fid = h.get("from_agent_id")
        if fid:
            all_from_ids.append(fid)
    from_agent_id = next((fid for fid in all_from_ids if fid), None)
    # BUG-034: 如果没有找到发送者（inbox/handoff 缺少 from_agent_id），
    # 使用 "system" 确保前端团队沟通面板不会显示"未知发送者"。
    if not from_agent_id:
        from_agent_id = "system"

    context = "\n\n".join(blocks)

    # 4.5 Lifecycle hook trigger.context.build — per-task recall injection
    # (ChatDev co-learning: recall past lessons matching this task's keywords).
    # Fail-open: hook failure must not break triggering. Output key: lessons_block.
    try:
        from typing import Any

        from hiveweave.hooks import TRIGGER_CONTEXT_BUILD, hooks

        hook_out: dict[str, Any] = {"lessons_block": None}
        await hooks.run(
            TRIGGER_CONTEXT_BUILD,
            {
                "agent_id": agent_id,
                "project_id": project_id,
                "trigger_type": trigger_type,
                "context": context,
            },
            hook_out,
        )
        lessons_block = hook_out.get("lessons_block")
        if isinstance(lessons_block, str) and lessons_block.strip():
            context = f"{context}\n\n{lessons_block}"
    except Exception as e:
        log.warning(
            "trigger_context_build_hook_failed",
            agent_id=agent_id,
            error=str(e),
        )

    return context, inbox_msg_ids, from_agent_id, wake_category


# ── AgentManager 延迟获取（避免循环导入）────────────────────


def _get_agent_manager():
    """获取全局 AgentManager 实例（延迟导入避免循环依赖）。

    trigger.py → supervisor.py → agent.py → trigger.py
    通过函数内延迟导入打破循环。
    """
    from hiveweave.agents.supervisor import agent_manager

    return agent_manager
