"""Task Ledger tool implementations — extracted from ToolExecutor.

These methods bridge LLM tool calls to TaskService / DispatchService methods.
State machine: created -> claimed -> running -> blocked/submitted ->
reviewing -> approved/rework -> closed.

The host class (ToolExecutor) must provide:
- _error(message: str) -> dict
- _get_project_id(agent_id: str) -> str | None
- _resolve_agent_id(project_id: str, name_or_id: str) -> str | None
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import structlog
from pydantic import field_validator

from hiveweave.services.task import TaskService
from hiveweave.tools.helpers import coerce_to_list as _coerce_to_list

log = structlog.get_logger(__name__)


class _TaskToolHost(Protocol):
    """Protocol describing the host interface TaskToolsMixin depends on."""

    def _error(self, message: str) -> dict: ...

    async def _get_project_id(self, agent_id: str) -> str | None: ...

    async def _resolve_agent_id(
        self, project_id: str, name_or_id: str
    ) -> str | None: ...


class TaskToolsMixin:
    """Task Ledger tool methods, mixed into ToolExecutor.

    Depends on the host class providing:
    - _error(message: str) -> dict
    - _get_project_id(agent_id: str) -> str | None
    - _resolve_agent_id(project_id: str, name_or_id: str) -> str | None
    """

    # Stub methods — overridden by ToolExecutor at runtime.
    # These exist so mypy can resolve attribute access without circular imports.

    def _error(self, message: str) -> dict:
        raise NotImplementedError

    async def _get_project_id(self, agent_id: str) -> str | None:
        raise NotImplementedError

    async def _resolve_agent_id(
        self, project_id: str, name_or_id: str
    ) -> str | None:
        raise NotImplementedError

    # ── dispatch_task ──────────────────────────────────────

    async def _tool_dispatch_task(
        self, agent_id: str, args: dict
    ) -> dict:
        """Dispatch a task to a subordinate."""
        target = (
            args.get("target") or args.get("agentId") or args.get("subordinate") or ""
        )
        task = args.get("task") or args.get("description") or ""
        expect_report = args.get("expectReport") or args.get("expect_report") or False
        existing_task_id = args.get("taskId") or args.get("task_id") or None
        if not target or not task:
            return self._error("dispatch_task requires 'target' and 'task'")
        from hiveweave.services.dispatch import DispatchService

        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        # Resolve target: agent name / short_id / UUID -> real agent_id
        resolved_id = await self._resolve_agent_id(project_id, target)
        if not resolved_id:
            return self._error(
                f"Cannot resolve target agent '{target}'. "
                f"Use the agent's name, short_id (e.g. A009), or UUID."
            )
        ds = DispatchService()
        result = await ds.dispatch_task(
            project_id=project_id,
            from_agent_id=agent_id,
            to_agent_id=resolved_id,
            description=task,
            expect_report=expect_report,
            existing_task_id=existing_task_id,
        )
        if result.get("success"):
            return {
                "success": True,
                "output": (
                    f"Task dispatched to "
                    f"{result.get('to_agent_id', resolved_id)} "
                    f"(task_id={result.get('task_id', '')})"
                ),
                "error": None,
                "task_id": result.get("task_id"),
            }
        return self._error(result.get("message", "Dispatch failed"))

    # ── Task Ledger tools ─────────────────────────────────
    # State machine:
    # created -> claimed -> running -> blocked/submitted -> reviewing ->
    # approved/rework -> closed. report_completion / approve_work /
    # reject_work are kept for backward compat but deprecated.

    async def _tool_create_task(
        self, agent_id: str, args: dict
    ) -> dict:
        """create_task: create a new task in the Task Ledger (status=created)."""
        title = args.get("title") or ""
        description = args.get("description") or ""
        if not title or not description:
            return self._error("create_task requires 'title' and 'description'")
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        priority = args.get("priority")
        due_at = args.get("dueAt")
        assignee_id = args.get("assigneeId")
        acceptance_criteria = args.get("acceptanceCriteria")
        parent_task_id = args.get("parentTaskId")
        depends_on = args.get("dependsOn")
        expected_modules = args.get("expectedModules")
        tags = args.get("tags")
        # Resolve assignee: name / short_id / UUID -> real agent_id
        if assignee_id:
            resolved = await self._resolve_agent_id(project_id, assignee_id)
            if resolved:
                assignee_id = resolved
        try:
            ts = TaskService()
            task_id = await ts.create_task(
                project_id=project_id,
                title=title,
                description=description,
                creator_id=agent_id,
                assignee_id=assignee_id,
                priority=int(priority) if priority is not None else 2,
                due_at=int(due_at) if due_at is not None else None,
                acceptance_criteria=acceptance_criteria,
                parent_task_id=parent_task_id,
                depends_on=depends_on,
                expected_modules=expected_modules,
                tags=tags,
                source="agent",
            )
            return {
                "success": True,
                "task_id": task_id,
                "output": f"Task created (id={task_id}): {title}",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to create task: {e}")

    async def _tool_claim_task(
        self, agent_id: str, args: dict
    ) -> dict:
        """claim_task: claim a task (created -> claimed), setting self as assignee."""
        task_id = args.get("taskId") or ""
        if not task_id:
            return self._error("claim_task requires 'taskId'")
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        try:
            ts = TaskService()
            await ts.claim_task(project_id, task_id, agent_id)
            return {
                "success": True,
                "output": f"Task {task_id} claimed by you.",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to claim task: {e}")

    async def _tool_update_task_status(
        self, agent_id: str, args: dict
    ) -> dict:
        """update_task_status: set task to 'running' (start/unblock) or 'blocked'.

        Simplified: LLM doesn't know the current state, so for 'running' we
        try start_task (claimed -> running) first, then fall back to
        unblock_task (blocked -> running).
        """
        task_id = args.get("taskId") or ""
        status = (args.get("status") or "").lower()
        if not task_id:
            return self._error("update_task_status requires 'taskId'")
        if status not in ("running", "blocked"):
            return self._error(
                "update_task_status requires 'status' of 'running' or 'blocked'"
            )
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        ts = TaskService()
        try:
            if status == "blocked":
                reason = (
                    args.get("blockedReason")
                    or args.get("blocked_reason")
                    or "Blocked by agent"
                )
                await ts.block_task(project_id, task_id, reason)
                return {
                    "success": True,
                    "output": f"Task {task_id} blocked: {reason}",
                    "error": None,
                }
            # status == "running": try start (claimed->running), fallback unblock
            try:
                await ts.start_task(project_id, task_id)
                return {
                    "success": True,
                    "output": f"Task {task_id} started (running).",
                    "error": None,
                }
            except ValueError:
                # Not in 'claimed' state -- try unblock (blocked -> running)
                await ts.unblock_task(project_id, task_id)
                return {
                    "success": True,
                    "output": f"Task {task_id} unblocked (running).",
                    "error": None,
                }
        except Exception as e:
            return self._error(f"Failed to update task status: {e}")

    async def _tool_update_progress(
        self, agent_id: str, args: dict
    ) -> dict:
        """update_progress: set task progress (0-100). Does not change status."""
        task_id = args.get("taskId") or ""
        if not task_id:
            return self._error("update_progress requires 'taskId'")
        progress = args.get("progress")
        if progress is None:
            return self._error("update_progress requires 'progress' (0-100)")
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        try:
            ts = TaskService()
            await ts.update_progress(project_id, task_id, int(progress))
            return {
                "success": True,
                "output": f"Task {task_id} progress set to {int(progress)}%.",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to update progress: {e}")

    async def _tool_submit_task(
        self, agent_id: str, args: dict
    ) -> dict:
        """submit_task: submit a task for review (running -> submitted).

        Replaces deprecated report_completion. Builds evidence dict and
        attaches to the task.
        """
        task_id = args.get("taskId") or ""
        summary = args.get("summary") or ""
        if not task_id or not summary:
            return self._error("submit_task requires 'taskId' and 'summary'")
        if args.get("testsPassed") is not True:
            return self._error(
                "submit_task requires testsPassed=true after running real tests"
            )
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        evidence: dict = {"summary": summary, "tests_passed": True}
        if args.get("commit"):
            evidence["commit"] = args["commit"]
        if args.get("filesChanged"):
            from hiveweave.services.worktree_review import normalize_files_changed

            evidence["files_changed"] = normalize_files_changed(
                args["filesChanged"]
                if isinstance(args["filesChanged"], list)
                else [args["filesChanged"]]
            )
        if args.get("testOutput"):
            evidence["test_output"] = str(args["testOutput"])[:4000]
        try:
            ts = TaskService()
            await ts.submit_task(project_id, task_id, evidence)
            return {
                "success": True,
                "message": "Task submitted for review",
                "output": f"Task {task_id} submitted for review.",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to submit task: {e}")

    async def _tool_review_task(
        self, agent_id: str, args: dict
    ) -> dict:
        """review_task: review a submitted task (reviewing -> approved/rework).

        Replaces deprecated approve_work / reject_work. Accepts tasks in either
        'submitted' or 'reviewing' state; if still 'submitted', start_review is
        invoked automatically to push it into 'reviewing' before the decision.
        """
        task_id = args.get("taskId") or ""
        decision = (args.get("decision") or "").lower()
        if not task_id:
            return self._error("review_task requires 'taskId'")
        if decision not in ("approve", "rework"):
            return self._error(
                "review_task requires 'decision' of 'approve' or 'rework'"
            )
        feedback = args.get("feedback")
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        try:
            ts = TaskService()
            # Check current status and only call start_review when needed,
            # instead of try/except pass which silently swallows real errors.
            task = await ts.get_task(project_id, task_id)
            if not task:
                return self._error(f"Task not found: {task_id}")
            current_status = task["status"]
            if current_status == "submitted":
                await ts.start_review(project_id, task_id)  # submitted -> reviewing
            elif current_status != "reviewing":
                return self._error(
                    f"Task must be 'submitted' or 'reviewing' to review, "
                    f"but is '{current_status}'"
                )
            await ts.review_task(project_id, task_id, decision, feedback)
            if decision == "approve":
                return {
                    "success": True,
                    "message": "Task approved",
                    "output": f"Task {task_id} approved.",
                    "error": None,
                }
            return {
                "success": True,
                "message": "Task sent back for rework",
                "output": f"Task {task_id} sent back for rework.",
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to review task: {e}")

    async def _tool_get_tasks(
        self, agent_id: str, args: dict
    ) -> dict:
        """get_tasks: list tasks in the Task Ledger with optional filters."""
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        status = args.get("status")
        assignee_id = args.get("assigneeId")
        try:
            ts = TaskService()
            tasks = await ts.list_tasks(
                project_id, status=status, assignee_id=assignee_id
            )
            if not tasks:
                return {
                    "success": True,
                    "tasks": [],
                    "output": "No tasks found matching the filters.",
                    "error": None,
                }
            lines = []
            for t in tasks:
                lines.append(
                    f"- [{t.get('status', '?')}] {t.get('title', '?')} "
                    f"(id={t.get('id', '?')}, "
                    f"progress={t.get('progress', 0)}%, "
                    f"assignee={t.get('assignee_id') or 'unassigned'})"
                )
            return {
                "success": True,
                "tasks": tasks,
                "output": f"Tasks ({len(tasks)}):\n" + "\n".join(lines),
                "error": None,
            }
        except Exception as e:
            return self._error(f"Failed to list tasks: {e}")


# ── Pydantic models + @tool registration (Phase 3 migration) ──────
#
# These @tool-registered functions mirror the TaskToolsMixin methods above,
# using the new typed-pipeline architecture:
#   - Pydantic BaseModel for parameter validation + alias normalization
#   - ToolResult.ok() / ToolResult.err() for return values
#   - helpers.get_project_id / helpers.resolve_agent_id instead of self._*
# The legacy TaskToolsMixin is left untouched for backward compatibility.

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult
from .helpers import get_project_id, resolve_agent_id


# ── dispatch_task ───────────────────────────────────────

# 只读协调角色 — 硬拒绝派活（不再只是软提醒）
_COORDINATOR_ASSIGNEE_BLOCK = (
    "拒绝派活：对方是 coordinator（只读协调角色），不能承接改代码任务。"
    "请改派 executor（工程师/QA 等可写角色），或让对方再 dispatch 给下属。"
)

# 保留常量名供旧测试/文档引用；语义已改为硬门文案前缀
_READONLY_ASSIGNEE_REMINDER = _COORDINATOR_ASSIGNEE_BLOCK


async def _get_assignee_permission_type(
    agent_id: str, org_service: Any = None
) -> str | None:
    """只读查询 assignee 的 permission_type（per-project DB agents 表）。

    返回小写 permission_type（如 "coordinator" / "executor"）；查无此人或
    查询失败时返回 None。
    """
    try:
        if org_service is None:
            from hiveweave.services.org import OrgService
            org_service = OrgService()
        agent = await org_service.get_agent(agent_id)
        if not agent:
            return None
        perm = (agent.get("permission_type") or "").strip().lower()
        return perm or None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dispatch_assignee_permission_lookup_failed",
            agent_id=agent_id,
            error=str(exc),
        )
        return None


class DispatchTaskParams(BaseModel):
    """Parameters for dispatch_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    target: str = Field(
        description="Target agent: name, short_id (e.g. A009), or UUID.",
        json_schema_extra={"aliases": ["agentId", "subordinate", "agent_id", "to"]},
    )
    task: str = Field(
        description="Task description to dispatch to the subordinate.",
        json_schema_extra={"aliases": ["description", "desc"]},
    )
    expect_report: bool = Field(
        default=False,
        alias="expectReport",
        description="Whether to expect a report back from the subordinate.",
        json_schema_extra={"aliases": ["expectReport", "expect_report"]},
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="Existing task ID to reuse (optional).",
        json_schema_extra={"aliases": ["taskId", "task_id", "existingTaskId", "existing_task_id"]},
    )


