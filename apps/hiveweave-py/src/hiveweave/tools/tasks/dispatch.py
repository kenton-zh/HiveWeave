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

_SHARED_ARTIFACT_PREFIX = ".hiveweave/shared"


def _is_shared_artifact_ref(ref_clean: str) -> bool:
    """True for ``.hiveweave/shared`` itself or a file/dir under it."""
    n = (ref_clean or "").replace("\\", "/")
    return n == _SHARED_ARTIFACT_PREFIX or n.startswith(
        _SHARED_ARTIFACT_PREFIX + "/"
    )


def _is_invisible_hiveweave_ref(ref_clean: str) -> bool:
    """True when a ref is under ``.hiveweave/`` but not the shared draft tree.

    Canonical contracts live in ``docs/``. ``.hiveweave/shared/`` is
    git-tracked draft/collab and is visible cross-worktree.
    """
    n = (ref_clean or "").replace("\\", "/")
    if ".hiveweave/" not in n and not n.startswith(".hiveweave"):
        return False
    if _is_shared_artifact_ref(n):
        return False
    return True


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
    submit_gate: str | None = Field(
        default=None,
        alias="submitGate",
        description=(
            "Required when creating a NEW task (omit taskId). "
            "docs | unit | module_visual | code_audit | "
            "code_audit+module_visual | code_audit+unit."
        ),
        json_schema_extra={"aliases": ["submitGate", "submit_gate", "gate"]},
    )
    milestone_verify: bool = Field(
        default=False,
        alias="milestoneVerify",
        description=(
            "Coordinator/CEO: mint a MAIN-serialized VERIFY: milestone QA task."
        ),
        json_schema_extra={"aliases": ["milestoneVerify", "milestone_verify"]},
    )
    depends_on: list[str] | None = Field(
        default=None,
        alias="dependsOn",
        description=(
            "Other task ids only (self-id rejected). Unmet → blocked "
            "(assignee recorded, not woken). VERIFY titles skip auto-block. "
            "People-waiting is commit_turn, not this list."
        ),
        json_schema_extra={"aliases": ["dependsOn", "depends_on"]},
    )

    @field_validator("expected_modules", "artifact_refs", "depends_on", mode="before")
    @classmethod
    def _coerce_dispatch_lists(cls, v: Any) -> Any:
        return _coerce_to_list(v)


