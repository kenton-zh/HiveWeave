"""review_task tool + merge-pending wake

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

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    try:
        ts = _task_svc.TaskService()
        task = await ts.get_task(project_id, params.task_id)
        if not task:
            return ToolResult.err(f"Task not found: {params.task_id}")

        # B3: 归档任务写保护 —— 已归档任务不可审批
        if task.get("is_archived"):
            return ToolResult.err(
                f"Task {params.task_id[:8]} is archived and cannot be reviewed. "
                f"It has been cancelled by a coordinator."
            )

        # D1: VERIFY 审门 —— 如果有有效的 waiver，跳过所有身份门禁。
        # 小团队可能没有合法的第四方独立审查员。waive_attestation 作为
        # CEO override 通道，审计痕迹留在 waiver 表中。
        # TEST6 P0-2: 豁免人不得与批准人相同（waive→approve 需第三人）。
        verify_waived = False
        waiver_row = None
        ceo_merger_override = False
        if ts._is_verify_task(task):
            from hiveweave.services.attestation import get_valid_waiver

            waiver_row = await get_valid_waiver(project_id, params.task_id)
            verify_waived = waiver_row is not None

        # Also load waiver for non-VERIFY approve path (P0-2 isolation)
        if waiver_row is None:
            from hiveweave.services.attestation import get_valid_waiver

            waiver_row = await get_valid_waiver(project_id, params.task_id)

        # Hard gate: 禁自审 —— reviewer 不得等于 assignee。
        # 有 waiver 时跳过（D1: CEO override 通道）
        # Hard gate: assignee == reviewer is NEVER waived (third-party isolation).
        # Waiver only unlocks attestation / VERIFY independent-reviewer set.
        assignee_id = task.get("assignee_id")
        if assignee_id and str(assignee_id) == str(agent_id):
            from hiveweave.services.unblock_soft import soft_reminder_after_self_review_deny

            extra = soft_reminder_after_self_review_deny(has_waiver=bool(waiver_row))
            return ToolResult.err(
                "Self-review is forbidden: you are the assignee of this task. "
                "Your submission goes to your superior (or task creator) for "
                "review — do not approve your own deliverable."
                + extra
            )

        if ts._is_verify_task(task) and not verify_waived:
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
            # TEST13 P0-1: CEO may approve even when they are the merger
            # (small-team escalation). Self-review (assignee==reviewer) still
            # blocked above. Stamp evidence.override for audit.
            ceo_merger_override = False
            try:
                from hiveweave.services.org import OrgService
                from hiveweave.services.policy import infer_role_family

                reviewer_row = await OrgService().get_agent(agent_id)
                if (
                    infer_role_family(reviewer_row or {}) == "ceo"
                    and str(agent_id) in forbidden
                ):
                    forbidden.discard(str(agent_id))
                    ceo_merger_override = True
            except Exception:
                pass
            if str(agent_id) in forbidden:
                return ToolResult.err(
                    "VERIFY approval must come from the CEO or an independent "
                    "reviewer — the implementer / merger of the parent task "
                    "cannot approve its verification. If you are CEO and also "
                    "merged the parent, retry (CEO escalation is allowed); "
                    "otherwise hire an independent reviewer or "
                    "reassign_task the VERIFY to a QA then have CEO/reviewer approve."
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
            from hiveweave.services.attestation import has_valid_waiver

            waived = await has_valid_waiver(project_id, params.task_id)
            # TEST6 P0-2: waived_by must not be the approver
            if waived and waiver_row:
                waived_by = str(waiver_row.get("agent_id") or "")
                if waived_by and waived_by == str(agent_id):
                    return ToolResult.err(
                        "Cannot approve: you issued the waiver for this task. "
                        "waive→approve requires a third party "
                        f"(waived_by={waived_by[:8]}…). "
                        "Ask another coordinator/CEO to approve, or obtain "
                        "real reviewer test_run attestation and approve "
                        "without relying on your own waiver."
                    )
            if needed and not waived:
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
                    kinds = ", ".join(sorted(needed))
                    return ToolResult.err(
                        f"Cannot approve: attestation gate failed ({policy_id}): {err}. "
                        f"taskId={tid} (use this full id).\n"
                        f"Required kind(s): {kinds}.\n"
                        f"Options:\n"
                        f"1) For docs_only: attest_doc_review(taskId, files=[{{path}}]) "
                        f"then approve with those attestationIds.\n"
                        f"2) Send rework; require assignee to attach real "
                        f"browse/test/doc_review attestationIds on resubmit.\n"
                        f"3) Last resort: waive_attestation(taskId=\"{tid}\", "
                        f"evidenceAttestationId=\"<test_run|browse_e2e id>\", "
                        f"reason=\"<why exempt>\") then a *different* agent "
                        f"approves (waived_by cannot approve)."
                    )
            elif not needed and not waived and evidence.get("tests_passed") is not True:
                # Soft path: tests_passed ack OR any real attestationIds OR waiver
                aids = evidence.get("attestation_ids") or []
                if not isinstance(aids, list):
                    aids = []
                soft_ok = False
                if aids:
                    soft_ok, _ = await attestation_service.verify_ids(
                        project_id,
                        [str(x) for x in aids],
                        task_id=params.task_id,
                    )
                if not soft_ok:
                    return ToolResult.err(
                        "Cannot approve without tests_passed=true, "
                        "attestationIds (browse_e2e / test_run / doc_review), "
                        "or waive_attestation(+evidenceAttestationId). "
                        "Prefer attest_doc_review for "
                        "document VERIFY instead of waiving."
                    )

            # P0-2: Reviewer-side execution evidence gate.
            # The submitter's attestation proves THEY ran tests. The reviewer
            # must ALSO have executed independently (run_tests / bash test cmd)
            # before approving. "12-second approve" with zero reviewer commands
            # is the root cause of P0-1's CHANGELOG loss going undetected.
            from hiveweave.services.attestation import (
                find_reviewer_attestation,
                reviewer_required_kinds,
            )

            rev_needed = reviewer_required_kinds(policy_id)
            if rev_needed and not waived:
                # Soft unblock (facts): CEO/reviewer may consume same-task
                # assignee fresh test_run — not a structured next-action command.
                consume_ids: list[str] = []
                asg = task.get("assignee_id")
                if asg and str(asg) != str(agent_id):
                    consume_ids.append(str(asg))
                has_rev = await find_reviewer_attestation(
                    project_id,
                    params.task_id,
                    agent_id,
                    rev_needed,
                    consume_agent_ids=consume_ids or None,
                )
                if not has_rev:
                    tid = task.get("id") or params.task_id
                    kinds_str = ", ".join(sorted(rev_needed))
                    return ToolResult.err(
                        f"Cannot approve: YOU (the reviewer) have no fresh "
                        f"execution evidence on this task.\n"
                        f"Required reviewer kind(s): {kinds_str}.\n"
                        f"Before approving, run the project's tests yourself "
                        f"(e.g. `bash`/`run_command` with taskId=\"{tid}\") "
                        f"so the platform records your attestation.\n"
                        f"taskId={tid}. Options:\n"
                        f"1) Run tests in the assignee's worktree or on main "
                        f"(after merge), then approve again.\n"
                        f"2) waive_attestation(taskId=\"{tid}\", "
                        f"evidenceAttestationId=\"<id>\", "
                        f"reason=\"...\") as last resort — then a *different* "
                        f"agent must approve (you cannot approve your own waiver)."
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

            # TEST11 evening: structured evidence verifiability
            # (files_changed existence + acceptance_criteria path tokens)
            from hiveweave.services.worktree_review import (
                check_evidence_verifiable,
            )

            ev_deny = await check_evidence_verifiable(
                project_id, task, evidence
            )
            if ev_deny:
                return ToolResult.err(ev_deny)

        current_status = task["status"]
        if decision == "approve":
            if current_status == "submitted":
                await ts.start_review(project_id, params.task_id, reviewer_id=agent_id)
            elif current_status != "reviewing":
                return ToolResult.err(
                    f"Task must be 'submitted' or 'reviewing' to approve, "
                    f"but is '{current_status}'"
                )
        else:
            # rework from reviewing (normal) or approved (post-approve merge conflict)
            if current_status == "submitted":
                await ts.start_review(project_id, params.task_id, reviewer_id=agent_id)
            elif current_status not in ("reviewing", "approved"):
                return ToolResult.err(
                    f"Task must be 'submitted', 'reviewing', or 'approved' "
                    f"to rework, but is '{current_status}'"
                )
        await ts.review_task(
            project_id, params.task_id, decision, params.feedback,
            reviewer_id=agent_id,
        )

        # TEST13 P0-1: audit stamp when CEO approved as merger
        if decision == "approve" and ceo_merger_override:
            try:
                import time as _time

                task_ev = await ts.get_task(project_id, params.task_id)
                ev = task_ev.get("evidence") if task_ev else {}
                if isinstance(ev, str):
                    try:
                        ev = json.loads(ev)
                    except Exception:
                        ev = {}
                if not isinstance(ev, dict):
                    ev = {}
                ev["override"] = "no_independent_reviewer"
                ev["ceo_merger_override"] = True
                ev["ceo_merger_override_by"] = agent_id
                from hiveweave.services import task as task_module

                await task_module._execute(
                    project_id,
                    "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                    [
                        json.dumps(ev),
                        int(_time.time() * 1000),
                        params.task_id,
                    ],
                )
            except Exception as e:
                log.warning("ceo_merger_override_stamp_failed", error=str(e))


        # ── 通知 assignee/executor 审查结果 ──
        task_after = await ts.get_task(project_id, params.task_id)
        if task_after and task_after.get("assignee_id"):
            assignee_id = task_after["assignee_id"]
            if assignee_id != agent_id:
                from hiveweave.services.inbox import InboxService
                inbox = InboxService()
                if decision == "approve":
                    from hiveweave.services.worktree_review import agent_worktree_path
                    from hiveweave.services.org import OrgService
                    from hiveweave.services.policy import infer_role_family

                    wt_path = await agent_worktree_path(assignee_id)
                    # TEST16 P1-1: role-aware approve notification
                    assignee_row = await OrgService().get_agent(assignee_id)
                    family = infer_role_family(assignee_row or {})
                    title = task_after.get('title', '')[:60]
                    if family in ("coordinator", "ceo"):
                        msg = (
                            f"[TASK APPROVED] Task '{title}' has been approved. "
                            f"You are the merge owner — please run "
                            f"git_worktree_merge on your worktree"
                            f"{f' ({wt_path})' if wt_path else ''} to land "
                            f"changes on main. CEO may exercise merge fallback "
                            f"if you stall. VERIFY runs after merge."
                        )
                    else:
                        msg = (
                            f"[TASK APPROVED] Task '{title}' has been approved. "
                            f"Wait for your coordinator to git_worktree_merge "
                            f"your worktree"
                            f"{f' ({wt_path})' if wt_path else ''}. "
                            f"VERIFY runs only AFTER merge lands on main — do "
                            f"not self-verify. If merge conflicts, you will get "
                            f"rework to rebase/merge main in YOUR worktree "
                            f"(not on main)."
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

            # Auto-close only with merge/no-code fact + clean zero-ahead tree.
            # Clean + 0 ahead + no fact = zero delivery (TEST20 N1) — keep open.
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
            from hiveweave.services.worktree_review import (
                _rel_paths,
                effective_delivery,
                evidence_has_merge_fact,
            )
            from hiveweave.services.task import MergeRequiredError

            has_claimed_files = bool(_rel_paths(list(claimed_files or [])))
            delivery = None
            if main_ws and wt:
                try:
                    delivery = await effective_delivery(main_ws, wt)
                except Exception:
                    delivery = None
            dirty = int((delivery or {}).get("dirty_count") or 0)

            if ahead == 0 and not has_claimed_files and dirty == 0:
                if evidence_has_merge_fact(evidence_after) or (
                    evidence_after.get("no_code_change") is True
                    or evidence_after.get("noCodeChange") is True
                ):
                    try:
                        await ts.close_task(project_id, params.task_id)
                    except MergeRequiredError as gate_err:
                        return ToolResult.err(str(gate_err))
                    except Exception as e:
                        log.warning(
                            "approve_no_diff_close_failed",
                            task_id=params.task_id,
                            error=str(e),
                        )
                        return ToolResult.ok(
                            f"Task {params.task_id} approved; worktree already on "
                            f"main (0 commits ahead, clean). "
                            f"Close manually if needed ({e}). No merge required."
                        )
                    return ToolResult.ok(
                        f"Task {params.task_id} approved and closed — "
                        f"already on main (0 commits ahead, clean, merge/no-code "
                        f"fact present). No git_worktree_merge needed."
                    )
                await _inject_merge_pending_wake(
                    project_id=project_id,
                    reviewer_id=agent_id,
                    task=task_after or task,
                    short_id=short,
                    reason="no_delivery",
                )
                return ToolResult.ok(
                    f"Task {params.task_id} approved but close blocked: "
                    f"no effective delivery (0 commits ahead, clean worktree, "
                    f"no merge fact). Executor must implement + checkpoint, "
                    f"or stamp evidence.no_code_change=true, or "
                    f"waive_merge(reason=…). Not treating as done."
                )

            if (ahead == 0 and has_claimed_files) or dirty > 0:
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
                    f"of main while files_changed is non-empty or worktree is "
                    f"dirty (dirty={dirty}). Executor must "
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

    # TEST16 D2: create structured merge obligation in the ledger.
    # Game tick will escalate to org parent if deadline passes unfulfilled.
    try:
        from hiveweave.services.obligation import ObligationLedger

        await ObligationLedger().create(
            project_id=project_id,
            owner_agent_id=reviewer_id,
            obligation_type="merge",
            task_id=tid or None,
            context={"short_id": short_id, "reason": reason},
        )
    except Exception as e:
        log.warning(
            "merge_obligation_create_failed",
            reviewer_id=reviewer_id,
            task_id=tid,
            error=str(e),
        )

