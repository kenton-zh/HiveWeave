"""Orchestration tools -- messaging, charter/goals, memory/logs, alarms.

Migrated from executor.py ``_tool_*`` methods to ``@tool``-registered
standalone functions.  Uses :class:`~hiveweave.tools.pipeline.ToolContext`
for service access (``ctx.inbox``, ``ctx.charter``, ``ctx.org``).

Tools:
    Messaging:     send_message, message_subordinate, message_superior,
                   message_peer, message_team
    Charter/Goals: read_charter, save_charter, read_goals, update_goals
    Memory/Logs:   read_memory, write_memory, read_work_logs, write_work_log
    Alarms:        schedule_alarm, list_alarms, cancel_alarm
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from pydantic import BaseModel, Field, ConfigDict, field_validator

from .base import tool
from .result import ToolResult
from .helpers import coerce_to_list, get_project_id, resolve_agent_id

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 1: Messaging tools
# ═══════════════════════════════════════════════════════════════════════


async def _send_message_core(
    agent_id: str,
    recipients: list[str],
    message: str,
    priority: str,
    expect_report: bool,
    ctx,
    message_type: str = "normal",
    *,
    enforce_span: bool = True,
    reply_to: str | None = None,
) -> ToolResult:
    """Core message-sending logic shared by all ``message_*`` tools.

    Handles:
    - JSON-string recipients (LLM sometimes sends ``'["HR"]'`` as a string).
    - User aliases (``user``, ``用户``, ``boss``, ``老板``) -- writes to
      ``chat_messages`` so the message appears in the user's Chat window.
    - Agent resolution: short_id -> name -> role (with warning on role
      fallback).
    - Self-skip (sending to yourself is a no-op).
    - TeamChat recording for the sender (BUG-034 fix).
    - message_type: ``ask`` forces expect_report; ``notify`` forces no expect.
    - enforce_span: command-chain gate (direct/superior/peer). ``message_team``
      sets False for intentional org-wide broadcast.
    """
    if not ctx or not getattr(ctx, "inbox", None):
        return ToolResult.err(
            "InboxService not available (ctx.inbox is missing)"
        )
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    # ── Normalise recipients ─────────────────────────────
    if isinstance(recipients, str):
        try:
            parsed = json.loads(recipients)
            if isinstance(parsed, list):
                recipients = parsed
            else:
                recipients = [recipients]
        except (json.JSONDecodeError, ValueError):
            recipients = [recipients]
    if isinstance(recipients, (list, tuple)) and len(recipients) == 0:
        recipients = []

    if not recipients:
        return ToolResult.err(
            "send_message requires 'recipients' "
            "(list of agent names or short_ids)"
        )
    if not message:
        return ToolResult.err("send_message requires 'message' (body text)")

    if message_type == "ask":
        expect_report = True
    elif message_type == "notify":
        expect_report = False
    else:
        from hiveweave.services.reply_policy import resolve_expect_report

        expect_report = resolve_expect_report(expect_report, message)

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    # ── Handle "user" / "用户" as a special recipient ────
    user_aliases = {"user", "用户", "boss", "老板"}
    user_recipients = [
        r for r in recipients if r.strip().lower() in user_aliases
    ]
    agent_recipients = [
        r for r in recipients if r.strip().lower() not in user_aliases
    ]

    results: list[dict[str, Any]] = []
    if user_recipients:
        from hiveweave.services.chat_message import ChatMessageService

        chat_service = ChatMessageService()
        await chat_service.save_message({
            "agent_id": agent_id,
            "role": "assistant",
            "content": message,
            "thinking": None,
            "tool_calls": "[]",
            "is_streaming": False,
            "is_background": False,
        })
        # Push via WebSocket so the frontend updates in real-time
        from hiveweave.realtime.event_bus import status_event_bus

        await status_event_bus.publish_chat_message(
            agent_id=agent_id,
            message={"role": "assistant", "content": message},
        )
        results.append({"to": "user", "message_id": "user-msg"})

    # ── Resolve remaining agent recipients ──────────────
    recipients = agent_recipients
    if not recipients:
        return ToolResult.ok(
            f"Messages sent. Results: {results}", results=results
        )

    all_agents = await ctx.org.list_agents(project_id)
    # Prefer active agents; never deliver to archived (ghost mailbox)
    active_agents = [
        a for a in all_agents if (a.get("status") or "active") == "active"
    ]
    resolved: list[dict[str, Any]] = []
    not_found: list[str] = []
    archived_hits: list[str] = []
    for r in recipients:
        r_stripped = r.strip()
        match = None
        # Try short_id match (active first)
        for a in active_agents:
            if a.get("short_id", "").upper() == r_stripped.upper():
                match = a
                break
        # Try name match (case-insensitive)
        if not match:
            for a in active_agents:
                if a.get("name", "").lower() == r_stripped.lower():
                    match = a
                    break
        # Try role match (e.g. "HR") -- last resort, warn to use 花名
        if not match:
            for a in active_agents:
                if a.get("role", "").lower() == r_stripped.lower():
                    match = a
                    log.warning(
                        "send_message_role_fallback",
                        agent_id=agent_id,
                        recipient=r,
                        matched_name=match.get("name"),
                        hint="use 花名 or short_id instead of role",
                    )
                    break
        if not match:
            # Detect archived-only hits for a clear error
            for a in all_agents:
                if (a.get("status") or "") != "archived":
                    continue
                if (
                    a.get("short_id", "").upper() == r_stripped.upper()
                    or a.get("name", "").lower() == r_stripped.lower()
                ):
                    archived_hits.append(r_stripped)
                    break
            not_found.append(r)
            continue
        # Skip self -- sending to yourself is a no-op
        if match["id"] == agent_id:
            log.info(
                "send_message_self_skip",
                agent_id=agent_id,
                recipient=r,
                match_name=match.get("name"),
            )
            continue
        resolved.append(match)

    if not resolved:
        # If we already sent to user, return partial success
        if results:
            return ToolResult.ok(
                f"Messages sent. Results: {results}",
                results=results,
                not_found=not_found,
            )
        hint = ""
        if archived_hits:
            hint = (
                f" Archived (cannot message): {archived_hits}. "
                "Use transfer_agent or hire a replacement — do not message ghosts."
            )
        return ToolResult.err(
            f"No active recipients found. Unknown: {not_found}.{hint} "
            f"Available active agents: "
            f"{[(a['name'], a.get('short_id'), a.get('role')) for a in active_agents]}"
        )

    # 指挥链硬门：只能联系直属下属 / 上级 / 同级（peer）
    # message_team 广播豁免（enforce_span=False）
    span_blocked: list[str] = []
    if enforce_span:
        from hiveweave.services.org_span import validate_message_span

        span_ok: list[dict[str, Any]] = []
        for target in resolved:
            err = await validate_message_span(agent_id, target["id"], ctx.org)
            if err:
                span_blocked.append(f"{target.get('name')}: {err}")
            else:
                span_ok.append(target)
        if not span_ok:
            if results:
                return ToolResult.ok(
                    f"Messages sent (user only). Cross-level blocked: {span_blocked}",
                    results=results,
                    blocked=span_blocked,
                )
            return ToolResult.err(
                "全部收件人被指挥链拒绝（禁止跨级沟通）。" + "; ".join(span_blocked)
            )
        if span_blocked:
            log.info(
                "send_message_span_filtered",
                agent_id=agent_id,
                blocked=span_blocked,
                allowed=[t.get("name") for t in span_ok],
            )
        resolved = span_ok

    # ── Send to each resolved recipient ─────────────────
    from hiveweave.services.team_chat import TeamChatService

    team_chat = TeamChatService()
    for target in resolved:
        recipient_disposition = None
        try:
            from hiveweave.agents.supervisor import agent_manager

            live = agent_manager.get_agent(target["id"])
            if live is not None:
                recipient_disposition = getattr(live, "disposition", None)
        except Exception:
            pass
        try:
            msg = await ctx.inbox.send_message(
                from_agent_id=agent_id,
                to_agent_id=target["id"],
                message=message,
                priority=priority,
                expect_report=expect_report,
                message_type=message_type,
                recipient_disposition=recipient_disposition,
                reply_to=reply_to,
            )
        except ValueError as e:
            return ToolResult.err(str(e))
        results.append({
            "to": target["name"],
            "short_id": target.get("short_id") or "",
            "message_id": msg["id"],
            "should_wake": msg.get("should_wake", True),
            "category": msg.get("category"),
        })
        # Record for sender so team comms panel shows outgoing
        # messages (BUG-034 fix).
        await team_chat.record_message(
            agent_id=agent_id,
            from_agent_id=agent_id,
            to_agent_id=target["id"],
            content=message,
        )
        # BUG-022 fix: do NOT trigger here -- the target agent's
        # inbox watcher polls every 5 s and triggers autonomously.

        # ── 标记 handoff 为已汇报 ──
        # send_message 回复对方时，只清除对该 recipient 的 expect_report 义务
        # 不清除其他 sender 的义务（A 回复 B 不应清除 C、D 的义务）
        try:
            from hiveweave.services.handoff import HandoffService
            hs = HandoffService()
            await hs.mark_reported(ctx.project_id, agent_id, to_sender_id=target["id"])
        except Exception:
            pass

    not_found_str = f" (not found: {not_found})" if not_found else ""
    return ToolResult.ok(
        f"Message sent to {len(resolved)} agent(s): "
        f"{', '.join(r['to'] for r in results)}{not_found_str}",
    )


# ── send_message ─────────────────────────────────────────


class SendMessageParams(BaseModel):
    """Parameters for send_message tool."""

    model_config = ConfigDict(populate_by_name=True)

    recipients: list[str] = Field(
        description=(
            "List of recipient agent names, short_ids, or UUIDs. "
            "Use 'user' to message the human user."
        ),
        json_schema_extra={"aliases": ["recipient", "to", "targets"]},
    )
    message: str = Field(
        description="Message body text.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )
    priority: str = Field(
        default="normal",
        description="Message priority: 'normal' or 'urgent'.",
        json_schema_extra={"aliases": ["level"]},
    )
    expect_report: bool = Field(
        default=False,
        alias="expectReport",
        description=(
            "True when recipient must reply via send_message. "
            "Also auto-set when message text asks for 回复/report back."
        ),
        json_schema_extra={"aliases": ["expectReport", "expect_report"]},
    )

    @field_validator("recipients", mode="before")
    @classmethod
    def _coerce_recipients(cls, v: Any) -> Any:
        """Handle JSON-string recipients (LLM sometimes sends '["HR]')."""
        return coerce_to_list(v)

    reply_to: str | None = Field(
        default=None,
        alias="replyTo",
        description=(
            "Reply contract ID from the original message's reply_contract_id. "
            "Include this when replying to a message that had reply_required=true "
            "to explicitly close the reply contract."
        ),
        json_schema_extra={"aliases": ["replyTo", "reply_to", "replyContractId"]},
    )


@tool(
    "send_message",
    "Sends a message to one or more specific recipients by name or agent "
    "ID. Use 'user' as a recipient to message the human user. For "
    "convenience shortcuts, use message_subordinate, message_superior, "
    "or message_peer.",
    requires_workspace=False,
    security_level="standard",
)
async def send_message_tool(
    params: SendMessageParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Send a message to one or more recipients."""
    return await _send_message_core(
        agent_id=agent_id,
        recipients=params.recipients,
        message=params.message,
        priority=params.priority,
        expect_report=params.expect_report,
        ctx=ctx,
        reply_to=params.reply_to,
    )


