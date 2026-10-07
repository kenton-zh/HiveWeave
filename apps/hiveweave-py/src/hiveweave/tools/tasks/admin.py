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


# ── remove_task_dependency（批26 修复3：depends_on 摘边）──────────


class RemoveTaskDependencyParams(BaseModel):
    """Parameters for remove_task_dependency tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="The task whose dependency edge should be removed.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    dep_task_id: str = Field(
        alias="depTaskId",
        description=(
            "The dependency edge to remove: the task id this task wrongly "
            "depends on (full id or unique short prefix)."
        ),
        json_schema_extra={
            "aliases": [
                "depTaskId", "dep_task_id", "dependency", "dependencyTaskId",
                "dependency_task_id", "removeDependency", "remove_dependency",
                "dep", "edge",
            ]
        },
    )
    reason: str = Field(
        default="",
        description="Optional audit note (e.g. 'cycle a↔b built by mistake').",
    )


@tool(
    "remove_task_dependency",
    "Remove a wrongly-created depends_on edge from a task (task creator or "
    "CEO only). Use when a dependency was built wrong (cycle / dead "
    "dependency) — the platform has no other edge-removal path, and a bad "
    "edge otherwise parks the task forever. Idempotent: removing an edge "
    "that does not exist succeeds as a no-op. If the task was blocked only "
    "on dependencies, it auto-unblocks and the assignee is woken.",
    requires_workspace=False,
    security_level="standard",
)
async def remove_task_dependency_tool(
    params: RemoveTaskDependencyParams, agent_id: str, workspace: str
) -> ToolResult:
    """摘除建错的 depends_on 边（creator / CEO 专用，幂等）。"""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = _task_svc.TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception:
        task = None
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    # 权限门：仅任务 creator 或 CEO（batch26 修复3 钦定）。
    if agent_id != str(task.get("creator_id") or ""):
        from hiveweave.services.org import OrgService
        from hiveweave.services.policy import infer_role_family

        me = await OrgService().resolve_agent(agent_id)
        family = infer_role_family(me or {})
        if family != "ceo":
            return ToolResult.err(
                "拒绝摘边：只有任务 creator 或 CEO 可以摘除 depends_on 边"
                f"（你的角色={family or 'unknown'}，creator="
                f"{str(task.get('creator_id') or '')[:8]}…）。"
                "请让 creator 或 CEO 执行；等人等待请用 wait_on(kind=agent)，"
                "不要用依赖边表达。"
            )

    try:
        info = await ts.remove_dependency_edge(
            project_id, params.task_id, params.dep_task_id,
            removed_by=agent_id,
        )
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"Failed to remove dependency: {e}")

    if not info.get("removed"):
        return ToolResult.ok(
            f"Dependency {params.dep_task_id} is not in task {params.task_id}'s "
            f"depends_on (idempotent no-op). Current depends_on="
            f"{info.get('depends_on') or []}, status={info.get('status')}."
        )

    output = (
        f"Dependency edge {params.dep_task_id} removed from task "
        f"{params.task_id} (remaining depends_on={info.get('depends_on') or []})."
    )
    if info.get("unblocked"):
        output += " Task auto-unblocked (no unmet dependency remains)."
        assignee = str(task.get("assignee_id") or "").strip()
        if assignee and assignee != agent_id:
            try:
                from hiveweave.agents.trigger import trigger_subordinate
                from hiveweave.services.inbox import InboxService

                await InboxService().send_message(
                    "system",
                    assignee,
                    (
                        f"[DEPENDENCY EDGE REMOVED] 任务 {params.task_id[:8]} "
                        f"「{str(task.get('title') or '')[:60]}」的一条依赖边"
                        "已被摘除，阻塞解除。请继续推进并 submit_task。"
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=params.task_id,
                    wake=True,
                )
                await trigger_subordinate(assignee)
                output += " Assignee woken."
            except Exception as e:
                log.warning("remove_dependency_wake_failed", error=str(e))
    else:
        cur_status = str(info.get("status") or "")
        if cur_status == "blocked":
            output += (
                " Task remains blocked (still-waiting timer/person wait, "
                "remaining unmet dependency, or VERIFY serialization) — "
                "reconcile keeps managing it."
            )
        else:
            output += f" Task status={cur_status}."
    return ToolResult.ok(output)


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

