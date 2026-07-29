"""waive_attestation / waive_merge

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
    evidence_attestation_id: str = Field(
        alias="evidenceAttestationId",
        description=(
            "REQUIRED: id of an execution attestation (test_run / browse_e2e / "
            "visual_check / doc_review) that backs this waiver. Pure read_file "
            "review is not accepted — the escape hatch must cite machine evidence."
        ),
        json_schema_extra={
            "aliases": [
                "evidenceAttestationId",
                "evidence_attestation_id",
                "attestationId",
                "attestation_id",
            ]
        },
    )


@tool(
    "waive_attestation",
    "Last-resort waiver of the attestation gate (coordinator/CEO). "
    "Requires evidenceAttestationId citing a real test_run/browse_e2e/"
    "visual_check/doc_review row. Max 2 waivers per task. The waiving agent "
    "CANNOT later approve the same task (third-party isolation). "
    "Prefer attest_doc_review for document/spec VERIFY instead of waiving.",
    requires_workspace=False,
    security_level="standard",
)
async def waive_attestation_tool(
    params: WaiveAttestationParams, agent_id: str, workspace: str
) -> ToolResult:
    """Coordinator 显式豁免任务的 attestation 门禁（可审计的正式通道）。

    替代过去的 charter 口头豁免（工具层不读 charter，口头豁免无效）。
    """
    from hiveweave.services.attestation import (
        BROWSE_E2E_KIND,
        MAX_WAIVERS_PER_TASK,
        VISUAL_CHECK_KIND,
        WAIVER_EVIDENCE_KINDS,
        attestation_service,
        count_waivers,
        create_waiver,
        has_valid_waiver,
    )
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import infer_role_family

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "waive_attestation requires a non-empty 'reason' (auditability)."
        )
    if len(reason) < 20:
        return ToolResult.err(
            "waive_attestation reason too short (min 20 chars). "
            "State what was checked and why machine attestation cannot apply."
        )

    evidence_id = (params.evidence_attestation_id or "").strip()
    if not evidence_id:
        return ToolResult.err(
            "waive_attestation requires evidenceAttestationId — cite a "
            "test_run / browse_e2e / visual_check / doc_review attestation. "
            "read_file-only review is not accepted (TEST6 P0-2)."
        )

    ts = _task_svc.TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load task: {e}")
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    # Lifetime cap — escape hatch must stay narrower than the front door
    prior = await count_waivers(project_id, params.task_id)
    if prior >= MAX_WAIVERS_PER_TASK:
        return ToolResult.err(
            f"waive_attestation rejected: task {params.task_id} already has "
            f"{prior} waiver(s) (max {MAX_WAIVERS_PER_TASK}). "
            "Obtain real attestation evidence instead."
        )
    if await has_valid_waiver(project_id, params.task_id):
        return ToolResult.err(
            f"waive_attestation rejected: task {params.task_id} already has "
            "an unexpired waiver. Wait for expiry or approve via a different "
            "agent (waived_by cannot approve)."
        )

    # Evidence attestation must be a real execution kind (not another waiver)
    try:
        await attestation_service.ensure_schema(project_id)
        ev = await attestation_service.get(project_id, evidence_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load evidence attestation: {e}")
    if not ev:
        return ToolResult.err(
            f"evidenceAttestationId not found: {evidence_id}"
        )
    ev_kind = (ev.get("kind") or "").strip()
    if ev_kind not in WAIVER_EVIDENCE_KINDS:
        return ToolResult.err(
            f"evidenceAttestationId kind '{ev_kind}' is not execution evidence. "
            f"Allowed: {sorted(WAIVER_EVIDENCE_KINDS)}."
        )
    ev_task = ev.get("task_id")
    if not ev_task or str(ev_task) != str(params.task_id):
        return ToolResult.err(
            f"evidenceAttestationId must be bound to this task "
            f"(attestation task_id={ev_task!r}, waive for {params.task_id}). "
            "Null/mismatched evidence cannot unlock an arbitrary task."
        )
    ev_exit = ev.get("exit_code")
    if (
        ev_exit is not None
        and int(ev_exit) != 0
        and ev_kind in ("test_run", VISUAL_CHECK_KIND, BROWSE_E2E_KIND)
    ):
        return ToolResult.err(
            f"evidenceAttestationId {evidence_id} is a failed {ev_kind} "
            f"(exit_code={ev_exit}); cannot unlock waiver."
        )

    tags = task.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    from hiveweave.services.attestation import resolve_task_policy

    policy_id = task.get("policy_id") or resolve_task_policy(
        title=task.get("title") or "",
        tags=tags if isinstance(tags, list) else [],
        description=task.get("description") or "",
    )
    if policy_id == "docs_only" or (
        isinstance(tags, list) and "docs_only" in tags
    ):
        return ToolResult.err(
            f"Cannot waive docs_only task {params.task_id}: "
            "use attest_doc_review(taskId, files=[{{path}}]) then "
            "submit/approve with attestationIds. Waiver is blocked for "
            "document tasks."
        )

    is_verify = ts._is_verify_task(task)
    if is_verify:
        agent = await OrgService().get_agent(agent_id)
        family = infer_role_family(agent or {})
        if family != "ceo":
            return ToolResult.err(
                f"VERIFY task {params.task_id}: only CEO may waive_attestation "
                "(identity / attestation last resort). Coordinators must require "
                "test_run / browse_e2e attestationIds, or escalate to CEO with "
                "an auditable reason."
            )

    try:
        waiver_id = await create_waiver(
            project_id,
            task_id=params.task_id,
            waived_by=agent_id,
            reason=f"[evidence={evidence_id}] {reason}",
        )
    except Exception as e:
        return ToolResult.err(f"Failed to create waiver: {e}")

    # Best-effort: mark / ensure verification case when this is a VERIFY task
    try:
        if is_verify:
            from hiveweave.services.task import VerificationCaseService

            parent_id = task.get("parent_task_id") or params.task_id
            vcs = VerificationCaseService()
            await vcs.ensure_case(
                project_id,
                original_task_id=parent_id,
                verify_task_id=params.task_id,
            )
            await vcs.mark_waived(
                project_id,
                parent_id,
                reason=reason,
                verify_task_id=params.task_id,
            )
    except Exception:
        pass

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
        is_verify=is_verify,
    )
    return ToolResult.ok(
        f"Attestation waived for task {params.task_id} "
        f"(waiver {waiver_id[:8]}, expires in 24h).\n"
        f"Stored reason (quote this in reports): {reason}\n"
        f"Assignee may now submit_task without attestationIds."
        + (
            " VERIFY waive is CEO-only and leaves an auditable verification_case."
            if is_verify
            else ""
        )
    )


# ── waive_merge ───────────────────────────────────────────────


class WaiveMergeParams(BaseModel):
    """Parameters for waive_merge tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the approved task whose merge-before-close gate "
        "should be waived.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    reason: str = Field(
        description=(
            "Auditable reason merge is not required (min 20 chars). "
            "E.g. docs-only delivery already on main via prior commit abc123."
        ),
    )


