"""cancel_task / unclaim_task / reassign_task

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

# ── cancel_task / unclaim_task ────────────────────────────────


class CancelTaskParams(BaseModel):
    """Parameters for cancel_task tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to cancel/archive.",
        json_schema_extra={
            "aliases": ["taskId", "task_id", "id"],
        },
    )
    reason: str = Field(
        description="Why this task is being cancelled (required, for audit).",
        json_schema_extra={
            "aliases": [
                "reason",
                "feedback",
                "comment",
                "description",
                "message",
                "why",
                "note",
            ],
        },
    )


@tool(
    "cancel_task",
    "Cancel/archive a task that was created by mistake or is no longer needed "
    "(coordinator only). Archived tasks disappear from all task lists and "
    "obligations. Use for mis-assigned or obsolete tasks instead of leaving "
    "them stuck in claimed/blocked forever.",
    requires_workspace=False,
    security_level="standard",
)
async def cancel_task_tool(
    params: CancelTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """废弃误建/误绑/过时的任务（可审计的正式通道）。

    背景：此前没有废弃路径，误绑 task 永远卡在 claimed（井字棋实测 #5）。
    """
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err("cancel_task requires a non-empty 'reason'.")

    ts = _task_svc.TaskService()
    # Soft unblock forbid: do not cancel merely to escape a review deadlock.
    try:
        task_row = await ts.get_task(project_id, params.task_id)
    except Exception:
        task_row = None
    if task_row:
        from hiveweave.services.unblock_soft import (
            cancel_allowed_due_to_approve_deadlock,
            review_deadlock_blocks_cancel,
        )

        forbid = await review_deadlock_blocks_cancel(
            project_id, task_row, cancel_reason=reason
        )
        if forbid:
            return ToolResult.err(forbid)
        deadlock_escape = await cancel_allowed_due_to_approve_deadlock(
            project_id, task_row
        )
    else:
        deadlock_escape = False

    try:
        from_status = await ts.archive_task(
            project_id,
            params.task_id,
            archived_by=agent_id,
            reason=reason,
            reason_code=(
                "cancelled_in_deadlock" if deadlock_escape else "agent_cancel"
            ),
        )
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"Failed to cancel task: {e}")

    assignee = str((task_row or {}).get("assignee_id") or "").strip()
    if assignee:
        try:
            from hiveweave.services.offturn import reap_offturn_for_task

            await reap_offturn_for_task(assignee, params.task_id)
        except Exception as e:
            log.warning(
                "cancel_task_offturn_reap_failed",
                task_id=params.task_id,
                error=str(e),
            )

    # Stamp evidence when escaping an approve-path deadlock (TEST6 S7).
    if deadlock_escape:
        try:
            import json as _json

            after = await ts.get_task(project_id, params.task_id)
            ev = (after or {}).get("evidence") or {}
            if isinstance(ev, str):
                try:
                    ev = _json.loads(ev)
                except Exception:
                    ev = {}
            if not isinstance(ev, dict):
                ev = {}
            ev["cancelled_in_deadlock"] = True
            ev["cancelled_in_deadlock_by"] = agent_id
            ev["cancelled_in_deadlock_reason"] = reason[:500]
            from hiveweave.services import task as task_module

            await task_module._execute(
                project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [_json.dumps(ev), int(time.time() * 1000), params.task_id],
            )
        except Exception as e:
            log.warning("cancel_deadlock_stamp_failed", error=str(e))

    suffix = (
        " [cancelled_in_deadlock — no lawful approver existed]"
        if deadlock_escape
        else ""
    )
    return ToolResult.ok(
        f"Task {params.task_id} archived (was '{from_status}'). "
        f"It no longer appears in task lists or obligations.{suffix}"
    )