# ── message_subordinate ──────────────────────────────────


class MessageSubordinateParams(BaseModel):
    """Parameters for message_subordinate tool."""

    model_config = ConfigDict(populate_by_name=True)

    target: str | None = Field(
        default=None,
        description=(
            "Accepted for API compatibility. The message is sent to ALL "
            "direct subordinates regardless of this value."
        ),
        json_schema_extra={
            "aliases": ["recipient", "to", "agentId", "agent_id"]
        },
    )
    message: str = Field(
        description="Message body to send to all subordinates.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )


@tool(
    "message_subordinate",
    "Send a message to ALL your direct subordinates at once. "
    "Use dispatch_task to delegate specific work.",
    requires_workspace=False,
    security_level="standard",
)
async def message_subordinate_tool(
    params: MessageSubordinateParams,
    agent_id: str,
    workspace: str,
    ctx=None,
) -> ToolResult:
    """Send a message to all direct subordinates."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    children = await ctx.org.get_subordinates(agent_id)
    if not children:
        return ToolResult.err("No subordinates found")

    recipients = [c.get("short_id") or c.get("id") for c in children]
    return await _send_message_core(
        agent_id=agent_id,
        recipients=recipients,
        message=params.message,
        priority="normal",
        expect_report=False,
        ctx=ctx,
    )


# ── message_superior ─────────────────────────────────────


class MessageSuperiorParams(BaseModel):
    """Parameters for message_superior tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Message body to send to your superior.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )


