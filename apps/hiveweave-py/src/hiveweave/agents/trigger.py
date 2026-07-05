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
            log.warning("trigger_no_agent_task", agent_id=agent_id)
            return

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
        all_handoffs = pending_handoffs + accepted_handoffs
        lines: list[str] = []
        for h in all_handoffs:
            from_name = await _agent_name(h.get("from_agent_id", ""))
            summary = h.get("summary") or ""
            h_status = h.get("status") or ""
            report_tag = " (report required)" if h.get("expect_report") else ""
            lines.append(
                f"  - From: {from_name}\n"
                f"    Task: {summary}\n"
                f"    Status: {h_status}{report_tag}"
            )
        handoff_text = "\n".join(lines)
        blocks.append(
            f"## Pending Tasks (respond in CAVEMAN style)\n{handoff_text}"
        )
        delivered_handoff_ids = [h["id"] for h in all_handoffs if h.get("id")]

    # ── 2. Rework block ──
    if rework_msgs:
        lines = []
        for m in rework_msgs:
            from_name = await _agent_name(m.get("from_agent_id", ""))
            lines.append(f"  - From: {from_name}\n    {m.get('message', '')}")
        rework_text = "\n".join(lines)
        blocks.append(
            f"## WORK REJECTED — Rework Required\n{rework_text}\n\n"
            "You must fix the issues and call report_completion again."
        )

    # ── 3. Messages block ──
    if other_msgs:
        lines = []
        for m in other_msgs:
            prefix = "**[REPLY REQUIRED]** " if m.get("expect_report") else ""
            from_name = await _agent_name(m.get("from_agent_id", ""))
            msg_type = m.get("message_type", "normal")
            priority = m.get("priority", "normal")
            lines.append(
                f"  - From: {from_name} (type={msg_type}, priority={priority})\n"
                f"    {prefix}{m.get('message', '')}"
            )
        msg_text = "\n".join(lines)
        blocks.append(
            f"## Messages (from other agents — reply in CAVEMAN style, "
            f"NO pleasantries)\n{msg_text}"
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