class UnclaimTaskParams(BaseModel):
    """Parameters for unclaim_task tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to release back to 'created' for reassignment.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )


@tool(
    "unclaim_task",
    "Release a claimed task back to 'created' and clear its assignee "
    "(coordinator only). Use when a task was claimed by the wrong agent: "
    "unclaim, then dispatch to the right one — no zombie task left behind.",
    requires_workspace=False,
    security_level="standard",
)
async def unclaim_task_tool(
    params: UnclaimTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """释放误绑的认领（claimed → created，清空 assignee）。"""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = _task_svc.TaskService()
    try:
        await ts.unclaim_task(project_id, params.task_id)
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"Failed to unclaim task: {e}")
    return ToolResult.ok(
        f"Task {params.task_id} released to 'created' (assignee cleared). "
        f"Dispatch it to the correct agent now."
    )


# ── reassign_task ────────────────────────────────────────


class ReassignTaskParams(BaseModel):
    """Parameters for reassign_task tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="Task to transfer to a new assignee.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    assignee_id: str = Field(
        alias="assigneeId",
        description="New assignee (agent id, short_id, or 花名).",
        json_schema_extra={"aliases": ["assigneeId", "assignee_id", "to"]},
    )
    reason: str = Field(
        default="",
        description="Optional audit reason (e.g. 'VERIFY executor must be QA').",
    )


@tool(
    "reassign_task",
    "Transfer a task's assignee to another agent and put it on their "
    "obligation ledger (coordinator/CEO). Use this instead of NL "
    "'forward' — messages alone create no obligation. New assignee is "
    "woken with a task message. Note: reassigning a queued VERIFY keeps "
    "it queued (serialized verification) — nudge wakes it when MAIN is free.",
    requires_workspace=False,
    security_level="standard",
)
async def reassign_task_tool(
    params: ReassignTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Structured task transfer — TEST13 P0-2 fix for forward deadlock."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    from hiveweave.services.org import OrgService

    org = OrgService()
    # 40 轮 P0-4：改派必须接 resolve_agent_ref（id/short_id/花名/唯一前缀）——
    # 旧实现用 resolve_agent 只认 id/short_id，传花名报「Assignee not found」
    # 而同秒隔壁工具传花名成功（3 次失败 vs dispatch 4 次成功）。
    target = await org.resolve_agent_ref(project_id, params.assignee_id)
    if not target or target.get("project_id") != project_id:
        # 审计收尾：归档者单独识别，不复用 not-found 措辞
        legacy = await org.resolve_agent(params.assignee_id)
        if legacy and (legacy.get("project_id") or "") == project_id \
                and (legacy.get("status") or "") == "archived":
            return ToolResult.err(
                f"该成员已归档，不可作为改派目标: {params.assignee_id}"
            )
        return ToolResult.err(
            f"Assignee not found in this project: {params.assignee_id}"
        )
    if (target.get("status") or "active") == "archived":
        return ToolResult.err("Cannot reassign to an archived agent")

    ts = _task_svc.TaskService()
    try:
        info = await ts.reassign_task(
            project_id,
            params.task_id,
            new_assignee_id=str(target["id"]),
            reassigned_by=agent_id,
            reason=params.reason or "",
        )
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"Failed to reassign: {e}")

    # Wake new assignee with structured facts
    try:
        from hiveweave.agents.trigger import trigger_subordinate
        from hiveweave.services.inbox import InboxService

        tid = info["task_id"]
        task = await ts.get_task(project_id, tid)
        title = (task or {}).get("title") or tid[:8]
        await InboxService().send_message(
            from_agent_id=agent_id,
            to_agent_id=str(target["id"]),
            message=(
                f"[TASK REASSIGNED] You are now the assignee of "
                f"'{title[:80]}' (taskId={tid}, status={info.get('status')}). "
                f"Claim/continue work, then submit_task. "
                f"Do not wait for a natural-language forward."
            ),
            message_type="task",
            priority="urgent",
            task_id=tid,
            wake=True,
            expect_report=False,
        )
        await trigger_subordinate(str(target["id"]))
    except Exception as e:
        log.warning("reassign_notify_failed", error=str(e))

    name = target.get("name") or target.get("short_id") or params.assignee_id
    return ToolResult.ok(
        f"Task {info['task_id'][:8]} reassigned → {name} "
        f"({target.get('short_id')}, status={info.get('status')}). "
        f"They have been woken; NL forward is not required."
    )

