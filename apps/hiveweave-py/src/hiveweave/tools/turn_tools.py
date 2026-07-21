"""Turn protocol tools: commit_turn, ask_agent, notify_agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.tools.helpers import coerce_to_list
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult
from hiveweave.services.turn_result import (
    TURN_RESULT_SCHEMA_VERSION,
    parse_turn_result,
    validate_phase_fields,
)
from hiveweave.services.turn_session import (
    get_pending_turn_result,
    set_pending_turn_result,
)


# ── commit_turn ──────────────────────────────────────────


class CommitTurnParams(BaseModel):
    """Mandatory end-of-turn return value (TurnResult ABI)."""

    model_config = ConfigDict(populate_by_name=True)

    phase: Literal["in_progress", "waiting", "blocked", "done_slice"] = Field(
        description=(
            "Control plane: in_progress=keep working; waiting=legal wait; "
            "blocked=stuck; done_slice=obligations for this slice cleared"
        ),
    )
    summary: str = Field(
        description="1-2 sentences: what this turn accomplished",
        json_schema_extra={"aliases": ["content", "message", "text"]},
    )
    waiting_on: list[dict[str, Any]] | None = Field(
        default=None,
        alias="waitingOn",
        description=(
            "Required for waiting/blocked. "
            "Items: {kind: agent|task|user|timer|external, ref: str, note?: str}"
        ),
        json_schema_extra={"aliases": ["waitingOn", "waiting_on"]},
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Data plane payload (replies, tasks, artifacts, …). May be {}",
    )
    extensions: dict[str, Any] | None = Field(
        default=None,
        description="Forward-compatible extensions. May be {}",
    )


@tool(
    "commit_turn",
    "MANDATORY end-of-turn return value. Every turn is a function call — "
    "you MUST commit_turn before stopping. phase=in_progress keeps you working; "
    "waiting/blocked require waiting_on; done_slice only when this slice's "
    "obligations are cleared. Assistant text is NOT a return value.",
    requires_workspace=False,
    security_level="standard",
)
async def commit_turn_tool(
    params: CommitTurnParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Validate and buffer TurnResult for exit gates + persist."""
    raw: dict[str, Any] = {
        "schema_version": TURN_RESULT_SCHEMA_VERSION,
        "phase": params.phase,
        "summary": params.summary,
        "waiting_on": params.waiting_on or [],
        "result": params.result if params.result is not None else {},
        "extensions": params.extensions if params.extensions is not None else {},
    }
    try:
        tr = parse_turn_result(raw)
    except Exception as e:
        return ToolResult.err(f"Invalid TurnResult: {e}")

    field_violations = validate_phase_fields(tr)
    if field_violations:
        return ToolResult.err(
            "commit_turn rejected: "
            + ", ".join(field_violations)
            + ". waiting/blocked require waiting_on=[{kind, ref}]."
        )

    payload = tr.to_persist_dict()

    # P2 doom loop 缓解：同一 turn 内同参数 commit_turn 已被接受过时，
    # 返回差异化提示（而非逐字相同的 "TurnResult accepted"），让模型看到
    # 新信息，打破"相同工具结果 → 相同决策"的重复调用循环。
    prev = get_pending_turn_result(agent_id)
    if isinstance(prev, dict) and prev == payload:
        return ToolResult.ok(
            f"TurnResult ALREADY committed (phase={tr.phase}) — this exact "
            "commit_turn was already accepted this turn. Do NOT call "
            "commit_turn again with the same arguments; produce your final "
            "assistant text now and let the exit gates evaluate. If a gate "
            "rejects the exit, it will tell you what is still outstanding.",
            turn_result=payload,
            duplicate=True,
        )

    set_pending_turn_result(agent_id, payload)

    # Persist for observability
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.work_log import WorkLogService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id and ctx is not None:
            project_id = getattr(ctx, "project_id", None)
        if project_id:
            await WorkLogService().write_work_log(
                project_id,
                agent_id,
                None,
                "turn_result",
                f"[{tr.phase}] {tr.summary}"[:140],
                details=payload,
            )
    except Exception:
        pass

    return ToolResult.ok(
        f"TurnResult accepted: phase={tr.phase}. "
        f"{'Will continue working.' if tr.phase == 'in_progress' else 'Ready to exit if gates pass.'}",
        turn_result=payload,
    )


