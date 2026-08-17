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
    "approve requires FRESH evidence kinds for this task's submitGate/policy — not bare testsPassed. "
    "Prefer consuming the assignee's hung attestations (unit→test_run, module_visual→browse/visual, "
    "docs→doc_review, code_audit*→code_audit). CEO: review-only — do not bash/self-test or merge leaf trees. "
    "If approve is rejected for missing evidence, do NOT retry approve — rework or wait for the gate. "
    "Does NOT spawn VERIFY. After a milestone is on MAIN, dispatch_task(..., milestoneVerify=true) "
    "for one QA task. VERIFY waive is CEO-only. docs_only: coordinators use "
    "attest_doc_review (waive rejected); CEO may waive that one taskId.",
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
        waive_self_approve_small_team = False
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
                ledger_policy_id,
                required_attestation_kinds,
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
            policy_id = ledger_policy_id(task)
            needed = required_attestation_kinds(policy_id)
            from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

            needed, _ = drop_code_audit_kind_if_soft(
                needed,
                agent_id=str(task.get("assignee_id") or "") or None,
                task_id=params.task_id,
                evidence=evidence,
            )
            from hiveweave.services.attestation import has_valid_waiver

            waived = await has_valid_waiver(project_id, params.task_id)
            # TEST6 P0-2: waived_by must not be the approver
            # TEST6 audit S2: small-team sole REVIEW holder may self-approve
            # after their own waiver (stamp override=waive_self_approve_small_team).
            waive_self_approve_small_team = False
            if waived and waiver_row:
                waived_by = str(waiver_row.get("agent_id") or "")
                if waived_by and waived_by == str(agent_id):
                    from hiveweave.services.unblock_soft import (
                        is_org_lookup_failed,
                        is_small_team_sole_reviewer,
                        no_lawful_approver,
                    )

                    sole = await is_small_team_sole_reviewer(
                        project_id,
                        assignee_id=str(task.get("assignee_id") or "") or None,
                        reviewer_id=str(agent_id),
                    )
                    if sole:
                        waive_self_approve_small_team = True
                    else:
                        deadlock = await no_lawful_approver(
                            project_id, task, waiver_row=waiver_row
                        )
                        extra = ""
                        if deadlock and not is_org_lookup_failed(deadlock):
                            extra = f"\nDEADLOCK: {deadlock}"
                        return ToolResult.err(
                            "Cannot approve: you issued the waiver for this task. "
                            "waive→approve requires a third party "
                            f"(waived_by={waived_by[:8]}…). "
                            "Ask another coordinator/CEO to approve, or obtain "
                            "real reviewer test_run attestation and approve "
                            "without relying on your own waiver."
                            + extra
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
                    # P0-3: be explicit about current state so the reviewer
                    # does not mis-think they are waived_by. No active waiver
                    # exists here (this branch is `not waived`); the blocker
                    # is missing/stale attestation evidence, NOT third-party
                    # isolation from a waiver they issued.
                    return ToolResult.err(
                        f"Cannot approve: attestation gate failed ({policy_id}): {err}. "
                        f"taskId={tid} (use this full id).\n"
                        f"Required kind(s): {kinds}.\n"
                        f"Current state: NO active waiver on this task — "
                        f"you are NOT blocked by waived_by third-party rule. "
                        f"The blocker is missing/stale attestation evidence.\n"
                        f"Options:\n"
                        f"1) For docs_only: attest_doc_review(taskId, files=[{{path}}]) "
                        f"then approve with those attestationIds.\n"
                        f"2) Send rework; require assignee to attach real "
                        f"browse/test/doc_review attestationIds on resubmit.\n"
                        f"3) Last resort: waive_attestation(taskId=\"{tid}\", "
                        f"evidenceAttestationId=\"<test_run|browse_e2e id>\", "
                        f"reason=\"<why THIS task is exempt>\"). Coordinators "
                        f"must cite evidence. CEO may omit evidenceAttestationId "
                        f"after looking at this task. One taskId only — cannot "
                        f"waive all tasks. After waiving YOU cannot approve "
                        f"(waived_by cannot approve); a *different* "
                        f"REVIEW holder must approve. Use get_tasks to see "
                        f"current waiver state before deciding."
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
                        "Do NOT retry approve without new evidence. "
                        "Prefer attest_doc_review for "
                        "document VERIFY instead of waiving."
                    )

            # P0-2: Reviewer-side execution evidence gate.
            # The submitter's attestation proves THEY ran tests. The reviewer
            # must ALSO have executed independently (run_tests / bash test cmd)
            # before approving. "12-second approve" with zero reviewer commands
            # is the root cause of P0-1's CHANGELOG loss going undetected.
            #
            # TEST6 audit S1: CEO has no TEST_RUN — primary path is consume
            # of assignee/subordinate evidence (incl. ancestor task binding),
            # not "run tests yourself".
            from hiveweave.services.attestation import (
                ancestor_task_ids,
                find_reviewer_attestation,
                format_attestation_mismatch_hint,
                list_reviewer_attestations_diag,
                reviewer_required_kinds,
            )
            from hiveweave.services.policy import (
                Capability,
                has_capability,
            )

            rev_needed = reviewer_required_kinds(policy_id)
            if rev_needed and not waived:
                consume_ids: list[str] = []
                asg = task.get("assignee_id")
                if asg and str(asg) != str(agent_id):
                    consume_ids.append(str(asg))
                # S1 audit: also consume QA agents (evidence may sit on an
                # independent tester, not only the assignee).
                # P1: extend to ANY active agent holding TEST_RUN capability
                # (incl. builder coordinator). In small teams the coordinator
                # often runs tests freshest, but their attestation could not
                # be consumed by a TEST_RUN-less CEO — narrowing the escape
                # path and causing deadlocks (TEST18 3-VERIFY stall).
                try:
                    from hiveweave.services.org import OrgService

                    for a in (await OrgService().list_agents(project_id)) or []:
                        if (a.get("status") or "").lower() == "archived":
                            continue
                        aid = str(a.get("id") or "")
                        if not aid or aid == str(agent_id) or aid in consume_ids:
                            continue
                        if has_capability(a, Capability.TEST_RUN):
                            consume_ids.append(aid)
                except Exception:
                    pass

                reviewer_row = None
                try:
                    from hiveweave.services.org import OrgService

                    reviewer_row = await OrgService().get_agent(agent_id)
                except Exception:
                    reviewer_row = None
                can_test = bool(
                    reviewer_row and has_capability(reviewer_row, Capability.TEST_RUN)
                )
                # CEO / no-TEST_RUN: consume-only (do not count own attestation).
                reviewer_must_hold = can_test
                extra_tids = await ancestor_task_ids(project_id, params.task_id)

                has_rev = await find_reviewer_attestation(
                    project_id,
                    params.task_id,
                    agent_id,
                    rev_needed,
                    consume_agent_ids=consume_ids or None,
                    extra_task_ids=extra_tids or None,
                    reviewer_must_hold=reviewer_must_hold,
                )
                if not has_rev:
                    tid = task.get("id") or params.task_id
                    kinds_str = ", ".join(sorted(rev_needed))
                    held = await list_reviewer_attestations_diag(
                        project_id, agent_id, kinds=rev_needed
                    )
                    # S6+: for CEO path, also surface consume-side holdings
                    consume_held: list[dict] = []
                    if not can_test:
                        for cid in consume_ids[:6]:
                            rows = await list_reviewer_attestations_diag(
                                project_id, cid, kinds=rev_needed, limit=4
                            )
                            for r in rows:
                                r = dict(r)
                                r["holder"] = cid[:8]
                                consume_held.append(r)
                    mismatch = format_attestation_mismatch_hint(
                        held, target_task_id=str(tid)
                    )
                    if consume_held:
                        mismatch += (
                            "\nConsume-side fresh attestation(s) "
                            "(assignee/QA — check task binding):"
                        )
                        for h in consume_held[:6]:
                            bound = h.get("task_id") or "(unbound)"
                            mismatch += (
                                f"\n  - holder={h.get('holder')} "
                                f"{h.get('kind')} id={str(h.get('id') or '')[:8]}… "
                                f"bound_task={str(bound)[:8]}"
                            )
                    from hiveweave.services.unblock_soft import (
                        is_org_lookup_failed,
                        no_lawful_approver,
                    )

                    deadlock = await no_lawful_approver(
                        project_id, task, waiver_row=waiver_row
                    )
                    deadlock_line = ""
                    if deadlock and not is_org_lookup_failed(deadlock):
                        deadlock_line = (
                            f"\nDEADLOCK: {deadlock} "
                            "Escape: cancel_task with reason≥20 chars "
                            "(stamps cancelled_in_deadlock), or hire another "
                            "REVIEW holder, or obtain assignee/QA test_run "
                            "on this task."
                        )
                    if not can_test:
                        return ToolResult.err(
                            f"Cannot approve: you lack TEST_RUN capability "
                            f"(role cannot self-produce test evidence). "
                            f"Do NOT retry approve without new evidence — "
                            f"use option 1 (consume) or 2 (waive) below.\n"
                            f"CEO/management path is to consume assignee/QA "
                            f"fresh evidence on this task (kinds follow submitGate).\n"
                            f"Required kind(s): {kinds_str}. taskId={tid}.\n"
                            f"{mismatch}\n"
                            f"Options:\n"
                            f"1) Require the assignee to hang the gate's attestations "
                            f"(rework if missing). Do not dispatch QA to unlock "
                            f"this mid-level gate.\n"
                            f"2) waive_attestation(taskId=\"{tid}\", …) — "
                            f"then a *different* agent must approve, unless "
                            f"you are the sole REVIEW holder beside assignee "
                            f"(small-team exemption)."
                            + deadlock_line
                        )
                    return ToolResult.err(
                        f"Cannot approve: no fresh evidence on this task matching "
                        f"submitGate ({kinds_str}).\n"
                        f"Do NOT retry approve. Do NOT run full-site tests yourself "
                        f"to unlock a leaf gate, and do not dispatch QA for 取证.\n"
                        f"{mismatch}\n"
                        f"taskId={tid}. Options:\n"
                        f"1) Consume the assignee's hung attestations; if missing, "
                        f"review_task(rework) and tell the leaf to produce the gate "
                        f"kinds (unit→bash taskId, module_visual→browse/assert_visual, "
                        f"code_audit→request_code_audit).\n"
                        f"2) waive_attestation(taskId=\"{tid}\", "
                        f"evidenceAttestationId=\"<id>\", "
                        f"reason=\"...\"). Coordinators must cite evidence; "
                        f"CEO may omit it for THIS task only (not all tasks). "
                        f"Then a *different* agent must approve "
                        f"(you cannot approve your own waiver)."
                        + deadlock_line
                    )

            # (1) Force worktree context for code tasks — VERIFY stays on MAIN
            # (audit P0-1: do not ensure personal write tree for VERIFY).
            if task.get("assignee_id") and not ts._is_verify_task(task):
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
                # 2026-08-11 slack-clone_01 复盘：files_changed 校验失败后
                # 任务停留在 submitted，assignee 无法重交（状态机拒
                # submitted→submitted），只能等 reviewer 手动 rework。
                # 证据可修（修正 filesChanged / 补 checkpoint），自动转
                # rework，assignee 立即能重交 —— 不自动循环（只转一次）。
                post = await _auto_rework_on_evidence_gate(
                    project_id, params, agent_id, task, ev_deny
                )
                if post == "reworked":
                    return ToolResult.err(
                        f"{ev_deny}\n\n[auto-rework] Task 已自动转回 running："
                        f"修正 filesChanged（只列 worktree 中真实存在的文件）"
                        f"后重新 submit_task。"
                    )
                return ToolResult.err(
                    f"{ev_deny}\n\n[auto-rework failed] 任务未能自动转回 "
                    f"running（当前状态 {post}）。请按上面的选项处理，或"
                    f"手动 review_task(decision='rework')。"
                )

        # TEST6 evening P1-3: VERIFY approve requires attestation on
        # target_merge_commit / current main tip (not a stale personal tip).
        # TEST18 P0-2: max_behind=5 ancestor window — merge 后 worktree 祖先
        # commit（含 merge 代码、落后 ≤5）的 attestation 也接受，消除
        # "worktree 跑测必拒"误伤；方向由 check_verify_baseline 的
        # target-is-ancestor 收紧把关（pre-merge base 不放行）。
        if decision == "approve" and ts._is_verify_task(task):
            from hiveweave.services.attestation import check_verify_baseline

            baseline_err = await check_verify_baseline(
                project_id, task, max_behind=5
            )
            if baseline_err:
                return ToolResult.err(baseline_err)

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

        # TEST6 S11 / TEST18 NEW-7: fulfill review obligation on approve OR
        # rework — either decision completes the review act; rework starts a
        # fresh clock when the assignee resubmits.
        if decision in ("approve", "rework"):
            try:
                from hiveweave.services.obligation import ObligationLedger

                await ObligationLedger().fulfill(
                    project_id, params.task_id, "review"
                )
            except Exception as e:
                log.warning(
                    "review_obligation_fulfill_failed",
                    task_id=params.task_id,
                    decision=decision,
                    error=str(e),
                )

        # TEST13 P0-1 / TEST6 S2: audit stamp for escalation overrides
        if decision == "approve" and (ceo_merger_override or waive_self_approve_small_team):
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
                if ceo_merger_override:
                    ev["override"] = "no_independent_reviewer"
                    ev["ceo_merger_override"] = True
                    ev["ceo_merger_override_by"] = agent_id
                if waive_self_approve_small_team:
                    ev["override"] = "waive_self_approve_small_team"
                    ev["waive_self_approve_small_team"] = True
                    ev["waive_self_approve_by"] = agent_id
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
                log.warning("review_override_stamp_failed", error=str(e))


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
                        # TEST18 NEW-2: single merge owner = reviewer (MERGE
                        # PENDING). Assignee waits — do not dual-assign.
                        msg = (
                            f"[TASK APPROVED] Task '{title}' has been approved. "
                            f"Wait for the reviewer/coordinator to "
                            f"git_worktree_merge your worktree"
                            f"{f' ({wt_path})' if wt_path else ''}. "
                            f"Do NOT merge yourself — dual merge owners cause "
                            f"the second merge to fail after the tree is torn "
                            f"down. VERIFY runs after merge."
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
                    a = await OrgService().resolve_agent(asg)  # type: ignore[assignment]
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
                    f"git_worktree_checkpoint before merge; do NOT treat "
                    f"as already-merged. Next: confirm checkpoint, then the "
                    f"merge owner (task creator/coordinator) runs "
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
                f"VERIFY is NOT auto-created on merge. "
                f"Next (merge owner = task creator/coordinator): "
                f"git_worktree_merge(branchName='{short or 'hw/<short_id>/...'}'). "
                f"After the milestone is on MAIN, dispatch_task(..., "
                f"milestoneVerify=true, submitGate=module_visual|unit) "
                f"to one QA. If you are NOT the task creator, relay merge "
                f"to the creator. "
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
    """Wake the merge owner to git_worktree_merge (same-turn follow-up).

    2026-08-11 slack-clone_01 复盘：即时 wake 曾发给 reviewer（审批人），
    但 merge 职责与后续清理（verify.py ``_clear_merge_pending_inbox`` 只清
    creator 的 inbox）都在 creator —— 第三方代审场景（reviewer ≠ creator
    ≠ implementer）下审批人既不该也做不了 merge，消息成纯噪音且 merge
    完成后永不清理。与 game_time stale nudge（发 creator）一致化：
    接收者 = creator，fallback reviewer。
    """
    tid = str(task.get("id") or "")
    title = (task.get("title") or "(untitled)").split("\n")[0][:60]
    branch = short_id or "hw/<short_id>/..."
    # 2026-08-11 slack-clone_01 复盘：merge 职责在 creator（与 stale nudge /
    # 清理逻辑一致），审批人（尤其第三方代审）不应背 merge 义务。统一走
    # resolve_merge_owner（排除 API 人类 creator 哨兵，fallback 审批人）。
    from hiveweave.services.tasks.verify import resolve_merge_owner

    recipient = resolve_merge_owner(task, reviewer_id) or reviewer_id
    body = (
        f"[MERGE PENDING] Task '{title}' ({tid[:8]}) is approved and needs "
        f"git_worktree_merge(branchName='{branch}'). "
        f"YOU (task creator/coordinator, merge owner) must merge — do not "
        f"ask the executor to merge on main. "
        f"reason={reason}"
    )
    try:
        from hiveweave.services.inbox import InboxService

        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=recipient,
            message=body,
            message_type="task",
            priority="urgent",
            task_id=tid or None,
            wake=True,
        )
        from hiveweave.agents.trigger import trigger_coordinator

        await trigger_coordinator(recipient)
        log.info(
            "merge_pending_wake_injected",
            reviewer_id=reviewer_id,
            recipient=recipient,
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
    # Owner = merge 职责方（recipient，与即时 wake 同一人），不是审批人。
    try:
        from hiveweave.services.obligation import ObligationLedger

        await ObligationLedger().create(
            project_id=project_id,
            owner_agent_id=recipient,
            obligation_type="merge",
            task_id=tid or None,
            context={"short_id": short_id, "reason": reason},
        )
    except Exception as e:
        log.warning(
            "merge_obligation_create_failed",
            reviewer_id=reviewer_id,
            recipient=recipient,
            task_id=tid,
            error=str(e),
        )


async def _auto_rework_on_evidence_gate(
    project_id: str,
    params: Any,
    agent_id: str,
    task: dict | None,
    deny_msg: str,
) -> str:
    """Approval evidence gate（files_changed 校验）失败 → 自动转 rework。

    2026-08-11 slack-clone_01 复盘：gate 拒绝后任务停留 submitted，
    assignee 无法重交（状态机拒 submitted→submitted），只能等 reviewer
    手动 rework —— 团队在 B3 上死等 20 分钟。证据可修，自动转回 running，
    assignee 立即能重交。失败静默（不阻断 approve 的错误返回）。

    Returns the task's post-state ("reworked" when both transitions ran,
    otherwise the actual status string).
    """
    try:
        from hiveweave.agents.trigger import trigger_subordinate
        from hiveweave.services.inbox import InboxService

        ts = _task_svc.TaskService()
        tid = params.task_id
        cur = (await ts.get_task(project_id, tid) or {}).get("status")
        if cur == "submitted":
            await ts.start_review(project_id, tid, reviewer_id=agent_id)
            cur = (await ts.get_task(project_id, tid) or {}).get("status")
        if cur in ("submitted", "reviewing"):
            await ts.review_task(
                project_id,
                tid,
                "rework",
                params.feedback or deny_msg[:500],
                reviewer_id=agent_id,
            )
            try:
                from hiveweave.services.obligation import ObligationLedger

                await ObligationLedger().fulfill(project_id, tid, "review")
            except Exception:
                pass
        assignee_id = (task or {}).get("assignee_id")
        if assignee_id:
            await InboxService().send_message(
                from_agent_id=agent_id,
                to_agent_id=assignee_id,
                message=(
                    f"[REWORK REQUESTED] Task '{str(tid)[:8]}' auto-reworked "
                    f"by approval evidence gate: {deny_msg[:200]}"
                ),
                message_type="task",
                priority="urgent",
                task_id=tid,
                wake=True,
            )
            await trigger_subordinate(assignee_id)
        return (await ts.get_task(project_id, tid) or {}).get("status") or "unknown"
    except Exception as e:
        log.warning(
            "review_auto_rework_evidence_gate_failed",
            task_id=str(getattr(params, "task_id", "")),
            error=str(e),
        )
        try:
            return (await _task_svc.TaskService().get_task(project_id, params.task_id) or {}).get("status") or "unknown"
        except Exception:
            return "unknown"

