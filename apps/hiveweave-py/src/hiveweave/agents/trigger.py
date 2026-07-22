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


async def trigger_subordinate(agent_id: str) -> None:
    """触发下属 executor 处理待处理内容。

    在 dispatch_task 或 rework 请求后调用。
    异步执行：延迟 100ms → 检查状态 → 构建上下文 → 调用 chat。

    对齐 Elixir agent.ex:157 trigger_subordinate/1。
    """
    await _do_trigger(agent_id, "subordinate")


async def trigger_coordinator(agent_id: str) -> None:
    """触发 coordinator 处理待处理 inbox 消息。

    仅当 coordinator 有未读消息时才执行（避免浪费 token）。

    对齐 Elixir agent.ex:168 trigger_coordinator/1。
    """
    await _do_trigger(agent_id, "coordinator")


# ── 内部实现 ────────────────────────────────────────────────


async def _do_trigger(agent_id: str, trigger_type: str) -> None:
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
        if trigger_type == "coordinator":
            pending = await _inbox_service.get_pending_messages(agent_id)
            if not pending:
                background = await _inbox_service.get_undelivered_background(
                    agent_id
                )
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

        # Give-up latch: task-class inbox / 30min decay unlocks before block
        latch_opts: dict = {"trigger": True, "source": "trigger"}
        try:
            pending_for_latch = await _inbox_service.get_pending_messages(agent_id)
            if any(
                (m.get("message_type") or "").lower() == "task"
                or m.get("task_id")
                for m in (pending_for_latch or [])
            ):
                latch_opts["source"] = "task"
                latch_opts["message_type"] = "task"
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
                # Triage running / fail-closed: still latch ids so wake is not
                # dropped under busy (Medium: busy+triage enqueue).
                pending = await _inbox_service.get_pending_messages(agent_id)
                background = await _inbox_service.get_undelivered_background(
                    agent_id
                )
                pool = list(pending) + list(background)
                inbox_msg_ids = [m["id"] for m in pool if m.get("id")]
                if not inbox_msg_ids:
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
                    inbox_msg_ids=inbox_msg_ids,
                ):
                    return
                await agent.enqueue_wake(
                    "[Inbox triage pending — recheck when idle]",
                    opts={
                        "trigger": True,
                        "from_agent_id": from_id,
                        "inbox_msg_ids": inbox_msg_ids,
                        "wake_category": wake_cat,
                        "source": latch_opts.get("source") or "trigger_busy_queue",
                        "message_type": latch_opts.get("message_type"),
                        "task_id": latch_opts.get("task_id"),
                        "is_background": True,
                    },
                )
                log.info(
                    "trigger_busy_enqueued_triage_pending",
                    agent_id=agent_id,
                    inbox_pending=len(inbox_msg_ids),
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

    # 获取 background 消息（wake=0 的 progress/ACK，不触发 LLM 但随本次
    # 触发捎带进上下文 —— BUGFIX: 此前这类消息写入即 read=1，永不进上下文，
    # 导致"验证通过/交付完成"等证据对接收方不可见）
    background_msgs = await _inbox_service.get_undelivered_background(agent_id)

    # complete + no actionable wake=1 / handoffs → skip (don't burn quota on
    # background-only progress/ACK 捎带)
    manager = _get_agent_manager()
    live = manager.get_agent(agent_id) if manager else None
    if live is not None and getattr(live, "disposition", None) == "complete":
        if (
            not inbox_messages
            and not pending_handoffs
            and not accepted_handoffs
        ):
            log.info(
                "trigger_complete_skip_background_only",
                agent_id=agent_id,
                background=len(background_msgs),
            )
            return None

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
        lines = []
        for m in other_msgs:
            entry = {
                "id": (m.get("id") or "")[:8],
                "from": await _agent_name(m.get("from_agent_id", "")),
                "content": m.get("message") or "",
            }
            # D3: 始终显式输出 reply_required 字段（true/false），
            # 避免 false 时省略导致模型脑补幽灵义务
            entry["reply_required"] = bool(
                m.get("expect_report")
                or (m.get("message_type") or "").lower() == "ask"
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
    inbox_msg_ids = [m["id"] for m in inbox_messages if m.get("id")]
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
    return context, inbox_msg_ids, from_agent_id, wake_category


# ── AgentManager 延迟获取（避免循环导入）────────────────────


def _get_agent_manager():
    """获取全局 AgentManager 实例（延迟导入避免循环依赖）。

    trigger.py → supervisor.py → agent.py → trigger.py
    通过函数内延迟导入打破循环。
    """
    from hiveweave.agents.supervisor import agent_manager

    return agent_manager