# ── defer_task_advance（不推进）───────────────────────────


class DeferTaskAdvanceParams(BaseModel):
    """Explicitly decline to advance actionable tasks this wake cycle."""

    model_config = ConfigDict(populate_by_name=True)

    reason: str = Field(
        description=(
            "Why you cannot advance now (blocked on whom/what, missing info, "
            "waiting for human, etc.). Be concrete — not empty filler."
        ),
        json_schema_extra={"aliases": ["reason", "why", "note", "summary"]},
    )


@tool(
    "defer_task_advance",
    "不推进：本轮无法推动可行动任务时必须调用。声明后平台停止 [TASK ADVANCE] "
    "循环提醒，直到你被再次唤醒（用户/inbox/任务）。不要用空话收工代替本工具。",
    requires_workspace=False,
    security_level="standard",
)
async def defer_task_advance_tool(
    params: DeferTaskAdvanceParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Mark this wake cycle as intentional no-advance — stops nudge loop."""
    from hiveweave.services.turn_session import set_task_advance_deferred

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "defer_task_advance requires a non-empty reason "
            "(why you cannot advance now)."
        )

    set_task_advance_deferred(agent_id, True)

    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.work_log import WorkLogService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id and ctx is not None:
            project_id = getattr(ctx, "project_id", None)
        if project_id:
            await WorkLogService().write_work_log(
                project_id,
                agent_id,
                None,
                "task_advance_deferred",
                f"[不推进] {reason}"[:140],
                details={"reason": reason},
            )
    except Exception:
        pass

    return ToolResult.ok(
        "已声明不推进。平台不会再因「未推动任务」循环提醒你，"
        "直到你被再次唤醒。请接着 commit_turn"
        "(通常 phase=waiting 或 blocked，并写清 waiting_on)。"
        f" reason={reason[:200]}"
    )


# ── ask_agent / notify_agent ─────────────────────────────


class AskNotifyParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recipients: list[str] = Field(
        description="Recipient 花名, short_id, or UUID list",
        json_schema_extra={"aliases": ["recipient", "to", "targets", "target"]},
    )
    message: str = Field(
        description="Message body",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )
    priority: str = Field(
        default="normal",
        description="'normal' or 'urgent'",
        json_schema_extra={"aliases": ["level"]},
    )

    @field_validator("recipients", mode="before")
    @classmethod
    def _coerce_recipients(cls, v: Any) -> Any:
        return coerce_to_list(v)


@tool(
    "ask_agent",
    "Ask one or more agents and REQUIRE a reply via send_message/ask_agent/notify_agent. "
    "Use for tool checks, opinions, reports. Prefer this over send_message(expectReport=true).",
    requires_workspace=False,
    security_level="standard",
)
async def ask_agent_tool(
    params: AskNotifyParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    from hiveweave.tools.orchestration_tools import _send_message_core

    return await _send_message_core(
        agent_id=agent_id,
        recipients=params.recipients,
        message=params.message,
        priority=params.priority,
        expect_report=True,
        ctx=ctx,
        message_type="ask",
    )


@tool(
    "notify_agent",
    "Notify agents (FYI) — does NOT require a reply. "
    "Use for status broadcasts. Prefer this over send_message for one-way updates.",
    requires_workspace=False,
    security_level="standard",
)
async def notify_agent_tool(
    params: AskNotifyParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    from hiveweave.tools.orchestration_tools import _send_message_core

    return await _send_message_core(
        agent_id=agent_id,
        recipients=params.recipients,
        message=params.message,
        priority=params.priority,
        expect_report=False,
        ctx=ctx,
        message_type="notify",
    )
