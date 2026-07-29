"""claim / update_task_status / update_progress tools

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# ── claim_task ──────────────────────────────────────────


class ClaimTaskParams(BaseModel):
    """Parameters for claim_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to claim.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


@tool(
    "claim_task",
    "Pick up an unassigned draft (created → claimed). Not needed when create_task/"
    "dispatch already set assigneeId — assign is claim.",
    requires_workspace=False,
    security_level="standard",
)
async def claim_task_tool(
    params: ClaimTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Claim a task."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = _task_svc.TaskService()
        await ts.claim_task(project_id, params.task_id, agent_id)
        return ToolResult.ok(f"Task {params.task_id} claimed by you.")
    except Exception as e:
        return ToolResult.err(f"Failed to claim task: {e}")


# ── update_task_status ──────────────────────────────────


class UpdateTaskStatusParams(BaseModel):
    """Parameters for update_task_status tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to update.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    status: str = Field(
        default="running",
        description="New status: 'running' (start/unblock) or 'blocked'. Defaults to 'running'.",
        json_schema_extra={"aliases": ["status", "state"]},
    )
    blocked_reason: str | None = Field(
        default=None,
        alias="blockedReason",
        description=(
            "Required when blocked. Prefer typed prefixes: "
            "dependency:<taskId|why>, timer:<why>, user:<why>, external:<why>."
        ),
        json_schema_extra={"aliases": ["blockedReason", "blocked_reason", "reason"]},
    )
    depends_on_task_id: str | None = Field(
        default=None,
        alias="dependsOnTaskId",
        description=(
            "When blocking on a dependency, the blocker task id. Enables "
            "auto-unblock when that task is approved/closed."
        ),
        json_schema_extra={
            "aliases": ["dependsOnTaskId", "depends_on_task_id", "dependsOn"]
        },
    )


@tool(
    "update_task_status",
    "Set task to 'running' (start/unblock) or 'blocked'. For 'running', "
    "unblocks if currently blocked (clears wait metadata), else starts.",
    requires_workspace=False,
    security_level="standard",
)
async def update_task_status_tool(
    params: UpdateTaskStatusParams, agent_id: str, workspace: str
) -> ToolResult:
    """Update task status (running or blocked)."""
    status = params.status.lower()
    if status not in ("running", "blocked"):
        return ToolResult.err(
            "update_task_status requires 'status' of 'running' or 'blocked'"
        )

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = _task_svc.TaskService()
    try:
        if status == "blocked":
            reason = params.blocked_reason or "Blocked by agent"
            dep = params.depends_on_task_id
            await ts.block_task(
                project_id, params.task_id, reason, depends_on_task_id=dep
            )
            warn = ""
            if reason.strip().lower().startswith("dependency:"):
                import re as _re
                has_id = bool(_re.search(r"[0-9a-fA-F-]{8,}", reason))
                if not dep and not has_id:
                    warn = (
                        " WARNING: dependency block without task id — "
                        "cannot auto-wake; pass dependsOnTaskId or include "
                        "the blocker task id in blockedReason."
                    )
            return ToolResult.ok(
                f"Task {params.task_id} blocked: {reason}{warn}"
            )
        # status == "running": deterministic by current state (TEST11 #5-L1)
        cur = await ts.get_task(project_id, params.task_id)
        cur_status = (cur or {}).get("status")
        if cur_status == "blocked":
            await ts.unblock_task(project_id, params.task_id)
            return ToolResult.ok(
                f"Task {params.task_id} unblocked (running)."
            )
        await ts.start_task(project_id, params.task_id)
        return ToolResult.ok(f"Task {params.task_id} started (running).")
    except Exception as e:
        return ToolResult.err(f"Failed to update task status: {e}")


# ── update_progress ─────────────────────────────────────


class UpdateProgressParams(BaseModel):
    """Parameters for update_progress tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to update.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )
    progress: int = Field(
        description="Progress percentage (0-100).",
        json_schema_extra={"aliases": ["progress", "percent"]},
    )


@tool(
    "update_progress",
    "Set task progress (0-100). Does not change task status.",
    requires_workspace=False,
    security_level="standard",
)
async def update_progress_tool(
    params: UpdateProgressParams, agent_id: str, workspace: str
) -> ToolResult:
    """Update task progress."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = _task_svc.TaskService()
        await ts.update_progress(project_id, params.task_id, params.progress)
        return ToolResult.ok(
            f"Task {params.task_id} progress set to {params.progress}%."
        )
    except Exception as e:
        return ToolResult.err(f"Failed to update progress: {e}")

