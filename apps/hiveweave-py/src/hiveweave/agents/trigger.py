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

from hiveweave.services.dispatch import DispatchService
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

# ── 模块级服务实例 ──────────────────────────────────────────

_org_service = OrgService()
_inbox_service = InboxService()
_handoff_service = HandoffService()
_dispatch_service = DispatchService()


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


def _is_coordinator(role: str | None) -> bool:
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

        # 4. coordinator：检查是否有 pending inbox 消息
        if trigger_type == "coordinator":
            pending = await _inbox_service.get_pending_messages(agent_id)
            if not pending:
                log.info(
                    "trigger_coordinator_no_messages",
                    agent_id=agent_id,
                )
                return

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

        # 5. 检查 agent 是否正在 processing → 跳过
        # 对齐 Elixir agent.ex:217-224: 等完成后自检 re-trigger
        if agent.status.value == "processing":
            log.info(
                "trigger_busy_skip",
                agent_id=agent_id,
                name=agent_record.get("name"),
            )
            return

        # 6. Accept pending handoffs
        await _handoff_service.accept_pending_handoffs(project_id, agent_id)

        # 7. Build trigger context
        result = await build_trigger_context(agent_record, trigger_type)
        if result is None:
            log.info("trigger_no_context", agent_id=agent_id)
            return

        context, inbox_msg_ids, from_agent_id = result

        log.info(
            "trigger_firing",
            agent_id=agent_id,
            name=agent_record.get("name"),
            trigger_type=trigger_type,
            context_preview=context[:100],
        )

        # 8. 保存为 background user 消息
        from hiveweave.services.chat_message import ChatMessageService

        chat_msg_service = ChatMessageService()
        await chat_msg_service.save_message(
            {
                "agent_id": agent_id,
                "role": "user",
                "content": context,
                "is_background": True,
                "is_read": False,
                "is_context": True,
                "team_from_agent_id": from_agent_id,
                "team_to_agent_id": agent_id,
            }
        )

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
            },
        )

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
) -> tuple[str, list[str], str | None] | None:
    """构建触发上下文消息。

    对齐 Elixir agent.ex:288 build_trigger_context/2。

    构建的 blocks（按顺序）：
    1. Pending Tasks — 待处理的 handoffs（pending + accepted）
    2. Rework — 被拒绝的工作（inbox 中含 [REWORK REQUESTED] 的消息）
    3. Messages — 其他 inbox 消息
    4. Subordinate Logs — coordinator 专属，下属的工作日志
    5. Report Required — coordinator 专属，未上报的 handoffs

    Args:
        agent: agent DB 记录 dict（含 id, project_id, name, role, ...）
        trigger_type: "subordinate" 或 "coordinator"

    Returns:
        (context, inbox_msg_ids, from_agent_id) 或 None（无上下文时）
        - context: 构建的上下文消息字符串
        - inbox_msg_ids: 待处理的 inbox 消息 ID 列表（在 LLM 非空输出后标记已读）
        - from_agent_id: 第一条消息的发送者 ID（用于 team chat 显示）
    """
    project_id = agent["project_id"]
    agent_id = agent["id"]

    # 获取 handoffs（仅未交付的）
    pending_handoffs = await _handoff_service.get_pending_handoffs(project_id, agent_id)
    accepted_handoffs = await _handoff_service.get_accepted_handoffs(project_id, agent_id)

    # 获取 inbox 未读消息
    inbox_messages = await _inbox_service.get_pending_messages(agent_id)

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
            if h.get("expect_report"):
                entry["report_required"] = True
            lines.append(_json.dumps(entry, ensure_ascii=False))
        blocks.append(
            "## Pending Tasks — each line is a JSON object with 'from', 'task', 'status', optional 'report_required'.\n"
            "Use send_message(recipients=[\"上级花名\"], message=\"...\", expectReport=true) to report results.\n"
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
            "You must fix the issues and call report_completion again."
        )

    # ── 3. Messages block ──
    # BUG-036: JSON-structured messages — no ambiguity from text formatting.
    # Each message is one JSON line: {"from": "...", "content": "...", ...}
    # LLM can parse fields unambiguously, unlike free-text [来自: xxx] format.
    if other_msgs:
        import json as _json
        lines = []
        for m in other_msgs:
            entry = {
                "from": await _agent_name(m.get("from_agent_id", "")),
                "content": m.get("message", ""),
            }
            if m.get("expect_report"):
                entry["reply_required"] = True
            if m.get("priority") == "urgent":
                entry["priority"] = "urgent"
            lines.append(_json.dumps(entry, ensure_ascii=False))
        msg_text = "\n".join(lines)
        blocks.append(
            f"## Messages — each line is a JSON object with 'from', 'content', optional 'reply_required' and 'priority'.\n"
            f"To reply to these, you MUST call send_message(recipients=[\"对方花名\"], message=\"...\").\n"
            f"Your assistant text DOES NOT reach other agents. Only send_message does.\n"
            f"CAVEMAN style, NO pleasantries.\n{msg_text}"
        )

    # ── 4 & 5. Coordinator 专属 blocks ──
    if trigger_type == "coordinator":
        children = await _org_service.get_subordinates(agent_id)

        # 4. Subordinate Logs
        child_log_lines: list[str] = []
        for child in children:
            child_id = child["id"]
            child_name = child.get("name") or child_id
            logs = await _dispatch_service.get_subordinate_logs(
                project_id, child_id, limit=5
            )
            for l in logs:
                log_type = l.get("type", "unknown")
                summary = l.get("summary", "")
                child_log_lines.append(
                    f"  [{child_name}] [{log_type}] {summary}"
                )

        if child_log_lines:
            blocks.append(
                f"## Subordinate Work Logs (terse format)\n"
                f"{chr(10).join(child_log_lines)}"
            )

        # 5. Report Required
        if unreported:
            blocks.append(
                f"## IMPORTANT — Report Required\n"
                f"You have {len(unreported)} task(s) with expect_report that "
                f"haven't been reported up. You MUST call message_superior to "
                f"report results to your superior."
            )

    # 无上下文 → 返回 None
    if not blocks:
        return None

    # 标记 handoffs 为已交付（不可逆）
    if delivered_handoff_ids:
        await _handoff_service.mark_delivered(project_id, delivered_handoff_ids)

    # 收集 inbox 消息 ID（在 LLM 非空输出后标记已读）
    inbox_msg_ids = [m["id"] for m in inbox_messages if m.get("id")]

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

    context = (
        "\n\n".join(blocks)
        + "\n\n---\nProcess the above. Use tools to work on tasks, report results."
    )

    return context, inbox_msg_ids, from_agent_id


# ── AgentManager 延迟获取（避免循环导入）────────────────────


def _get_agent_manager():
    """获取全局 AgentManager 实例（延迟导入避免循环依赖）。

    trigger.py → supervisor.py → agent.py → trigger.py
    通过函数内延迟导入打破循环。
    """
    from hiveweave.agents.supervisor import agent_manager

    return agent_manager
