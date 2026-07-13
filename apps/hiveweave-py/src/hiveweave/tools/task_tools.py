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
        project_id = await self._get_project_id(agent_id)
        if not project_id:
            return self._error(f"Agent {agent_id} has no project")
        evidence: dict = {"summary": summary}
        if args.get("commit"):
            evidence["commit"] = args["commit"]
        if args.get("filesChanged"):
            evidence["files_changed"] = args["filesChanged"]
        if args.get("testsPassed") is not None:
            evidence["tests_passed"] = args["testsPassed"]
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
    "Dispatch a task to a subordinate agent. Resolves target by name, short_id, or UUID.",
    requires_workspace=False,
    security_level="standard",
)
async def dispatch_task_tool(
    params: DispatchTaskParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Dispatch a task to a subordinate."""
    from hiveweave.services.dispatch import DispatchService

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
        return ToolResult.ok(
            f"Task dispatched to {result.get('to_agent_id', resolved_id)} "
            f"(task_id={result.get('task_id', '')})",
            task_id=result.get("task_id"),
        )
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
    "Create a new task in the Task Ledger (status=created).",
    requires_workspace=False,
    security_level="standard",
)
async def create_task_tool(
    params: CreateTaskParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Create a new task."""
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    assignee_id = params.assignee_id
    if assignee_id:
        org_service = ctx.org if ctx else None
        resolved = await resolve_agent_id(project_id, assignee_id, org_service)
        if resolved:
            assignee_id = resolved

    try:
        ts = TaskService()
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
        return ToolResult.ok(
            f"Task created (id={task_id}): {params.title}",
            task_id=task_id,
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
    "Claim a task (created -> claimed), setting yourself as assignee.",
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
        description="Reason for blocking (used when status='blocked').",
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
        description="Whether tests passed (optional).",
        json_schema_extra={"aliases": ["testsPassed", "tests_passed"]},
    )

    @field_validator("files_changed", mode="before")
    @classmethod
    def _coerce_files_changed(cls, v: Any) -> Any:
        return _coerce_to_list(v)


@tool(
    "submit_task",
    "Submit a task for review (running -> submitted). Attaches evidence (summary, commit, files, tests). If taskId omitted, auto-detects your current running task.",
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
    if not task_id:
        # Auto-detect: find agent's current running/claimed task
        ts = TaskService()
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

    evidence: dict[str, Any] = {"summary": params.summary}
    if params.commit:
        evidence["commit"] = params.commit
    if params.files_changed:
        evidence["files_changed"] = params.files_changed
    if params.tests_passed is not None:
        evidence["tests_passed"] = params.tests_passed

    try:
        ts = TaskService()
        # Auto-transition: if task is in 'created' or 'claimed' status,
        # automatically claim/start it before submitting.
        # This simplifies the LLM workflow — no need to call claim_task +
        # update_task_status before submit_task.
        task = await ts.get_task(project_id, task_id)
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

        # ── 通知 creator/coordinator 有 task 待审 ──
        # Task Ledger 通知链：submit 后必须通知 creator，否则 coordinator
        # 不知道有 task 待审 → idle → 团队停滞。
        task_after = await ts.get_task(project_id, task_id)
        if task_after and task_after.get("creator_id"):
            creator_id = task_after["creator_id"]
            if creator_id != agent_id:  # 不通知自己
                from hiveweave.services.inbox import InboxService
                inbox = InboxService()
                await inbox.send_message(
                    from_agent_id=agent_id,
                    to_agent_id=creator_id,
                    message=(
                        f"[TASK SUBMITTED] Task '{task_after.get('title', '')[:60]}' "
                        f"has been submitted for your review. "
                        f"Use review_task(taskId='{task_id}', decision='approve'/'rework') "
                        f"to review."
                    ),
                    message_type="task",
                    priority="normal",
                    task_id=task_id,
                )
                # 触发 coordinator 处理
                from hiveweave.agents.trigger import trigger_coordinator
                await trigger_coordinator(creator_id)

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
    "Review a submitted task (reviewing -> approved/rework). If task is 'submitted', starts review automatically.",
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
        current_status = task["status"]
        if current_status == "submitted":
            await ts.start_review(project_id, params.task_id)
        elif current_status != "reviewing":
            return ToolResult.err(
                f"Task must be 'submitted' or 'reviewing' to review, "
                f"but is '{current_status}'"
            )
        await ts.review_task(project_id, params.task_id, decision, params.feedback)

        # ── 通知 assignee/executor 审查结果 ──
        # Task Ledger 通知链：review 后必须通知 assignee，否则 executor
        # 不知道审批结果 → idle → 团队停滞。
        task_after = await ts.get_task(project_id, params.task_id)
        if task_after and task_after.get("assignee_id"):
            assignee_id = task_after["assignee_id"]
            if assignee_id != agent_id:  # 不通知自己
                from hiveweave.services.inbox import InboxService
                inbox = InboxService()
                if decision == "approve":
                    msg = (
                        f"[TASK APPROVED] Task '{task_after.get('title', '')[:60]}' "
                        f"has been approved. You can proceed with your next task."
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
                )
                # 触发 executor 处理
                from hiveweave.agents.trigger import trigger_subordinate
                await trigger_subordinate(assignee_id)

        if decision == "approve":
            return ToolResult.ok(f"Task {params.task_id} approved.")
        return ToolResult.ok(f"Task {params.task_id} sent back for rework.")
    except Exception as e:
        return ToolResult.err(f"Failed to review task: {e}")


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
