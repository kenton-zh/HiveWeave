"""dispatch_task tool

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
    force: bool = Field(
        default=False,
        description=(
            "Force create despite cross-assignee / structured duplicate "
            "(same-assignee dup cannot be forced)."
        ),
    )
    parent_task_id: str | None = Field(
        default=None,
        alias="parentTaskId",
        description="Parent task ID for structured dedup (optional).",
        json_schema_extra={"aliases": ["parentTaskId", "parent_task_id"]},
    )
    expected_modules: list[str] | None = Field(
        default=None,
        alias="expectedModules",
        description="Expected modules for structured dedup (optional).",
        json_schema_extra={"aliases": ["expectedModules", "expected_modules"]},
    )
    artifact_refs: list[str] | None = Field(
        default=None,
        alias="artifactRefs",
        description=(
            "Structured file paths the assignee must be able to read "
            "(e.g. specs, contracts). Validated at dispatch time in the "
            "assignee's worktree — missing paths are reported immediately."
        ),
        json_schema_extra={"aliases": ["artifactRefs", "artifact_refs", "required_paths"]},
    )

    @field_validator("expected_modules", mode="before")
    @classmethod
    def _coerce_dispatch_modules(cls, v: Any) -> Any:
        return _coerce_to_list(v)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _coerce_artifact_refs(cls, v: Any) -> Any:
        return _coerce_to_list(v)


@tool(
    "dispatch_task",
    "Deliver work NOW: ledger entry + inbox wake. "
    "To re-assign/delegate an EXISTING task, pass taskId — this keeps a single "
    "ledger entry (assignee changes, no duplicate task). "
    "Only create a NEW task (omit taskId) when the work is genuinely new. "
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
    from hiveweave.services import task as _task_svc

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    org_service = ctx.org if ctx else None
    resolved_id = await _helpers.resolve_agent_id(project_id, params.target, org_service)
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

    # Dedup (TEST21 M3): structured + title similarity + cross-assignee force gate
    force_note = ""
    if not params.task_id:
        ts_dedup = _task_svc.TaskService()
        try:
            struct_dup = await ts_dedup.find_structured_open_dup(
                project_id,
                parent_task_id=params.parent_task_id,
                expected_modules=params.expected_modules,
            )
            if struct_dup and not params.force:
                return ToolResult.err(
                    f"结构化重复任务 id={struct_dup['id']} "
                    f"status={struct_dup.get('status')} "
                    f"parent={struct_dup.get('parent_task_id', '')[:8]}。"
                    f"请复用 taskId=\"{struct_dup['id']}\"，"
                    f"或 force=true 强制新建。"
                )
        except Exception as e:
            log.warning("dispatch_structured_dedup_failed", error=str(e))
        try:
            dup = await ts_dedup.find_similar_open_task(
                project_id,
                params.task[:100],
                assignee_id=resolved_id,
                include_unassigned=True,
            )
            if dup:
                dup_assignee = dup.get("assignee_id")
                if dup_assignee == resolved_id:
                    return ToolResult.err(
                        f"已有相似未完成任务 id={dup['id']} "
                        f"status={dup.get('status')} "
                        f"title={dup.get('title', '')[:60]!r}。"
                        f"请复用：dispatch_task(..., taskId=\"{dup['id']}\")，"
                        f"或先 cancel_task(taskId=\"{dup['id']}\", reason=\"...\")。"
                        f"（同 assignee 重复不可 force。）"
                    )
                if not dup_assignee:
                    if not params.force:
                        return ToolResult.err(
                            f"已有相似未分配任务 id={dup['id']} "
                            f"title={dup.get('title', '')[:60]!r}。"
                            f"请复用 taskId=\"{dup['id']}\" 或 force=true。"
                        )
                    force_note = " (force=true: proceeding despite unassigned dup)"
        except Exception as e:
            log.warning("dispatch_dedup_check_failed", error=str(e))
        try:
            cross_dup = await ts_dedup.find_similar_open_task(
                project_id, params.task[:100], assignee_id=None
            )
            if cross_dup and cross_dup.get("assignee_id") != resolved_id:
                if not params.force:
                    return ToolResult.err(
                        f"跨 assignee 相似任务 id={cross_dup['id']} "
                        f"assignee={cross_dup.get('assignee_id', '?')[:8]} "
                        f"title={cross_dup.get('title', '')[:40]!r}。"
                        f"请复用 taskId=\"{cross_dup['id']}\"、"
                        f"cancel_task 旧任务，或 force=true 强制派发。"
                    )
                if not force_note:
                    force_note = (
                        " (force=true: proceeding despite cross-assignee dup)"
                    )
        except Exception:
            pass

    # P1-2: artifact_refs existence validation at dispatch time.
    # Catches "spec file not visible" bugs immediately (creator-side check).
    artifact_warnings: list[str] = []
    if params.artifact_refs:
        from pathlib import Path as _P

        from hiveweave.db import meta as meta_db

        main_ws = await meta_db.get_project_workspace(project_id)
        # Resolve assignee worktree (best-effort)
        assignee_wt: str | None = None
        try:
            from hiveweave.services.worktree_review import agent_worktree_path

            assignee_wt = await agent_worktree_path(resolved_id)
        except Exception:
            pass
        for ref in params.artifact_refs:
            ref_clean = str(ref).strip().lstrip("./")
            if not ref_clean:
                continue
            # .hiveweave/ paths are gitignored → invisible cross-worktree
            if ".hiveweave/" in ref_clean or ref_clean.startswith(".hiveweave"):
                artifact_warnings.append(
                    f"PATH INVISIBLE: '{ref_clean}' is under .hiveweave/ "
                    f"(gitignored, not visible to other agents). "
                    f"Move shared specs to docs/ or a git-tracked path."
                )
                continue
            # Check existence in main workspace and/or assignee worktree
            found = False
            if main_ws and (_P(main_ws) / ref_clean).exists():
                found = True
            if not found and assignee_wt and (_P(assignee_wt) / ref_clean).exists():
                found = True
            if not found:
                artifact_warnings.append(
                    f"NOT FOUND: '{ref_clean}' does not exist in main "
                    f"workspace or assignee worktree. Ensure it is committed "
                    f"to main before the assignee starts."
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
        if artifact_warnings:
            output += (
                "\n\n⚠ ARTIFACT_REF WARNINGS:\n"
                + "\n".join(f"- {w}" for w in artifact_warnings)
            )
        return ToolResult.ok(output + force_note, task_id=result.get("task_id"))
    return ToolResult.err(result.get("message", "Dispatch failed"))

