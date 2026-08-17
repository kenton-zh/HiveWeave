"""create_task tool

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
    contract_json: dict[str, Any] | None = Field(
        default=None,
        alias="contractJson",
        description=(
            "Slice contract (optional). When set, this task is a slice: "
            "ready gate blocks start until upstream verified; submit runs "
            "machine acceptance (file_exists/content_contains/min_lines)."
        ),
        json_schema_extra={"aliases": ["contractJson", "contract_json", "contract"]},
    )
    force: bool = Field(
        default=False,
        description=(
            "Force create despite cross-assignee / structured duplicate "
            "(same-assignee dup cannot be forced)."
        ),
    )
    submit_gate: str = Field(
        ...,
        alias="submitGate",
        description=(
            "How this task will be evidenced at submit/review. Required. "
            "docs | unit | module_visual | code_audit | "
            "code_audit+module_visual | code_audit+unit."
        ),
        json_schema_extra={"aliases": ["submitGate", "submit_gate", "gate"]},
    )
    milestone_verify: bool = Field(
        default=False,
        alias="milestoneVerify",
        description=(
            "Coordinator/CEO only: mint a MAIN-serialized VERIFY: task for "
            "a complete milestone (not per-leaf merge). Testing stays on MAIN."
        ),
        json_schema_extra={"aliases": ["milestoneVerify", "milestone_verify"]},
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
    "(assign=claim; no separate claim_task) unless depends_on is unmet (blocked). "
    "Does NOT wake anyone — call dispatch_task to deliver. "
    "submitGate is required (docs|unit|module_visual|code_audit|…). "
    "If you are delegating/transferring an EXISTING task to someone else, prefer "
    "dispatch_task(taskId=<existing_id>) instead — it re-assigns in a single ledger "
    "entry and avoids duplicate tasks with untracked free-text dependencies. "
    "Coordinator/CEO milestone MAIN QA: milestoneVerify=true (mints VERIFY:).",
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

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    assignee_id = params.assignee_id
    org_service = ctx.org if ctx else None
    description = params.description
    if assignee_id:
        resolved = await _helpers.resolve_agent_id(project_id, assignee_id, org_service)
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
            # CODE AUDIT POLICY: 写码 assignee 创建即钉审计门禁（幂等，
            # create→dispatch 双钉无害；无 worktree 能力（CEO/HR）不钉）
            try:
                from hiveweave.services.code_audit import append_code_audit_notice
                from hiveweave.services.git_worktree import agent_gets_write_worktree
                from hiveweave.services.org import OrgService

                assignee_agent = await OrgService().resolve_agent(assignee_id)
                if assignee_agent and agent_gets_write_worktree(assignee_agent):
                    description = append_code_audit_notice(description)
            except Exception as e:
                log.warning(
                    "create_task_audit_notice_failed",
                    assignee_id=assignee_id,
                    error=str(e),
                )
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
        ts = _task_svc.TaskService()
        force_note = ""
        struct_dup = await ts.find_structured_open_dup(
            project_id,
            parent_task_id=params.parent_task_id,
            expected_modules=params.expected_modules,
        )
        if struct_dup and not params.force:
            return ToolResult.err(
                f"结构化重复任务 id={struct_dup['id']} "
                f"status={struct_dup.get('status')}。"
                f"请复用 taskId=\"{struct_dup['id']}\" 或 force=true。"
            )
        dup = await ts.find_similar_open_task(
            project_id,
            params.title,
            assignee_id=assignee_id,
            include_unassigned=True,
        )
        if dup:
            dup_assignee = dup.get("assignee_id")
            if assignee_id and dup_assignee == assignee_id:
                return ToolResult.err(
                    f"已有相似未完成任务 id={dup['id']} "
                    f"title={dup.get('title', '')[:60]!r}。"
                    f"请复用 taskId=\"{dup['id']}\"（同 assignee 不可 force）。"
                )
            if not dup_assignee:
                if not params.force:
                    return ToolResult.err(
                        f"已有相似未分配任务 id={dup['id']} "
                        f"title={dup.get('title', '')[:60]!r}。"
                        f"请复用 taskId=\"{dup['id']}\" 或 force=true。"
                    )
                force_note = " (force=true: proceeding despite unassigned dup)"
            elif dup_assignee and dup_assignee != assignee_id:
                if not params.force:
                    return ToolResult.err(
                        f"跨 assignee 相似任务 id={dup['id']} "
                        f"assignee={dup_assignee[:8]}。"
                        f"请复用、cancel 旧任务，或 force=true。"
                    )
                force_note = (
                    " (force=true: proceeding despite cross-assignee dup)"
                )
        cross_dup = await ts.find_similar_open_task(
            project_id, params.title, assignee_id=None
        )
        if (
            cross_dup
            and cross_dup.get("assignee_id")
            and cross_dup.get("assignee_id") != assignee_id
        ):
            if not params.force:
                return ToolResult.err(
                    f"跨 assignee 相似任务 id={cross_dup['id']} "
                    f"assignee={cross_dup.get('assignee_id', '?')[:8]}。"
                    f"请复用 taskId=\"{cross_dup['id']}\" 或 force=true。"
                )
            if not force_note:
                force_note = (
                    " (force=true: proceeding despite cross-assignee dup)"
                )
        from hiveweave.services.attestation import policy_from_submit_gate

        try:
            policy_id = policy_from_submit_gate(params.submit_gate)
        except ValueError as e:
            return ToolResult.err(str(e))

        title = params.title
        source = "agent"
        if params.milestone_verify:
            from hiveweave.services.policy import infer_role_family
            from hiveweave.services.org import OrgService
            from hiveweave.services.tasks.verify import is_verify_title

            me = await OrgService().resolve_agent(agent_id)
            family = infer_role_family(me or {})
            if family not in ("ceo", "coordinator"):
                return ToolResult.err(
                    "milestoneVerify is for coordinators/CEO arranging MAIN QA, "
                    "not leaf self-service."
                )
            if not is_verify_title(title):
                title = f"VERIFY: {title}"
            source = "system"
        task_id = await ts.create_task(
            project_id=project_id,
            title=title,
            description=description,
            creator_id=agent_id,
            assignee_id=assignee_id,
            priority=params.priority,
            due_at=params.due_at,
            acceptance_criteria=params.acceptance_criteria,
            parent_task_id=params.parent_task_id,
            depends_on=params.depends_on,
            expected_modules=params.expected_modules,
            tags=params.tags,
            source=source,
            contract_json=params.contract_json,
            policy_id=policy_id,
        )
        task = await ts.get_task(project_id, task_id)
        st = (task or {}).get("status") or "created"
        if st == "blocked":
            note = (
                "status=blocked (depends_on unmet; assignee recorded, "
                "not claimed or woken)"
            )
        elif assignee_id and st == "claimed":
            note = "status=claimed (assign=claim)"
        else:
            note = f"status={st}"
        return ToolResult.ok(
            f"Task created (id={task_id}, {note}): {title}{force_note}",
            task_id=task_id,
            status=st,
        )
    except Exception as e:
        return ToolResult.err(f"Failed to create task: {e}")