@tool(
    "message_superior",
    "Send a message to your parent/superior. Use submit_task when "
    "finishing a delegated task.",
    requires_workspace=False,
    security_level="standard",
)
async def message_superior_tool(
    params: MessageSuperiorParams,
    agent_id: str,
    workspace: str,
    ctx=None,
) -> ToolResult:
    """Send a message to the superior."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    superior = await ctx.org.get_superior(agent_id)
    if not superior:
        return ToolResult.err("No superior found")

    recipients = [superior.get("short_id") or superior["id"]]
    return await _send_message_core(
        agent_id=agent_id,
        recipients=recipients,
        message=params.message,
        priority="normal",
        expect_report=False,
        ctx=ctx,
    )


# ── message_peer ─────────────────────────────────────────


class MessagePeerParams(BaseModel):
    """Parameters for message_peer tool."""

    model_config = ConfigDict(populate_by_name=True)

    target: str = Field(
        description="Target peer agent: name, short_id, or UUID.",
        json_schema_extra={
            "aliases": ["recipient", "to", "agentId", "agent_id"]
        },
    )
    message: str = Field(
        description="Message body to send to the peer.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )


@tool(
    "message_peer",
    "Send a direct message to a single peer agent at the same level.",
    requires_workspace=False,
    security_level="standard",
)
async def message_peer_tool(
    params: MessagePeerParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Send a message to a specific peer."""
    return await _send_message_core(
        agent_id=agent_id,
        recipients=[params.target],
        message=params.message,
        priority="normal",
        expect_report=False,
        ctx=ctx,
    )