@tool(
    "waive_merge",
    "Last-resort waiver of the merge-before-close hard gate "
    "(coordinator/CEO). Prefer git_worktree_merge. After waiving, "
    "close_task may proceed; evidence records merge_waived for audit.",
    requires_workspace=False,
    security_level="standard",
)
async def waive_merge_tool(
    params: WaiveMergeParams, agent_id: str, workspace: str
) -> ToolResult:
    """Explicit merge exemption — the only escape from close hard gate."""
    import time as _time

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "waive_merge requires a non-empty 'reason' (auditability)."
        )
    if len(reason) < 20:
        return ToolResult.err(
            "waive_merge reason too short (min 20 chars). "
            "State what was checked and why merge is not required."
        )

    ts = _task_svc.TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:
        return ToolResult.err(f"Failed to load task: {e}")
    if not task:
        return ToolResult.err(f"Task not found: {params.task_id}")

    if ts._is_verify_task(task):
        return ToolResult.err(
            f"VERIFY task {params.task_id}: waive_merge does not apply "
            "(VERIFY has no merge step). Use waive_attestation if needed."
        )

    ev = task.get("evidence") or {}
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    if not isinstance(ev, dict):
        ev = {}
    ev["merge_waived"] = True
    ev["merge_waive_reason"] = reason[:500]
    ev["merge_waived_by"] = agent_id
    ev["merge_waived_at"] = int(_time.time() * 1000)

    try:
        from hiveweave.services import task as task_module

        await task_module._execute(
            project_id,
            "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
            [json.dumps(ev), int(_time.time() * 1000), params.task_id],
        )
    except Exception as e:
        return ToolResult.err(f"Failed to stamp merge waiver: {e}")

    # Fulfill pending merge obligation so exit gates clear
    try:
        from hiveweave.services.obligation import ObligationLedger

        await ObligationLedger().fulfill(project_id, params.task_id, "merge")
    except Exception as e:
        log.warning("waive_merge_fulfill_failed", error=str(e))

    log.info(
        "merge_waived",
        project_id=project_id,
        task_id=params.task_id,
        waived_by=agent_id,
        reason=reason[:120],
    )
    return ToolResult.ok(
        f"Merge waived for task {params.task_id}. "
        f"Stored reason: {reason}\n"
        f"close_task / approve auto-close may now proceed without "
        f"git_worktree_merge. Prefer merging next time when there is "
        f"real code delivery."
    )