@tool(
    "dispatch_task",
    "Deliver work NOW: ledger entry + inbox wake (unless blocked on depends_on). "
    "Always pass submitGate (docs|unit|module_visual|code_audit|…) — required for "
    "new tasks, ignored on taskId reuse. Unmet dependsOn also applies when reusing "
    "taskId (blocked, not woken). dependsOn = other task ids only "
    "(self-id rejected); waiting on a person is commit_turn. "
    "To re-assign/delegate an EXISTING task, pass taskId — this keeps a single "
    "ledger entry (assignee changes, no duplicate task). "
    "Only create a NEW task (omit taskId) when the work is genuinely new. "
    "Only direct reports; never assign coordinators code work. "
    "Writer leaves auto-carry a delivery contract — executor fills "
    "deliveryContract={summary, test} at submit (see submit_task). "
    "Milestone MAIN QA: milestoneVerify=true (coordinator/CEO).",
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
    shared_refs_ok = False
    if params.artifact_refs:
        from pathlib import Path as _P

        from hiveweave.db import meta as meta_db

        main_ws = await meta_db.get_project_workspace(project_id)
        from hiveweave.services.worktree_review import (
            agent_worktree_path,
            normalize_evidence_path,
        )

        assignee_wt: str | None = None
        dispatcher_wt: str | None = None
        try:
            assignee_wt = await agent_worktree_path(resolved_id)
        except Exception:
            pass
        try:
            dispatcher_wt = await agent_worktree_path(agent_id)
        except Exception:
            pass
        dispatcher_on_main = False
        try:
            dispatcher_on_main = bool(
                dispatcher_wt
                and main_ws
                and _P(dispatcher_wt).resolve() == _P(main_ws).resolve()
            )
        except (OSError, ValueError):
            dispatcher_on_main = False

        for ref in params.artifact_refs:
            # Do NOT use str.lstrip("./") — strips every leading '.' and breaks
            # ".hiveweave/…" into "hiveweave/…" (TEST11/TEST19).
            ref_clean = normalize_evidence_path(ref)
            if not ref_clean:
                continue
            # Private .hiveweave/ paths are invisible cross-worktree.
            # .hiveweave/shared/ is git-tracked draft/collab — allow + exist-check.
            if _is_invisible_hiveweave_ref(ref_clean):
                artifact_warnings.append(
                    f"PATH INVISIBLE: '{ref_clean}' is under .hiveweave/ "
                    f"(gitignored, not visible to other agents). "
                    f"Move shared specs to docs/ or a git-tracked path."
                )
                continue
            if _is_shared_artifact_ref(ref_clean):
                shared_refs_ok = True
            # Check existence in main workspace and/or assignee worktree
            found = False
            if main_ws and (_P(main_ws) / ref_clean).exists():
                found = True
            if not found and assignee_wt and (_P(assignee_wt) / ref_clean).exists():
                found = True
            on_dispatcher = bool(
                dispatcher_wt
                and not dispatcher_on_main
                and (_P(dispatcher_wt) / ref_clean).exists()
            )
            if not found and on_dispatcher:
                artifact_warnings.append(
                    f"EXISTS ON YOUR TREE ONLY: '{ref_clean}' is in your "
                    f"worktree, not MAIN. Assignee will treat it as absent "
                    f"until git_worktree_merge. That is OK."
                )
            elif not found:
                artifact_warnings.append(
                    f"NOT FOUND: '{ref_clean}' does not exist in MAIN or "
                    f"the assignee worktree. Assignee will treat it as "
                    f"absent until it is on MAIN. That is OK."
                )

    ds = DispatchService()
    policy_id: str | None = None
    title: str | None = None
    source = "agent"
    if not params.task_id:
        from hiveweave.services.attestation import policy_from_submit_gate

        try:
            policy_id = policy_from_submit_gate(params.submit_gate)
        except ValueError as e:
            return ToolResult.err(str(e))
        if params.milestone_verify:
            from hiveweave.services.org import OrgService
            from hiveweave.services.policy import infer_role_family
            from hiveweave.services.tasks.verify import is_verify_title

            me = await OrgService().resolve_agent(agent_id)
            family = infer_role_family(me or {})
            if family not in ("ceo", "coordinator"):
                return ToolResult.err(
                    "milestoneVerify is for coordinators/CEO arranging MAIN QA, "
                    "not leaf self-service."
                )
            title = (params.task or "")[:100]
            if not is_verify_title(title):
                title = f"VERIFY: {title}"
            source = "system"
    elif params.milestone_verify:
        return ToolResult.err(
            "milestoneVerify only applies when creating a new task (omit taskId)."
        )
    result = await ds.dispatch_task(
        project_id=project_id,
        from_agent_id=agent_id,
        to_agent_id=resolved_id,
        description=params.task,
        expect_report=params.expect_report,
        existing_task_id=params.task_id,
        policy_id=policy_id,
        title=title,
        source=source,
        depends_on=params.depends_on,
        parent_task_id=params.parent_task_id,
    )
    if result.get("success"):
        # Align with review_task: inbox alone is not enough — wake assignee
        # unless the ledger parked the task on unmet depends_on.
        if not result.get("blocked"):
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
        wt_sid = result.get("worktree_short_id")
        if wt_sid:
            output += f" worktree={wt_sid}."
        parent = result.get("parent_task_id") or ""
        output += (
            " Wait on this child with commit_turn(waiting, kind=task, "
            "ref=the task_id above); do not ask_agent the assignee to submit."
        )
        if parent:
            output += f" parent_task_id={parent}."
        if result.get("blocked"):
            output += (
                " Task is blocked on unmet depends_on; assignee recorded, "
                "not woken."
            )
        if shared_refs_ok:
            output += (
                " Note: .hiveweave/shared is draft/collab; "
                "canonical contracts live in docs/."
            )
        if artifact_warnings:
            output += (
                "\n\n⚠ ARTIFACT_REF WARNINGS:\n"
                + "\n".join(f"- {w}" for w in artifact_warnings)
            )
        return ToolResult.ok(output + force_note, task_id=result.get("task_id"))
    return ToolResult.err(result.get("message", "Dispatch failed"))