# ── message_team ─────────────────────────────────────────


class MessageTeamParams(BaseModel):
    """Parameters for message_team tool."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(
        description="Message body to broadcast to the entire team.",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )


@tool(
    "message_team",
    "Broadcast a message to every agent in your project team "
    "(excluding yourself).",
    requires_workspace=False,
    security_level="standard",
)
async def message_team_tool(
    params: MessageTeamParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Broadcast a message to all agents in the project."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    all_agents = await ctx.org.list_agents(project_id)
    # Exclude self
    recipients = [
        a.get("short_id") or a.get("name") or a.get("id")
        for a in all_agents
        if a.get("id") != agent_id
    ]
    if not recipients:
        return ToolResult.err("No team members found to message")

    return await _send_message_core(
        agent_id=agent_id,
        recipients=recipients,
        message=params.message,
        priority="normal",
        expect_report=False,
        ctx=ctx,
        enforce_span=False,
    )


# ═══════════════════════════════════════════════════════════════════════
# Section 2: Charter / Goals tools
# ═══════════════════════════════════════════════════════════════════════


# ── read_charter ─────────────────────────────────────────


class ReadCharterParams(BaseModel):
    """Parameters for read_charter tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "read_charter",
    "Reads the organization charter document. Use it to review the "
    "mission, purpose, rules, and operating principles that govern the "
    "agent organization.",
    requires_workspace=False,
    security_level="standard",
)
async def read_charter_tool(
    params: ReadCharterParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read the project charter."""
    if not ctx or not getattr(ctx, "charter", None):
        return ToolResult.err(
            "CharterService not available (ctx.charter is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    charter = await ctx.charter.read_charter(project_id)
    if not charter:
        return ToolResult.ok("No charter has been saved yet.")

    output = "=== Project Charter ===\n"
    output += f"Title: {charter.get('title', 'N/A')}\n"
    output += f"Status: {charter.get('status', 'N/A')}\n"
    output += f"Content:\n{charter.get('content', 'N/A')}\n"
    return ToolResult.ok(output)


# ── save_charter ─────────────────────────────────────────


class SaveCharterParams(BaseModel):
    """Parameters for save_charter tool."""

    model_config = ConfigDict(populate_by_name=True)

    content: str = Field(
        description="Charter body content.",
        json_schema_extra={"aliases": ["charter", "body", "text"]},
    )
    title: str = Field(
        default="Project Charter",
        description="Charter title.",
        json_schema_extra={"aliases": ["name"]},
    )


@tool(
    "save_charter",
    "Creates or updates the organization charter document. Use it to "
    "define or amend the mission, purpose, rules, and operating "
    "principles.",
    requires_workspace=False,
    security_level="standard",
)
async def save_charter_tool(
    params: SaveCharterParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Save/update the project charter."""
    if not ctx or not getattr(ctx, "charter", None):
        return ToolResult.err(
            "CharterService not available (ctx.charter is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    if not params.content:
        return ToolResult.err(
            "save_charter requires 'content' (charter body)"
        )

    try:
        charter_id = await ctx.charter.save_charter(
            project_id,
            agent_id,
            {
                "title": params.title,
                "content": params.content,
                "status": "active",
            },
        )
        return ToolResult.ok(
            f"Charter saved (id={charter_id}). Title: {params.title}",
            charter_id=charter_id,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to save charter: {e}")


# ── read_goals ───────────────────────────────────────────


class ReadGoalsParams(BaseModel):
    """Parameters for read_goals tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "read_goals",
    "Reads the current organizational goals and objectives. Use it to "
    "review what the organization is working toward.",
    requires_workspace=False,
    security_level="standard",
)
async def read_goals_tool(
    params: ReadGoalsParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read enterprise goals."""
    if not ctx or not getattr(ctx, "charter", None):
        return ToolResult.err(
            "CharterService not available (ctx.charter is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    goals = await ctx.charter.read_goals(project_id)
    if not goals:
        return ToolResult.ok("No goals have been set yet.")

    output = "=== Enterprise Goals ===\n"
    output += f"Objective: {goals.get('objective', 'N/A')}\n"
    output += f"Focus: {goals.get('focus', 'N/A')}\n"
    output += f"User Involvement: {goals.get('userInvolvement', 'N/A')}\n"
    krs = goals.get("keyResults", [])
    if krs:
        output += "Key Results:\n"
        for i, kr in enumerate(krs, 1):
            if isinstance(kr, dict):
                output += (
                    f"  {i}. {kr.get('description', kr.get('text', str(kr)))}\n"
                )
            else:
                output += f"  {i}. {kr}\n"
    return ToolResult.ok(output)


# ── update_goals ─────────────────────────────────────────


class UpdateGoalsParams(BaseModel):
    """Parameters for update_goals tool."""

    model_config = ConfigDict(populate_by_name=True)

    objective: str | None = Field(
        default=None,
        description="The main objective.",
    )
    focus: str | None = Field(
        default=None,
        description="Focus area.",
    )
    key_results: list[Any] | None = Field(
        default=None,
        alias="keyResults",
        description="Key results list.",
        json_schema_extra={"aliases": ["keyResults", "key_results"]},
    )

    @field_validator("key_results", mode="before")
    @classmethod
    def _coerce_key_results(cls, v: Any) -> Any:
        return coerce_to_list(v)

    user_involvement: str | None = Field(
        default=None,
        alias="userInvolvement",
        description="User involvement level.",
        json_schema_extra={"aliases": ["userInvolvement", "user_involvement"]},
    )


@tool(
    "update_goals",
    "Updates the organizational goals, objectives, focus areas, and "
    "key results. At least one field must be provided.",
    requires_workspace=False,
    security_level="standard",
)
async def update_goals_tool(
    params: UpdateGoalsParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Update enterprise goals."""
    if not ctx or not getattr(ctx, "charter", None):
        return ToolResult.err(
            "CharterService not available (ctx.charter is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    goals: dict[str, Any] = {}
    if params.objective is not None:
        goals["objective"] = params.objective
    if params.focus is not None:
        goals["focus"] = params.focus
    if params.key_results is not None:
        goals["key_results"] = params.key_results
    if params.user_involvement is not None:
        goals["user_involvement"] = params.user_involvement

    if not goals:
        return ToolResult.err(
            "update_goals requires at least one of: "
            "objective, focus, keyResults, userInvolvement"
        )

    try:
        await ctx.charter.update_goals(project_id, goals)
        return ToolResult.ok("Goals updated successfully.")
    except Exception as e:
        return ToolResult.err(f"Failed to update goals: {e}")


# ═══════════════════════════════════════════════════════════════════════
# Section 3: Memory / Logs tools
# ═══════════════════════════════════════════════════════════════════════


# ── read_memory ──────────────────────────────────────────


class ReadMemoryParams(BaseModel):
    """Parameters for read_memory tool."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str | None = Field(
        default=None,
        alias="agentId",
        description=(
            "Agent ID, name, or short_id to read memories for. "
            "Defaults to the calling agent (self)."
        ),
        json_schema_extra={"aliases": ["agentId", "agent_id", "agent", "id"]},
    )
    module_id: str | None = Field(
        default=None,
        alias="moduleId",
        description="Optional module/scope filter for memories.",
        json_schema_extra={"aliases": ["moduleId", "module_id", "key"]},
    )


@tool(
    "read_memory",
    "Reads previously stored memory for an agent. Use agentId to read "
    "another agent's memories; omit to read your own.",
    requires_workspace=False,
    security_level="standard",
)
async def read_memory_tool(
    params: ReadMemoryParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read agent memories."""
    from hiveweave.services.memory import MemoryService

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    # Determine target agent: default to self
    target_agent_id = agent_id
    if params.agent_id:
        resolved = await resolve_agent_id(
            project_id, params.agent_id, ctx.org if ctx else None
        )
        if not resolved:
            return ToolResult.err(f"Agent not found: {params.agent_id}")
        target_agent_id = resolved

    mem = MemoryService()
    try:
        # BUG-P1a: moduleId 是 module_id 列过滤条件，不是 scope。
        # 写入侧 add_entry 恒以 scope='agent' 落库（memory.py），
        # 读取侧必须按 scope='agent' + module_id 列过滤才能读写对称。
        entries = await mem.get_agent_memories(
            target_agent_id, project_id, "agent", module_id=params.module_id
        )
        if not entries:
            return ToolResult.ok("(no memories)")
        lines = [
            f"- [{e.get('type', '?')}] {e.get('content', '')}"
            for e in entries[:20]
        ]
        return ToolResult.ok("\n".join(lines))
    except Exception as e:
        return ToolResult.err(f"Failed to read memories: {e}")


# ── write_memory ─────────────────────────────────────────


class WriteMemoryParams(BaseModel):
    """Parameters for write_memory tool."""

    model_config = ConfigDict(populate_by_name=True)

    content: str = Field(
        description="Content to store in memory.",
        json_schema_extra={"aliases": ["data", "body", "text", "memory"]},
    )
    module_id: str | None = Field(
        default=None,
        alias="moduleId",
        description="Optional module/scope ID.",
        json_schema_extra={"aliases": ["moduleId", "module_id", "id", "key"]},
    )
    tags: list[str] | None = Field(
        default=None,
        description="Optional tags for the memory entry.",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> Any:
        return coerce_to_list(v)


@tool(
    "write_memory",
    "Writes content to the agent memory system. Use it to store "
    "information, context, or state for later retrieval by read_memory.",
    requires_workspace=False,
    security_level="standard",
)
async def write_memory_tool(
    params: WriteMemoryParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Write a memory entry."""
    from hiveweave.services.memory import MemoryService

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    if not params.content:
        return ToolResult.err("write_memory requires 'content'")

    mem = MemoryService()
    try:
        await mem.add_entry(
            agent_id=agent_id,
            project_id=project_id,
            content=params.content,
            category="tool_written",
            module_id=params.module_id,
            tags=params.tags if isinstance(params.tags, list) else [],
        )
        return ToolResult.ok("Memory saved.")
    except Exception as e:
        return ToolResult.err(f"Failed to write memory: {e}")


# ── read_work_logs ───────────────────────────────────────


class ReadWorkLogsParams(BaseModel):
    """Parameters for read_work_logs tool."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str | None = Field(
        default=None,
        alias="agentId",
        description=(
            "Agent ID/name/short_id to read logs for. Omit to read all "
            "subordinates' logs."
        ),
        json_schema_extra={
            "aliases": ["agentId", "agent_id", "agent", "target"]
        },
    )
    limit: int = Field(
        default=10,
        description="Maximum number of log entries per agent.",
        json_schema_extra={"aliases": ["count", "max"]},
    )


@tool(
    "read_work_logs",
    "Read work logs. Use agentId to specify whose logs to read; omit it "
    "to read all subordinates' logs. Each log entry shows what the agent "
    "did (type) and a summary.",
    requires_workspace=False,
    security_level="standard",
)
async def read_work_logs_tool(
    params: ReadWorkLogsParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Read work logs from subordinates or a specific agent."""
    if not ctx or not getattr(ctx, "org", None):
        return ToolResult.err(
            "OrgService not available (ctx.org is missing)"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    # If target specified, resolve it; otherwise list all subordinates'
    # logs
    if params.agent_id:
        target_agent = await ctx.org.resolve_agent(params.agent_id)
        if not target_agent:
            return ToolResult.err(f"Agent not found: {params.agent_id}")
        target_ids = [target_agent["id"]]
    else:
        subs = await ctx.org.get_subordinates(agent_id)
        target_ids = [s["id"] for s in subs]

    if not target_ids:
        return ToolResult.ok("No agents to read work logs from.")

    from hiveweave.services.work_log import WorkLogService

    wls = WorkLogService()
    all_logs: list[dict[str, Any]] = []
    for tid in target_ids:
        try:
            rows = await wls.get_recent(project_id, tid, params.limit)
            all_logs.extend(rows)
        except Exception as exc:
            log.warning(
                "read_work_logs_failed",
                agent_id=tid,
                project_id=project_id,
                error=str(exc),
            )

    if not all_logs:
        return ToolResult.ok("No work logs found.")

    # Newest first across agents
    all_logs.sort(key=lambda r: int(r.get("created_at") or 0), reverse=True)

    lines = []
    for log_entry in all_logs:
        ts = log_entry.get("created_at", 0)
        summary = (
            log_entry.get("summary")
            or log_entry.get("content")
            or ""
        )
        lines.append(
            f"[{ts}] {log_entry.get('agent_id', '?')} "
            f"({log_entry.get('type', '?')}): "
            f"{str(summary)[:100]}"
        )
    return ToolResult.ok(
        f"=== Work Logs ({len(all_logs)}) ===\n" + "\n".join(lines)
    )


# ── write_work_log ───────────────────────────────────────


class WriteWorkLogParams(BaseModel):
    """Parameters for write_work_log tool."""

    model_config = ConfigDict(populate_by_name=True)

    content: str = Field(
        description="Work log summary/content.",
        json_schema_extra={
            "aliases": ["summary", "message", "description"]
        },
    )
    entry_type: str | None = Field(
        default=None,
        alias="entryType",
        description=(
            "Log entry type (e.g. discussion, progress, milestone). "
            "Defaults to 'discussion'."
        ),
        json_schema_extra={
            "aliases": [
                "entryType",
                "entry_type",
                "type",
                "logType",
                "log_type",
            ]
        },
    )
    details: str | None = Field(
        default=None,
        description="Optional additional details.",
        json_schema_extra={"aliases": ["data", "extra", "metadata"]},
    )


@tool(
    "write_work_log",
    "Record what you just did in your work log. Use todowrite for "
    "planning future tasks.",
    requires_workspace=False,
    security_level="standard",
)
async def write_work_log_tool(
    params: WriteWorkLogParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Record a work log entry for the calling agent."""
    from hiveweave.services.work_log import WorkLogService

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project_id")

    if not params.content:
        return ToolResult.err("write_work_log requires 'content'")

    log_type = params.entry_type or "discussion"
    wl = WorkLogService()
    log_id = await wl.write_work_log(
        project_id,
        agent_id,
        None,
        log_type,
        params.content,
        details=params.details,
    )
    return ToolResult.ok(
        f"Work log written (id={log_id}, type={log_type}).",
        log_id=log_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# Section 4: Alarm tools
# ═══════════════════════════════════════════════════════════════════════


# ── schedule_alarm ───────────────────────────────────────


class ScheduleAlarmParams(BaseModel):
    """Parameters for schedule_alarm tool."""

    model_config = ConfigDict(populate_by_name=True)

    fire_in_game_seconds: int = Field(
        alias="fireInGameSeconds",
        description="Delay in game-time seconds before the alarm fires.",
        json_schema_extra={
            "aliases": ["fireInGameSeconds", "fire_in_game_seconds", "delay"]
        },
    )
    purpose: str = Field(
        description=(
            "Message delivered when the alarm fires. "
            "For task waits include taskId, e.g. 'task <id>: check script result'."
        ),
        json_schema_extra={"aliases": ["message", "description"]},
    )
    repeat_interval_seconds: int = Field(
        default=0,
        alias="repeatIntervalSeconds",
        description=(
            "If set (> 0), alarm repeats every N game-time seconds. "
            "Omit or set to 0 for one-shot."
        ),
        json_schema_extra={
            "aliases": [
                "repeatIntervalSeconds",
                "repeat_interval_seconds",
                "interval",
            ]
        },
    )
    to_agent_id: str | None = Field(
        default=None,
        alias="toAgentId",
        description="Agent to deliver the alarm to. Defaults to self.",
        json_schema_extra={
            "aliases": ["toAgentId", "to_agent_id", "target"]
        },
    )


@tool(
    "schedule_alarm",
    "Schedule an alarm to fire after a game-time delay, optionally "
    "repeating.",
    requires_workspace=False,
    security_level="standard",
)
async def schedule_alarm_tool(
    params: ScheduleAlarmParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Schedule an alarm: one-shot or recurring."""
    if not params.purpose:
        return ToolResult.err(
            "schedule_alarm requires 'purpose' "
            "(message delivered on fire)"
        )
    if not params.fire_in_game_seconds or params.fire_in_game_seconds <= 0:
        return ToolResult.err("schedule_alarm requires 'fireInGameSeconds' > 0")

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    # Resolve to_agent: if empty or "self", use caller
    to_id = agent_id
    if params.to_agent_id and params.to_agent_id not in ("self", "me"):
        from hiveweave.services.org import OrgService

        org = ctx.org if ctx else OrgService()
        agents = await org.list_agents(project_id)
        for a in agents:
            if (
                a.get("id") == params.to_agent_id
                or a.get("short_id") == params.to_agent_id
                or a.get("name") == params.to_agent_id
            ):
                to_id = a["id"]
                break

    from hiveweave.services.game_time import GameTimeService

    gts = GameTimeService(project_id)
    current = await gts.get_current_time(project_id)
    fire_at = (current.get("game_seconds", 0) or 0) + params.fire_in_game_seconds

    alarm_id = await gts.schedule_alarm(
        project_id=project_id,
        from_agent_id=agent_id,
        to_agent_id=to_id,
        purpose=params.purpose,
        fire_at_game_seconds=fire_at,
        repeat_interval_seconds=(
            params.repeat_interval_seconds
            if params.repeat_interval_seconds
            else 0
        ),
    )
    kind = "recurring" if params.repeat_interval_seconds else "one-shot"
    return ToolResult.ok(
        f"Alarm scheduled ({kind}). Fires at game second {fire_at} "
        f"(in {params.fire_in_game_seconds} game seconds). "
        f"Use alarm_id={alarm_id} to cancel.",
        alarm_id=alarm_id,
    )


# ── list_alarms ──────────────────────────────────────────


class ListAlarmsParams(BaseModel):
    """Parameters for list_alarms tool."""

    model_config = ConfigDict(populate_by_name=True)


@tool(
    "list_alarms",
    "List all pending scheduled alarms.",
    requires_workspace=False,
    security_level="standard",
)
async def list_alarms_tool(
    params: ListAlarmsParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """List all pending alarms for the project."""
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    from hiveweave.services.game_time import GameTimeService

    gts = GameTimeService(project_id)
    alarms = await gts.get_alarms(project_id)
    pending = [a for a in alarms if a.get("status") == "pending"]
    if not pending:
        return ToolResult.ok("(no pending alarms)")

    current = await gts.get_current_time(project_id)
    now = current.get("game_seconds", 0) or 0
    lines = []
    for a in pending[:20]:
        remaining = max(0, (a.get("fire_at_game_seconds", 0) or 0) - now)
        lines.append(
            f"[{a['id']}] fire in {remaining}gs -- {a.get('purpose', '?')}"
        )
    return ToolResult.ok("\n".join(lines))


# ── cancel_alarm ─────────────────────────────────────────


class CancelAlarmParams(BaseModel):
    """Parameters for cancel_alarm tool."""

    model_config = ConfigDict(populate_by_name=True)

    alarm_id: str = Field(
        alias="alarmId",
        description="ID of the alarm to cancel.",
        json_schema_extra={"aliases": ["alarmId", "alarm_id", "id"]},
    )


@tool(
    "cancel_alarm",
    "Cancel a scheduled alarm by its ID.",
    requires_workspace=False,
    security_level="standard",
)
async def cancel_alarm_tool(
    params: CancelAlarmParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Cancel a pending alarm."""
    if not params.alarm_id:
        return ToolResult.err("cancel_alarm requires 'alarmId'")

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    from hiveweave.services.game_time import GameTimeService

    gts = GameTimeService(project_id)
    await gts.cancel_alarm(params.alarm_id)
    return ToolResult.ok(f"Alarm {params.alarm_id} cancellation requested.")