@tool(
    "dispatch_task",
    "Deliver work NOW: ledger entry + inbox wake. Pass taskId if create_task already drafted/queued it. "
    "Only direct reports; never assign coordinators code work.",
    requires_workspace=False,
    security_level="standard",
)
async def dispatch_task_tool(
    params: DispatchTaskParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Dispatch a task to a subordinate."""
    from hiveweave.services.dispatch import DispatchService
    from hiveweave.services.org_span import (
        validate_ceo_dispatch_target,
        validate_dispatch_span,
        validate_executor_assignee,
    )
    from hiveweave.services.task import TaskService

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    org_service = ctx.org if ctx else None
    resolved_id = await resolve_agent_id(project_id, params.target, org_service)
    if not resolved_id:
        return ToolResult.err(
            f"Cannot resolve target agent '{params.target}'. "
            f"Use the agent's name, short_id (e.g. A009), or UUID."
        )

    span_err = await validate_dispatch_span(agent_id, resolved_id, org_service)
    if span_err:
        return ToolResult.err(span_err)

    ceo_err = await validate_ceo_dispatch_target(agent_id, resolved_id, org_service)
    if ceo_err:
        return ToolResult.err(ceo_err)

    coord_err = await validate_executor_assignee(resolved_id, org_service)
    if coord_err:
        return ToolResult.err(coord_err)

    # Dedup: same assignee + similar title already open → reuse via taskId hint
    if not params.task_id:
        try:
            dup = await TaskService().find_similar_open_task(
                project_id, params.task[:100], assignee_id=resolved_id
            )
            if dup:
                return ToolResult.err(
                    f"已有相似未完成任务 id={dup['id']} status={dup.get('status')} "
                    f"title={dup.get('title', '')[:60]!r}。"
                    f"请复用：dispatch_task(..., taskId=\"{dup['id']}\")，"
                    f"或先 cancel_task(taskId=\"{dup['id']}\", reason=\"...\") 再新建。"
                )
        except Exception as e:
            log.warning("dispatch_dedup_check_failed", error=str(e))

    ds = DispatchService()
    result = await ds.dispatch_task(
        project_id=project_id,
        from_agent_id=agent_id,
        to_agent_id=resolved_id,
        description=params.task,
        expect_report=params.expect_report,
        existing_task_id=params.task_id,
    )
    if result.get("success"):
        # Align with review_task: inbox alone is not enough — wake assignee
        try:
            from hiveweave.agents.trigger import trigger_subordinate

            await trigger_subordinate(resolved_id)
        except Exception as e:
            log.warning(
                "dispatch_trigger_failed",
                target=resolved_id,
                error=str(e),
            )
        output = (
            f"Task dispatched to {result.get('to_agent_id', resolved_id)} "
            f"(task_id={result.get('task_id', '')})"
        )
        return ToolResult.ok(output, task_id=result.get("task_id"))
    return ToolResult.err(result.get("message", "Dispatch failed"))


# ── create_task ─────────────────────────────────────────


class CreateTaskParams(BaseModel):
    """Parameters for create_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(
        description="Task title.",
    )
    description: str = Field(
        description="Task description.",
    )
    priority: int = Field(
        default=2,
        description="Task priority (1=high, 2=normal, 3=low).",
    )
    due_at: int | None = Field(
        default=None,
        alias="dueAt",
        description="Due timestamp in milliseconds (optional).",
        json_schema_extra={"aliases": ["dueAt", "due_at", "deadline"]},
    )
    assignee_id: str | None = Field(
        default=None,
        alias="assigneeId",
        description="Assignee agent ID, name, or short_id (optional).",
        json_schema_extra={"aliases": ["assigneeId", "assignee_id", "assignee"]},
    )
    acceptance_criteria: list[Any] | None = Field(
        default=None,
        alias="acceptanceCriteria",
        description="Acceptance criteria list (optional).",
        json_schema_extra={"aliases": ["acceptanceCriteria", "acceptance_criteria"]},
    )
    parent_task_id: str | None = Field(
        default=None,
        alias="parentTaskId",
        description="Parent task ID (optional).",
        json_schema_extra={"aliases": ["parentTaskId", "parent_task_id"]},
    )
    depends_on: list[str] | None = Field(
        default=None,
        alias="dependsOn",
        description="List of task IDs this task depends on (optional).",
        json_schema_extra={"aliases": ["dependsOn", "depends_on"]},
    )
    expected_modules: list[str] | None = Field(
        default=None,
        alias="expectedModules",
        description="Expected modules (optional).",
        json_schema_extra={"aliases": ["expectedModules", "expected_modules"]},
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tags for the task (optional).",
        json_schema_extra={"aliases": ["tags", "tag"]},
    )

    @field_validator(
        "acceptance_criteria", "depends_on", "expected_modules", "tags",
        mode="before",
    )
    @classmethod
    def _coerce_list_fields(cls, v: Any) -> Any:
        return _coerce_to_list(v)


@tool(
    "create_task",
    "Ledger entry. Unassigned → status=created (draft). With assigneeId → claimed "
    "(assign=claim; no separate claim_task). Does NOT wake anyone — call dispatch_task to deliver.",
    requires_workspace=False,
    security_level="standard",
)
async def create_task_tool(
    params: CreateTaskParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a new task."""
    from hiveweave.services.org_span import (
        validate_ceo_dispatch_target,
        validate_dispatch_span,
        validate_executor_assignee,
    )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    assignee_id = params.assignee_id
    org_service = ctx.org if ctx else None
    if assignee_id:
        resolved = await resolve_agent_id(project_id, assignee_id, org_service)
        if resolved:
            assignee_id = resolved
            span_err = await validate_dispatch_span(agent_id, assignee_id, org_service)
            if span_err:
                return ToolResult.err(span_err)
            ceo_err = await validate_ceo_dispatch_target(
                agent_id, assignee_id, org_service
            )
            if ceo_err:
                return ToolResult.err(ceo_err)
            coord_err = await validate_executor_assignee(assignee_id, org_service)
            if coord_err:
                return ToolResult.err(coord_err)
            # builder coordinator / executor assignee 须真正 ensure 成功；
            # 失败只降级为告警日志（任务照建），但绝不静默 pass。
            try:
                from hiveweave.services.git_worktree import ensure_executor_worktree

                ensured = await ensure_executor_worktree(
                    project_id, assignee_id, task_name=params.title
                )
                if not ensured.get("success"):
                    log.warning(
                        "create_task_worktree_ensure_failed",
                        assignee_id=assignee_id,
                        error=ensured.get("message"),
                    )
            except Exception as e:
                log.warning(
                    "create_task_worktree_ensure_error",
                    assignee_id=assignee_id,
                    error=str(e),
                )

    try:
        ts = TaskService()
        dup = await ts.find_similar_open_task(
            project_id, params.title, assignee_id=assignee_id
        )
        if dup:
            return ToolResult.err(
                f"已有相似未完成任务 id={dup['id']} status={dup.get('status')} "
                f"title={dup.get('title', '')[:60]!r}。"
                f"请复用该 taskId 调用 dispatch_task，或先 "
                f"cancel_task(taskId=\"{dup['id']}\", reason=\"重复\")。"
            )
        task_id = await ts.create_task(
            project_id=project_id,
            title=params.title,
            description=params.description,
            creator_id=agent_id,
            assignee_id=assignee_id,
            priority=params.priority,
            due_at=params.due_at,
            acceptance_criteria=params.acceptance_criteria,
            parent_task_id=params.parent_task_id,
            depends_on=params.depends_on,
            expected_modules=params.expected_modules,
            tags=params.tags,
            source="agent",
        )
        task = await ts.get_task(project_id, task_id)
        st = (task or {}).get("status") or "created"
        note = (
            f"status={st} (assign=claim)"
            if assignee_id and st == "claimed"
            else f"status={st}"
        )
        return ToolResult.ok(
            f"Task created (id={task_id}, {note}): {params.title}",
            task_id=task_id,
            status=st,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to create task: {e}")


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
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = TaskService()
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


@tool(
    "update_task_status",
    "Set task to 'running' (start/unblock) or 'blocked'. For 'running', tries start_task first, then falls back to unblock_task.",
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

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = TaskService()
    try:
        if status == "blocked":
            reason = params.blocked_reason or "Blocked by agent"
            await ts.block_task(project_id, params.task_id, reason)
            return ToolResult.ok(f"Task {params.task_id} blocked: {reason}")
        # status == "running": try start (claimed->running), fallback unblock
        try:
            await ts.start_task(project_id, params.task_id)
            return ToolResult.ok(f"Task {params.task_id} started (running).")
        except ValueError:
            # Not in 'claimed' state -- try unblock (blocked -> running)
            await ts.unblock_task(project_id, params.task_id)
            return ToolResult.ok(f"Task {params.task_id} unblocked (running).")
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
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = TaskService()
        await ts.update_progress(project_id, params.task_id, params.progress)
        return ToolResult.ok(
            f"Task {params.task_id} progress set to {params.progress}%."
        )
    except Exception as e:
        return ToolResult.err(f"Failed to update progress: {e}")


# ── submit_task ─────────────────────────────────────────


class SubmitTaskParams(BaseModel):
    """Parameters for submit_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="ID of the task to submit. If omitted, auto-detects your current running task.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    summary: str = Field(
        description="Summary of work done.",
        json_schema_extra={"aliases": ["summary", "report"]},
    )
    commit: str | None = Field(
        default=None,
        description="Git commit hash (optional).",
        json_schema_extra={"aliases": ["commit", "commitHash"]},
    )
    files_changed: list[str] | None = Field(
        default=None,
        alias="filesChanged",
        description="List of changed files (optional).",
        json_schema_extra={"aliases": ["filesChanged", "files_changed", "files"]},
    )
    tests_passed: bool | None = Field(
        default=None,
        alias="testsPassed",
        description=(
            "MANDATORY for code tasks: true only after you actually ran tests "
            "(npm test / pytest / etc.) and they passed. "
            "Documentation/explore-only tasks may set true with summary noting N/A."
        ),
        json_schema_extra={"aliases": ["testsPassed", "tests_passed"]},
    )
    test_output: str | None = Field(
        default=None,
        alias="testOutput",
        description="Brief test command output / proof (recommended).",
        json_schema_extra={"aliases": ["testOutput", "test_output", "testLog"]},
    )
    attestation_ids: list[str] | None = Field(
        default=None,
        alias="attestationIds",
        description=(
            "Server-issued attestation ids from browse/bash test runs. "
            "Required for UI/code tasks (bare testsPassed is rejected)."
        ),
        json_schema_extra={"aliases": ["attestationIds", "attestation_ids"]},
    )

    @field_validator("files_changed", mode="before")
    @classmethod
    def _coerce_files_changed(cls, v: Any) -> Any:
        return _coerce_to_list(v)

    @field_validator("attestation_ids", mode="before")
    @classmethod
    def _coerce_attestation_ids(cls, v: Any) -> Any:
        return _coerce_to_list(v)


@tool(
    "submit_task",
    "Submit a task for review (running -> submitted). Requires server "
    "attestationIds from browse (UI) or bash test runs (code). "
    "docs/explore tasks may use tags docs/explore. "
    "If taskId omitted, auto-detects your current running task.",
    requires_workspace=False,
    security_level="standard",
)
async def submit_task_tool(
    params: SubmitTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Submit a task for review."""
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    task_id = params.task_id
    ts = TaskService()
    if not task_id:
        tasks = await ts.list_tasks(project_id, assignee_id=agent_id)
        active = [t for t in tasks if t.get("status") in ("running", "claimed")]
        if not active:
            return ToolResult.err(
                "submit_task requires 'taskId'. No active task found for your agent. "
                "Call get_tasks to find your tasks, then pass taskId."
            )
        if len(active) > 1:
            task_list = ", ".join(
                f"{t['id'][:8]} ({t.get('title', '?')})" for t in active
            )
            return ToolResult.err(
                f"Multiple active tasks found: {task_list}. "
                "Please specify which taskId to submit."
            )
        task_id = active[0]["id"]

    task = await ts.get_task(project_id, task_id)
    if not task:
        return ToolResult.err(f"Task not found: {task_id}")

    from hiveweave.services.attestation import (
        attestation_service,
        required_attestation_kinds,
        resolve_task_policy,
    )

    tags = task.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    policy_id = (
        task.get("policy_id")
        or resolve_task_policy(
            title=task.get("title") or "",
            tags=tags if isinstance(tags, list) else [],
            description=task.get("description") or "",
        )
    )
    needed = required_attestation_kinds(policy_id)
    attest_ids = list(params.attestation_ids or [])

    if needed:
        # Waiver 短路：coordinator 已显式豁免（CLI/脚本类任务正式出口）
        from hiveweave.services.attestation import has_valid_waiver

        if not await has_valid_waiver(project_id, task_id):
            # TEST4: auto-attach recent matching attestations if LLM omitted ids
            if not attest_ids:
                attest_ids = await attestation_service.find_recent_for_agent(
                    project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    kinds=needed,
                )
                if attest_ids:
                    log.info(
                        "submit_task_auto_attached_attestations",
                        agent_id=agent_id,
                        task_id=task_id,
                        count=len(attest_ids),
                    )
            ok, err = await attestation_service.verify_ids(
                project_id,
                attest_ids,
                expected_agent_id=agent_id,
                expected_kinds=needed,
                task_id=task_id,
            )
            if not ok:
                return ToolResult.err(
                    f"submit_task attestation gate failed ({policy_id}): {err}. "
                    f"taskId={task_id} (use this full id).\n"
                    f"Options:\n"
                    f"1) Run bash/tests as the assignee, then "
                    f"submit_task(taskId=\"{task_id}\", attestationIds=[...]).\n"
                    f"2) Coordinator: "
                    f"waive_attestation(taskId=\"{task_id}\", "
                    f"reason=\"<why exempt>\").\n"
                    f"Bare testsPassed is rejected."
                )
    elif params.tests_passed is not True:
        # docs_only still asks for explicit ack
        return ToolResult.err(
            "docs_only submit still requires testsPassed=true "
            "(note N/A in summary)."
        )

    evidence: dict[str, Any] = {
        "summary": params.summary,
        "tests_passed": True,
        "policy_id": policy_id,
        "attestation_ids": attest_ids,
    }
    if params.commit:
        evidence["commit"] = params.commit
    if params.files_changed:
        from hiveweave.services.worktree_review import normalize_files_changed

        evidence["files_changed"] = normalize_files_changed(params.files_changed)
    if params.test_output:
        evidence["test_output"] = params.test_output[:4000]

    try:
        # Auto-transition: if task is in 'created' or 'claimed' status,
        # automatically claim/start it before submitting.
        if task:
            status = task.get("status", "")
            if status == "created":
                await ts.claim_task(project_id, task_id, agent_id)
                await ts.start_task(project_id, task_id)
            elif status == "claimed":
                await ts.start_task(project_id, task_id)
        await ts.submit_task(project_id, task_id, evidence)

        # ── 标记 handoff 为已汇报 ──
        # submit_task 即"向上汇报"，清除 expect_report 义务
        try:
            from hiveweave.services.handoff import HandoffService
            hs = HandoffService()
            cnt = await hs.mark_reported(project_id, agent_id, task_id)
            if cnt:
                log.info("handoff_marked_reported", agent_id=agent_id, task_id=task_id, count=cnt)
        except Exception as e:
            log.warning("handoff_mark_reported_failed", error=str(e))

        # ── 通知 reviewer 有 task 待审 ──
        # 正常路径：wake creator。自交（creator==assignee，如中层自建骨架任务）
        # 时改 wake org parent（中层→CEO），避免「通知自己 + 禁自审」死锁。
        task_after = await ts.get_task(project_id, task_id)
        if task_after and task_after.get("creator_id"):
            creator_id = task_after["creator_id"]
            from hiveweave.services.inbox import InboxService
            inbox = InboxService()
            self_submit = creator_id == agent_id
            notify_id = creator_id
            if self_submit:
                try:
                    from hiveweave.services.org import OrgService

                    me = await OrgService().resolve_agent(agent_id)
                    parent_id = (me or {}).get("parent_id")
                    if parent_id:
                        notify_id = parent_id
                except Exception as e:
                    log.warning("submit_parent_lookup_failed", error=str(e))
            await inbox.send_message(
                from_agent_id=agent_id if not self_submit else "system",
                to_agent_id=notify_id,
                message=(
                    f"[TASK SUBMITTED] Task '{task_after.get('title', '')[:60]}' "
                    f"has been submitted for your review. "
                    f"Use review_task(taskId='{task_id}', decision='approve'/'rework') "
                    f"to review."
                ),
                message_type="task",
                priority="normal",
                task_id=task_id,
                wake=True,
            )
            from hiveweave.agents.trigger import trigger_coordinator
            await trigger_coordinator(notify_id)

        return ToolResult.ok(f"Task {task_id} submitted for review.")
    except Exception as e:
        return ToolResult.err(f"Failed to submit task: {e}")


# ── review_task ─────────────────────────────────────────


class ReviewTaskParams(BaseModel):
    """Parameters for review_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to review.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )
    decision: str = Field(
        description="Review decision: 'approve' or 'rework'.",
        json_schema_extra={"aliases": ["decision", "verdict"]},
    )
    feedback: str | None = Field(
        default=None,
        description="Review feedback (optional).",
        json_schema_extra={"aliases": ["feedback", "comment", "comments"]},
    )


@tool(
    "review_task",
    "Review a submitted task (reviewing -> approved/rework). If task is 'submitted', starts review automatically. "
    "approve requires valid attestation_ids in evidence (not bare testsPassed) and "
    "assignee worktree context; does NOT spawn VERIFY — call git_worktree_merge next; "
    "VERIFY is created only after merge succeeds.",
    requires_workspace=False,
    security_level="standard",
)
async def review_task_tool(
    params: ReviewTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Review a submitted task."""
    decision = params.decision.lower()
    if decision not in ("approve", "rework"):
        return ToolResult.err(
            "review_task requires 'decision' of 'approve' or 'rework'"
        )

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    try:
        ts = TaskService()
        task = await ts.get_task(project_id, params.task_id)
        if not task:
            return ToolResult.err(f"Task not found: {params.task_id}")

        # Hard gate: 禁自审 —— reviewer 不得等于 assignee。
        assignee_id = task.get("assignee_id")
        if assignee_id and str(assignee_id) == str(agent_id):
            return ToolResult.err(
                "Self-review is forbidden: you are the assignee of this task. "
                "Your submission goes to your superior (or task creator) for "
                "review — do not approve your own deliverable."
            )

        # Hard gate: VERIFY 审权不落回实现者/合并人 —— 须 CEO 或独立非实现者。
        if ts._is_verify_task(task):
            forbidden: set[str] = set()
            merged_by = None
            evidence_raw = task.get("evidence") or {}
            if isinstance(evidence_raw, str):
                try:
                    evidence_raw = json.loads(evidence_raw)
                except Exception:
                    evidence_raw = {}
            if isinstance(evidence_raw, dict):
                merged_by = evidence_raw.get("merged_by")
                if merged_by:
                    forbidden.add(str(merged_by))
            parent_assignee = None
            parent_id = task.get("parent_task_id")
            if parent_id:
                parent = await ts.get_task(project_id, parent_id)
                if parent and parent.get("assignee_id"):
                    parent_assignee = parent["assignee_id"]
                    forbidden.add(str(parent_assignee))
            # BUG-P1b: creator_id 不得无差别禁止 —— VERIFY spawn 时
            # creator 恒落到 CEO（见 _spawn_verify_task），无差别加入会让
            # CEO 永远无法审批 VERIFY。仅当 creator 本身就是实现者/合并人
            # 时才禁止（保持"实现者不得自审"的初衷）。
            creator_id = task.get("creator_id")
            if creator_id and (
                str(creator_id) == str(merged_by)
                or str(creator_id) == str(parent_assignee)
            ):
                forbidden.add(str(creator_id))
            if str(agent_id) in forbidden:
                return ToolResult.err(
                    "VERIFY approval must come from the CEO or an independent "
                    "reviewer — the implementer / merger of the parent task "
                    "cannot approve its verification."
                )

        # Phase 3: approve requires attestation evidence
        if decision == "approve":
            from hiveweave.services.attestation import (
                attestation_service,
                required_attestation_kinds,
                resolve_task_policy,
            )
            from hiveweave.services.worktree_review import review_worktree_gate

            evidence = task.get("evidence") or {}
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            tags = task.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            policy_id = (
                evidence.get("policy_id")
                or task.get("policy_id")
                or resolve_task_policy(
                    title=task.get("title") or "",
                    tags=tags if isinstance(tags, list) else [],
                    description=task.get("description") or "",
                )
            )
            needed = required_attestation_kinds(policy_id)
            if needed:
                from hiveweave.services.attestation import has_valid_waiver

                waived = await has_valid_waiver(project_id, params.task_id)
                if not waived:
                    aids = evidence.get("attestation_ids") or []
                    if not isinstance(aids, list):
                        aids = []
                    ok, err = await attestation_service.verify_ids(
                        project_id,
                        [str(x) for x in aids],
                        expected_kinds=needed,
                        task_id=params.task_id,
                    )
                    if not ok:
                        tid = task.get("id") or params.task_id
                        return ToolResult.err(
                            f"Cannot approve: attestation gate failed ({policy_id}): {err}. "
                            f"taskId={tid} (use this full id).\n"
                            f"Options:\n"
                            f"1) Send rework; require assignee to attach real "
                            f"browse/test attestationIds on resubmit.\n"
                            f"2) waive_attestation(taskId=\"{tid}\", "
                            f"reason=\"<why exempt>\") then approve again.\n"
                            f"3) Have an executor/QA run tests and submit "
                            f"attestationIds on this task first."
                        )
            elif evidence.get("tests_passed") is not True:
                return ToolResult.err(
                    "Cannot approve docs_only task without tests_passed=true ack."
                )

            # (1) Force worktree context — ensure assignee tree exists, then gate.
            # builder coordinator / executor assignee 须真正 ensure；失败降级为
            # 告警日志并交给 review_worktree_gate 判定，绝不静默 pass。
            if task.get("assignee_id"):
                try:
                    from hiveweave.services.git_worktree import ensure_executor_worktree

                    ensured = await ensure_executor_worktree(
                        project_id, str(task["assignee_id"]),
                        task_id=params.task_id,  # P0 稳定命名 hw/<sid>/t-<taskid8>
                    )
                    if not ensured.get("success"):
                        log.warning(
                            "review_task_worktree_ensure_failed",
                            task_id=params.task_id,
                            assignee_id=task["assignee_id"],
                            error=ensured.get("message"),
                        )
                except Exception as e:
                    log.warning(
                        "review_task_worktree_ensure_error",
                        task_id=params.task_id,
                        error=str(e),
                    )
            wt_deny, wt_meta = await review_worktree_gate(
                project_id, task, evidence
            )
            if wt_deny:
                return ToolResult.err(wt_deny)

        current_status = task["status"]
        if decision == "approve":
            if current_status == "submitted":
                await ts.start_review(project_id, params.task_id)
            elif current_status != "reviewing":
                return ToolResult.err(
                    f"Task must be 'submitted' or 'reviewing' to approve, "
                    f"but is '{current_status}'"
                )
        else:
            # rework from reviewing (normal) or approved (post-approve merge conflict)
            if current_status == "submitted":
                await ts.start_review(project_id, params.task_id)
            elif current_status not in ("reviewing", "approved"):
                return ToolResult.err(
                    f"Task must be 'submitted', 'reviewing', or 'approved' "
                    f"to rework, but is '{current_status}'"
                )
        await ts.review_task(
            project_id, params.task_id, decision, params.feedback,
            reviewer_id=agent_id,
        )


        # ── 通知 assignee/executor 审查结果 ──
        task_after = await ts.get_task(project_id, params.task_id)
        if task_after and task_after.get("assignee_id"):
            assignee_id = task_after["assignee_id"]
            if assignee_id != agent_id:
                from hiveweave.services.inbox import InboxService
                inbox = InboxService()
                if decision == "approve":
                    from hiveweave.services.worktree_review import agent_worktree_path

                    wt_path = await agent_worktree_path(assignee_id)
                    msg = (
                        f"[TASK APPROVED] Task '{task_after.get('title', '')[:60]}' "
                        f"has been approved. Wait for your coordinator to "
                        f"git_worktree_merge your worktree"
                        f"{f' ({wt_path})' if wt_path else ''}. "
                        f"VERIFY runs only AFTER merge lands on main — do not "
                        f"self-verify. If merge conflicts, you will get rework "
                        f"to rebase/merge main in YOUR worktree (not on main)."
                    )
                    priority = "normal"
                else:
                    feedback = params.feedback or "No specific feedback provided."
                    msg = (
                        f"[REWORK REQUESTED] Task '{task_after.get('title', '')[:60]}' "
                        f"needs rework. Feedback: {feedback}"
                    )
                    priority = "urgent"
                await inbox.send_message(
                    from_agent_id=agent_id,
                    to_agent_id=assignee_id,
                    message=msg,
                    message_type="task",
                    priority=priority,
                    task_id=params.task_id,
                    # Force wake: approve/rework must reach assignee (TEST3).
                    wake=True,
                )
                from hiveweave.agents.trigger import trigger_subordinate
                await trigger_subordinate(assignee_id)

        # (3) Do NOT spawn VERIFY on approve — only after successful merge
        # Exception: VERIFY child approve already closed parent; pure no-diff
        # tasks need no merge.
        if decision == "approve":
            from hiveweave.services.worktree_review import (
                agent_worktree_path,
                worktree_commits_ahead,
                project_main_workspace,
            )

            if ts._is_verify_task(task_after or task):
                return ToolResult.ok(
                    f"VERIFY task {params.task_id} approved — parent closed. "
                    "No git_worktree_merge needed."
                )

            asg = (task_after or {}).get("assignee_id")
            wt = await agent_worktree_path(asg) if asg else None
            main_ws = await project_main_workspace(project_id)
            ahead = (
                await worktree_commits_ahead(main_ws, wt)
                if main_ws and wt
                else None
            )
            short = ""
            if asg:
                try:
                    from hiveweave.services.org import OrgService
                    a = await OrgService().resolve_agent(asg)
                    short = (a or {}).get("short_id") or ""
                except Exception:
                    pass

            # Only auto-close when there is truly nothing to merge: empty
            # files_changed + 0 commits ahead. Non-empty files_changed with
            # ahead==0 usually means uncommitted worktree dirt — do NOT close
            # (merge tip wouldn't pick those up either; executor must checkpoint).
            evidence_after = (task_after or {}).get("evidence") or {}
            if isinstance(evidence_after, str):
                try:
                    evidence_after = json.loads(evidence_after)
                except Exception:
                    evidence_after = {}
            if not isinstance(evidence_after, dict):
                evidence_after = {}
            claimed_files = evidence_after.get("files_changed") or evidence_after.get(
                "filesChanged"
            ) or []
            from hiveweave.services.worktree_review import _rel_paths

            has_claimed_files = bool(_rel_paths(list(claimed_files or [])))

            if ahead == 0 and not has_claimed_files:
                try:
                    await ts.close_task(project_id, params.task_id)
                except Exception as e:
                    log.warning(
                        "approve_no_diff_close_failed",
                        task_id=params.task_id,
                        error=str(e),
                    )
                    return ToolResult.ok(
                        f"Task {params.task_id} approved; worktree already on "
                        f"main (0 commits ahead, no files_changed). "
                        f"Close manually if needed ({e}). No merge required."
                    )
                return ToolResult.ok(
                    f"Task {params.task_id} approved; assignee worktree already "
                    f"matches main (0 commits ahead, no files_changed) — "
                    f"closed without merge. No git_worktree_merge needed."
                )

            if ahead == 0 and has_claimed_files:
                await _inject_merge_pending_wake(
                    project_id=project_id,
                    reviewer_id=agent_id,
                    task=task_after or task,
                    short_id=short,
                    reason="uncommitted_files_changed",
                )
                return ToolResult.ok(
                    f"Task {params.task_id} approved against assignee worktree"
                    f"{f' ({wt})' if wt else ''}, but HEAD is 0 commits ahead "
                    f"of main while evidence.files_changed is non-empty "
                    f"(likely uncommitted changes). Executor must "
                    f"git_worktree_checkpoint before you merge; do NOT treat "
                    f"as already-merged. Next: confirm checkpoint, then "
                    f"git_worktree_merge(branchName='{short or 'hw/<short_id>/...'}')."
                )

            await _inject_merge_pending_wake(
                project_id=project_id,
                reviewer_id=agent_id,
                task=task_after or task,
                short_id=short,
                reason="approved_needs_merge",
            )
            return ToolResult.ok(
                f"Task {params.task_id} approved against assignee worktree"
                f"{f' ({wt})' if wt else ''}. "
                f"VERIFY is NOT created yet. "
                f"Next (YOU, coordinator): git_worktree_merge("
                f"branchName='{short or 'hw/<short_id>/...'}'). "
                f"On real content conflict: rework executor to rebase/merge "
                f"main in their worktree. On untracked-on-main: that is MAIN "
                f"hygiene — retry merge (auto-quarantine), do NOT rework."
            )
        return ToolResult.ok(f"Task {params.task_id} sent back for rework.")
    except Exception as e:
        return ToolResult.err(f"Failed to review task: {e}")


async def _inject_merge_pending_wake(
    *,
    project_id: str,
    reviewer_id: str,
    task: dict,
    short_id: str = "",
    reason: str = "approved_needs_merge",
) -> None:
    """Wake the approving coordinator to git_worktree_merge (same-turn follow-up)."""
    tid = str(task.get("id") or "")
    title = (task.get("title") or "(untitled)").split("\n")[0][:60]
    branch = short_id or "hw/<short_id>/..."
    body = (
        f"[MERGE PENDING] Task '{title}' ({tid[:8]}) is approved and needs "
        f"git_worktree_merge(branchName='{branch}'). "
        f"YOU (coordinator) must merge — do not ask the executor to merge on main. "
        f"reason={reason}"
    )
    try:
        from hiveweave.services.inbox import InboxService

        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=reviewer_id,
            message=body,
            message_type="task",
            priority="urgent",
            task_id=tid or None,
            wake=True,
        )
        from hiveweave.agents.trigger import trigger_coordinator

        await trigger_coordinator(reviewer_id)
        log.info(
            "merge_pending_wake_injected",
            reviewer_id=reviewer_id,
            task_id=tid,
            reason=reason,
        )
    except Exception as e:
        log.warning(
            "merge_pending_wake_failed",
            reviewer_id=reviewer_id,
            task_id=tid,
            error=str(e),
        )


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
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err("cancel_task requires a non-empty 'reason'.")

    ts = TaskService()
    try:
        from_status = await ts.archive_task(
            project_id, params.task_id, archived_by=agent_id, reason=reason
        )
    except ValueError as e:
        return ToolResult.err(str(e))
    except Exception as e:
        return ToolResult.err(f"Failed to cancel task: {e}")
    return ToolResult.ok(
        f"Task {params.task_id} archived (was '{from_status}'). "
        f"It no longer appears in task lists or obligations."
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
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = TaskService()
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


# ── waive_attestation ────────────────────────────────────────


class WaiveAttestationParams(BaseModel):
    """Parameters for waive_attestation tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task whose attestation gate should be waived.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    reason: str = Field(
        description="Why this task is exempt (e.g. 'CLI 任务无 UI 可 browse，"
        "以 bash 验证日志替代'). Required for auditability.",
    )


@tool(
    "waive_attestation",
    "Waive the attestation gate for a task (coordinator only). Use for CLI-only "
    "tasks with no browsable UI, where bash verification logs replace "
    "browse/test attestations. The waiver is persisted (auditable) and expires "
    "in 24h. After waiving, the assignee can submit_task without attestationIds.",
    requires_workspace=False,
    security_level="standard",
)
async def waive_attestation_tool(
    params: WaiveAttestationParams, agent_id: str, workspace: str
) -> ToolResult:
    """Coordinator 显式豁免任务的 attestation 门禁（可审计的正式通道）。

    替代过去的 charter 口头豁免（工具层不读 charter，口头豁免无效）。
    """
    from hiveweave.services.attestation import create_waiver

    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "waive_attestation requires a non-empty 'reason' (auditability)."
        )

    ts = TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load task: {e}")
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    try:
        waiver_id = await create_waiver(
            project_id,
            task_id=params.task_id,
            waived_by=agent_id,
            reason=reason,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to create waiver: {e}")

    # 通知 assignee 现在可以无 attestationIds 提交（task 通道，wake=1）
    assignee = task.get("assignee_id")
    if assignee and assignee != agent_id:
        try:
            from hiveweave.services.inbox import InboxService

            await InboxService().send_message(
                from_agent_id=agent_id,
                to_agent_id=assignee,
                message=(
                    f"[TASK] Attestation gate waived for task "
                    f"'{(task.get('title') or '')[:60]}' ({params.task_id}). "
                    f"Reason: {reason[:200]}. You may now submit_task without "
                    f"attestationIds (bash 验证日志已在 summary 中说明)。"
                ),
                message_type="task",
                priority="normal",
                task_id=params.task_id,
            )
        except Exception as e:
            log.warning("waiver_notify_failed", error=str(e))

    log.info(
        "attestation_waived",
        project_id=project_id,
        task_id=params.task_id,
        waived_by=agent_id,
        reason=reason[:120],
    )
    return ToolResult.ok(
        f"Attestation waived for task {params.task_id} "
        f"(waiver {waiver_id[:8]}, expires in 24h). "
        f"Assignee may now submit_task without attestationIds."
    )


async def _spawn_post_approve_verify_task(
    ts: TaskService,
    project_id: str,
    reviewer_id: str,
    parent_task: dict,
) -> str | None:
    """Create a mandatory VERIFY child after successful worktree merge.

    Call sites: nudge_verify_tasks_after_merge only (not review_task approve).
    VERIFY stays created until merge/stale nudge claims it.
    """
    parent_id = parent_task.get("id")
    if not parent_id:
        return None
    # ── Prevent infinite VERIFY chain ──────────────────────────────
    # If the parent task itself is already a VERIFY task (identified by
    # its tags or a "VERIFY:" title prefix), do NOT spawn another VERIFY.
    # The original engineering task already has its VERIFY; allowing a
    # VERIFY-of-VERIFY-of-VERIFY… chain wastes architect/CEO review
    # cycles indefinitely.
    parent_tags = parent_task.get("tags") or []
    parent_title = parent_task.get("title") or ""
    if (isinstance(parent_tags, list) and "verify" in parent_tags) or (
        isinstance(parent_title, str) and parent_title.startswith("VERIFY:")
    ):
        log.info(
            "verify_chain_stopped",
            parent_task_id=parent_id,
            parent_title=parent_title[:80],
            reason="parent is already a VERIFY task",
        )
        return None
    # ────────────────────────────────────────────────────────────────
    # Avoid spawning duplicate VERIFY children for the same parent
    existing = await ts.list_tasks(project_id)
    for t in existing:
        tags = t.get("tags") or []
        if (
            t.get("parent_task_id") == parent_id
            and isinstance(tags, list)
            and "verify" in tags
            and t.get("status") not in ("closed", "approved")
        ):
            try:
                await ts.mark_verifying(project_id, parent_id)
            except Exception:
                pass
            return t.get("id")

    title = parent_task.get("title") or "task"
    original_assignee = parent_task.get("assignee_id")
    qa_assignee = await _find_independent_qa(
        project_id,
        original_assignee=original_assignee,
        # 合并人（通常=中层 builder）也不得自验 VERIFY
        exclude_ids={str(reviewer_id)} if reviewer_id else None,
    )
    blocked_note = ""
    if not qa_assignee:
        blocked_note = (
            " No independent QA agent found — VERIFY left unassigned/blocked; "
            "notify HR to hire QA."
        )

    # VERIFY 的 creator 落到 CEO（审权不落回 merger=中层）；submit 时
    # [TASK SUBMITTED] 因此直达 CEO 做里程碑验收。找不到 CEO 时退回 merger。
    creator_id = reviewer_id
    try:
        from hiveweave.services.org import OrgService

        ceo = await OrgService().get_agent_by_role(project_id, "ceo")
        if ceo and ceo.get("id"):
            creator_id = ceo["id"]
    except Exception as e:
        log.warning("verify_ceo_lookup_failed", error=str(e))

    verify_tags = ["verify", "mandatory", "post-merge"]
    from hiveweave.services.attestation import resolve_task_policy

    parent_policy = parent_task.get("policy_id") or resolve_task_policy(
        title=parent_task.get("title") or "",
        tags=parent_tags if isinstance(parent_tags, list) else [],
        description=parent_task.get("description") or "",
    )
    if parent_policy == "ui_browser_e2e":
        verify_tags.append("ui")

    verify_id = await ts.create_task(
        project_id,
        title=f"VERIFY: {title}"[:200],
        description=(
            f"Mandatory post-merge verification for parent task {parent_id}.\n"
            "1. Work is already on MAIN (coordinator merged the worktree).\n"
            "2. On the MAIN workspace only, run the project test suite "
            "(npm test / pytest / etc.).\n"
            "3. If the parent task touched user-visible screens, also run visual "
            "acceptance on main.\n"
            "4. submit_task with attestationIds from those runs.\n"
            "If checks fail, report blockers — do not silently pass.\n"
            f"Original implementer must NOT self-verify (was: {original_assignee})."
        ),
        creator_id=creator_id,
        assignee_id=qa_assignee,
        priority=1,
        acceptance_criteria=[
            {"text": "Final version on main passes project tests", "required": True},
            {"text": "submit_task includes attestationIds", "required": True},
        ],
        parent_task_id=parent_id,
        tags=verify_tags,
        source="system",
        # merged_by 供 review_task 的 VERIFY 独立审门排除合并人。
        evidence={"merged_by": str(reviewer_id)} if reviewer_id else None,
    )
    try:
        await ts.mark_verifying(project_id, parent_id)
    except Exception as e:
        log.warning(
            "parent_mark_verifying_failed",
            parent_id=parent_id,
            error=str(e),
        )

    if not qa_assignee:
        try:
            await ts.block_task(
                project_id,
                verify_id,
                "No independent QA agent; hire QA before VERIFY can run",
            )
        except Exception:
            try:
                await ts.update_task(
                    project_id,
                    verify_id,
                    blocked_reason="No independent QA; hire QA",
                )
            except Exception:
                pass
        # Notify HR + reviewer
        try:
            from hiveweave.services.inbox import InboxService
            from hiveweave.services.org import OrgService

            agents = await OrgService().list_agents(project_id)
            hr_ids = [
                a["id"]
                for a in agents
                if (a.get("status") or "active") == "active"
                and a.get("id")
                and (
                    (a.get("role") or "").lower() == "hr"
                    or "人力资源" in (a.get("role") or "")
                )
            ]
            inbox = InboxService()
            msg = (
                f"[VERIFY BLOCKED] Task {verify_id[:8]} needs independent QA "
                f"(≠ implementer {str(original_assignee)[:8]}). Please hire QA."
            )
            for hid in hr_ids:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=hid,
                    message=msg,
                    message_type="system",
                    priority="urgent",
                    task_id=verify_id,
                )
            if reviewer_id:
                await inbox.send_message(
                    from_agent_id="system",
                    to_agent_id=reviewer_id,
                    message=msg,
                    message_type="system",
                    priority="normal",
                    task_id=verify_id,
                )
        except Exception as e:
            log.warning("verify_qa_notify_failed", error=str(e))

    log.info(
        "verify_task_spawned",
        parent_task_id=parent_id,
        verify_task_id=verify_id,
        assignee_id=qa_assignee,
        original_assignee=original_assignee,
    )
    return verify_id


async def _find_independent_qa(
    project_id: str,
    *,
    original_assignee: str | None,
    exclude_ids: set[str] | None = None,
) -> str | None:
    """Pick QA-capability agent ≠ original implementer / merger (prefer same parent)."""
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import (
        Capability,
        has_capability,
        infer_role_family,
    )

    excluded = {str(x) for x in (exclude_ids or set()) if x}
    if original_assignee:
        excluded.add(str(original_assignee))

    agents = await OrgService().list_agents(project_id)
    active = [
        a
        for a in agents
        if (a.get("status") or "active") == "active"
        and a.get("id")
        and str(a.get("id")) not in excluded
    ]
    original_parent = None
    if original_assignee:
        for a in agents:
            if a.get("id") == original_assignee:
                original_parent = a.get("parent_id")
                break

    def is_qa(a: dict) -> bool:
        if infer_role_family(a) == "qa":
            return True
        return has_capability(a, Capability.BROWSER_ACCEPTANCE)

    qa_agents = [a for a in active if is_qa(a)]
    if not qa_agents:
        return None
    if original_parent:
        same = [a for a in qa_agents if a.get("parent_id") == original_parent]
        if same:
            return same[0]["id"]
    return qa_agents[0]["id"]


async def retry_qa_blocked_verify_tasks(project_id: str) -> int:
    """Re-attach VERIFY tasks left blocked+unassigned for lack of QA.

    背景（VERIFY 死区）：VERIFY 创建时若找不到独立 QA（≠ 父任务实施者），
    `_spawn_post_approve_verify_task` 会把它置为 blocked 且 assignee=NULL，
    只通知 HR 招人。人到岗后此前没有任何回头路 —— `_nudge_one_verify_task`
    在 assignee 为空时直接 return False，新 QA 只能闲置。
    本函数在 hire_agent 成功后被调用：扫描 blocked 且 assignee IS NULL 的
    VERIFY，复用 `_find_independent_qa` 重新挑人，挂回 created 并唤醒。

    单个任务失败不影响其余；找不到 QA 的任务保持 blocked 不动。
    Returns: 成功重挂（assign + unblock）的 VERIFY 数量。
    """
    import time as _time

    from hiveweave.services import task as task_module

    ts = TaskService()
    tasks = await ts.list_tasks(project_id)
    by_id = {t.get("id"): t for t in tasks if t.get("id")}
    reattached = 0
    for t in tasks:
        if not ts._is_verify_task(t):
            continue
        if t.get("status") != "blocked" or t.get("assignee_id"):
            continue
        tid = t.get("id")
        if not tid:
            continue
        try:
            # 独立性别名规则：排除父任务实施者 + 合并人，与创建时同一套查找逻辑
            parent = by_id.get(t.get("parent_task_id") or "")
            original = (parent or {}).get("assignee_id")
            ex: set[str] = set()
            ev = t.get("evidence") or {}
            if isinstance(ev, dict) and ev.get("merged_by"):
                ex.add(str(ev["merged_by"]))
            qa = await _find_independent_qa(
                project_id, original_assignee=original,
                exclude_ids=ex or None,
            )
            if not qa:
                log.info(
                    "verify_retry_no_qa",
                    project_id=project_id,
                    verify_task_id=tid,
                )
                continue
            await ts.update_task(project_id, tid, assignee_id=qa)
            # blocked → created 不在 _TRANSITIONS 内（状态机只允许
            # blocked → running/closed）。但这是 spawn 时兜底阻塞的纠偏：
            # 任务从未被认领执行，回到 created 等价于回到创建时刻，随后由
            # 既有 nudge 通道（claim + [POST-MERGE VERIFY] + trigger）接管。
            # 参照 archive_task：生命周期外纠偏，不走 _TRANSITIONS。
            now_ms = int(_time.time() * 1000)
            try:
                await task_module._execute(
                    project_id,
                    "UPDATE tasks SET status = 'created', blocked_reason = NULL, "
                    "wait_kind = NULL, wake_at = NULL, updated_at = ? "
                    "WHERE id = ?",
                    [now_ms, tid],
                )
            except Exception:
                await task_module._execute(
                    project_id,
                    "UPDATE tasks SET status = 'created', "
                    "blocked_reason = NULL, updated_at = ? WHERE id = ?",
                    [now_ms, tid],
                )
            nudged = await _nudge_one_verify_task(
                project_id,
                "system",
                {**t, "assignee_id": qa, "status": "created"},
                reason="merge",
            )
            reattached += 1
            log.info(
                "verify_retry_reattached",
                project_id=project_id,
                verify_task_id=tid,
                qa_assignee=qa,
                original_assignee=original,
                nudged=nudged,
            )
        except Exception as e:
            log.warning(
                "verify_retry_task_failed",
                project_id=project_id,
                verify_task_id=tid,
                error=str(e),
            )
    if reattached:
        log.info(
            "verify_retry_done",
            project_id=project_id,
            reattached=reattached,
        )
    return reattached


# Stale VERIFY child under a verifying parent (ms) — matches stall cooldown scale
VERIFY_STALE_MS = 15 * 60 * 1000
VERIFY_STALE_COOLDOWN_MS = 15 * 60 * 1000  # don't re-nudge same VERIFY every tick
_stale_verify_cooldowns: dict[str, int] = {}  # verify_task_id → last_nudge_ms


async def _nudge_one_verify_task(
    project_id: str,
    from_agent_id: str,
    task: dict,
    *,
    reason: str = "merge",
) -> bool:
    """Claim (if created) + send [POST-MERGE VERIFY] + trigger. Returns True if sent."""
    assignee = task.get("assignee_id")
    if not assignee:
        return False
    from hiveweave.services.inbox import InboxService
    from hiveweave.agents.trigger import trigger_subordinate
    from hiveweave.db import meta as meta_db

    dest = await meta_db.get_agent_by_id(assignee)
    if not dest or (dest.get("status") or "") != "active":
        return False

    # Claim on nudge — this is when VERIFY becomes actionable (post-merge / stale)
    tid = task.get("id")
    if tid and task.get("status") == "created":
        try:
            await TaskService().claim_task(project_id, tid, assignee)
            task = {**task, "status": "claimed"}
        except Exception as e:
            log.warning(
                "verify_nudge_claim_failed",
                verify_task_id=tid,
                error=str(e),
            )

    inbox = InboxService()
    await inbox.supersede_watchdog_messages(
        assignee, prefixes=["[POST-MERGE VERIFY]"]
    )
    title = (task.get("title") or "")[:60]
    if reason == "stale":
        body = (
            f"[POST-MERGE VERIFY] Stale verification — parent is still "
            f"'verifying'. Confirm merge landed, then run final tests on MAIN "
            f"for task '{title}' (id={tid}). "
            f"Run tests, then submit_task(testsPassed=true, testOutput=...)."
        )
    else:
        body = (
            f"[POST-MERGE VERIFY] Worktree merge completed. "
            f"Run final verification NOW on main for task "
            f"'{title}' (id={tid}). "
            f"Run tests on main, then "
            f"submit_task(testsPassed=true, testOutput=...)."
        )
    try:
        await inbox.send_message(
            from_agent_id=from_agent_id,
            to_agent_id=assignee,
            message=body,
            message_type="task",
            priority="urgent",
            task_id=tid,
        )
    except ValueError:
        return False
    await trigger_subordinate(assignee)
    return True


async def spawn_verify_for_approved_assignee(
    project_id: str,
    coordinator_id: str,
    *,
    assignee_id: str,
    merged_files: list[str] | None = None,
) -> list[str]:
    """Create VERIFY children for tasks covered by this merge (post-merge)."""
    from hiveweave.services.worktree_review import select_tasks_for_merged_work

    ts = TaskService()
    tasks = await ts.list_tasks(project_id)
    selected = select_tasks_for_merged_work(
        tasks,
        assignee_id=assignee_id,
        merged_files=merged_files,
        statuses=("approved", "verifying"),
    )
    spawned: list[str] = []
    for t in selected:
        vid = await _spawn_post_approve_verify_task(
            ts, project_id, coordinator_id, t
        )
        if vid:
            spawned.append(vid)
    return spawned


def parse_short_id_from_branch(branch_name: str) -> str | None:
    b = (branch_name or "").strip()
    if b.startswith("hw/"):
        parts = b.split("/")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    if b and len(b) <= 8 and b[0].upper() == "A" and b.isalnum():
        return b.upper() if b[0].isupper() else ("A" + b[1:])
    return None


async def resolve_agent_id_by_short_id(
    project_id: str, short_id: str
) -> str | None:
    from hiveweave.services.org import OrgService

    agents = await OrgService().list_agents(project_id)
    sid = (short_id or "").strip().upper()
    for a in agents:
        if (a.get("short_id") or "").upper() == sid:
            return a.get("id")
    return None


async def rework_tasks_after_merge_conflict(
    project_id: str,
    from_agent_id: str,
    *,
    merged_short_id: str | None = None,
    merged_branch: str | None = None,
    conflicts: list[str] | None = None,
    merged_files: list[str] | None = None,
) -> int:
    """On merge conflict: rework scoped approved tasks; wake executor in worktree."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.worktree_review import select_tasks_for_merged_work
    from hiveweave.agents.trigger import trigger_subordinate

    ts = TaskService()
    short = merged_short_id or (
        parse_short_id_from_branch(merged_branch or "") if merged_branch else None
    )
    agent_id = None
    if short:
        agent_id = await resolve_agent_id_by_short_id(project_id, short)
    if not agent_id:
        return 0

    tasks = await ts.list_tasks(project_id)
    selected = select_tasks_for_merged_work(
        tasks,
        assignee_id=agent_id,
        merged_files=merged_files or conflicts,
        statuses=("approved",),
    )
    files = ", ".join((conflicts or merged_files or [])[:12]) or "(unknown)"
    feedback = (
        f"[MERGE CONFLICT] Main merge aborted. In YOUR worktree, merge or "
        f"rebase main into your branch, resolve: {files}. "
        f"Then checkpoint and re-submit. Do NOT edit main."
    )
    inbox = InboxService()
    count = 0
    for t in selected:
        tid = t.get("id")
        if not tid:
            continue
        try:
            await ts.review_task(project_id, tid, "rework", feedback)
        except Exception as e:
            log.warning("merge_conflict_rework_task_failed", task_id=tid, error=str(e))
            continue
        try:
            await inbox.send_message(
                from_agent_id=from_agent_id,
                to_agent_id=agent_id,
                message=f"[REWORK REQUESTED] {feedback}",
                message_type="task",
                priority="urgent",
                task_id=tid,
            )
            await trigger_subordinate(agent_id)
        except Exception as e:
            log.warning("merge_conflict_inbox_failed", error=str(e))
        count += 1
    if count:
        log.info(
            "merge_conflict_tasks_reworked",
            project_id=project_id,
            assignee_id=agent_id,
            count=count,
        )
    return count


async def nudge_verify_tasks_after_merge(
    project_id: str,
    from_agent_id: str,
    *,
    merged_short_id: str | None = None,
    merged_agent_id: str | None = None,
    merged_branch: str | None = None,
    merged_files: list[str] | None = None,
) -> int:
    """After successful merge: spawn VERIFY for scoped tasks, then nudge.

    VERIFY is intentionally NOT created at approve time — only here.
    Scope is the merged work (files/branch), not every approved task.
    """
    from hiveweave.services.worktree_review import select_tasks_for_merged_work

    ts = TaskService()
    agent_id = merged_agent_id
    short = merged_short_id or (
        parse_short_id_from_branch(merged_branch or "") if merged_branch else None
    )
    if not agent_id and short:
        agent_id = await resolve_agent_id_by_short_id(project_id, short)

    parent_ids: set[str] = set()
    if agent_id:
        try:
            all_tasks = await ts.list_tasks(project_id)
            selected = select_tasks_for_merged_work(
                all_tasks,
                assignee_id=agent_id,
                merged_files=merged_files,
                statuses=("approved", "verifying"),
            )
            parent_ids = {t["id"] for t in selected if t.get("id")}
            spawned = []
            for t in selected:
                tid = t.get("id")
                try:
                    vid = await _spawn_post_approve_verify_task(
                        ts, project_id, from_agent_id, t
                    )
                    if vid:
                        spawned.append(vid)
                    elif tid:
                        # Spawn returned None without raising — still advance
                        # parent out of bare approved so it cannot stall forever.
                        try:
                            await ts.mark_verifying(project_id, tid)
                        except Exception:
                            pass
                except Exception as spawn_err:
                    log.warning(
                        "verify_spawn_one_failed",
                        parent_id=tid,
                        error=str(spawn_err),
                    )
                    if tid:
                        try:
                            await ts.mark_verifying(project_id, tid)
                        except Exception:
                            pass
            if spawned:
                log.info(
                    "verify_spawned_after_merge",
                    project_id=project_id,
                    assignee_id=agent_id,
                    count=len(spawned),
                    parent_ids=list(parent_ids),
                )
        except Exception as e:
            log.warning("verify_spawn_after_merge_failed", error=str(e))
            raise

    tasks = await ts.list_tasks(project_id)
    nudged = 0
    for t in tasks:
        tags = t.get("tags") or []
        if not (isinstance(tags, list) and "verify" in tags):
            continue
        if t.get("status") not in ("created", "claimed", "running"):
            continue
        # Only VERIFY children of parents covered by this merge
        if parent_ids:
            if t.get("parent_task_id") not in parent_ids:
                continue
        elif agent_id:
            parent_id = t.get("parent_task_id")
            if parent_id:
                parent = next((p for p in tasks if p.get("id") == parent_id), None)
                if parent and parent.get("assignee_id") not in (None, agent_id):
                    continue
        if await _nudge_one_verify_task(
            project_id, from_agent_id, t, reason="merge"
        ):
            nudged += 1
    if nudged:
        log.info(
            "verify_tasks_nudged_after_merge",
            project_id=project_id,
            count=nudged,
        )
    return nudged


async def nudge_stale_verify_tasks(
    project_id: str,
    *,
    stale_ms: int = VERIFY_STALE_MS,
    now_ms: int | None = None,
) -> int:
    """Nudge VERIFY children stuck under verifying parents past stale_ms.

    Closes the gap when merge nudge never fired: parent stays verifying and
    VERIFY sits in created/claimed with nobody woken.
    """
    import time as _time

    ts = TaskService()
    tasks = await ts.list_tasks(project_id)
    now = now_ms if now_ms is not None else int(_time.time() * 1000)

    verifying_parents = {
        t["id"]
        for t in tasks
        if t.get("status") == "verifying" and not ts._is_verify_task(t)
    }
    if not verifying_parents:
        return 0

    nudged = 0
    for t in tasks:
        if not ts._is_verify_task(t):
            continue
        if t.get("status") not in ("created", "claimed"):
            continue
        parent_id = t.get("parent_task_id")
        if not parent_id or parent_id not in verifying_parents:
            continue
        updated = int(t.get("updated_at") or 0)
        if updated and (now - updated) < stale_ms:
            continue
        tid = t.get("id") or ""
        last = _stale_verify_cooldowns.get(tid, 0)
        if now - last < VERIFY_STALE_COOLDOWN_MS:
            continue
        if await _nudge_one_verify_task(
            project_id, "system", t, reason="stale"
        ):
            _stale_verify_cooldowns[tid] = now
            nudged += 1
            try:
                from hiveweave.services.telemetry import (
                    telemetry,
                    VERIFY_STALE_NUDGE,
                )

                telemetry.emit(
                    VERIFY_STALE_NUDGE,
                    {
                        "project_id": project_id,
                        "verify_task_id": t.get("id"),
                        "parent_task_id": parent_id,
                        "assignee_id": t.get("assignee_id"),
                    },
                )
            except Exception:
                pass

    if nudged:
        log.warning(
            "verify_stale_tasks_nudged",
            project_id=project_id,
            count=nudged,
        )
    return nudged


# ── get_tasks ───────────────────────────────────────────


class GetTasksParams(BaseModel):
    """Parameters for get_tasks tool."""
    model_config = ConfigDict(populate_by_name=True)

    status: str | None = Field(
        default=None,
        description="Filter by status (optional).",
        json_schema_extra={"aliases": ["status", "state"]},
    )
    assignee_id: str | None = Field(
        default=None,
        alias="assigneeId",
        description="Filter by assignee agent ID (optional).",
        json_schema_extra={"aliases": ["assigneeId", "assignee_id", "assignee"]},
    )


@tool(
    "get_tasks",
    "List tasks in the Task Ledger with optional filters (status, assignee).",
    requires_workspace=False,
    security_level="standard",
)
async def get_tasks_tool(
    params: GetTasksParams, agent_id: str, workspace: str
) -> ToolResult:
    """List tasks with optional filters."""
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = TaskService()
        tasks = await ts.list_tasks(
            project_id, status=params.status, assignee_id=params.assignee_id
        )
        if not tasks:
            return ToolResult.ok("No tasks found matching the filters.", tasks=[])
        lines = []
        for t in tasks:
            lines.append(
                f"- [{t.get('status', '?')}] {t.get('title', '?')} "
                f"(id={t.get('id', '?')}, "
                f"progress={t.get('progress', 0)}%, "
                f"assignee={t.get('assignee_id') or 'unassigned'})"
            )
        return ToolResult.ok(
            f"Tasks ({len(tasks)}):\n" + "\n".join(lines),
            tasks=tasks,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to list tasks: {e}")
