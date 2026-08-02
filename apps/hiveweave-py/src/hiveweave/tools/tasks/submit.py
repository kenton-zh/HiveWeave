"""submit_task tool

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
    core_interaction_executed: bool | None = Field(
        default=None,
        alias="coreInteractionExecuted",
        description=(
            "UI VERIFY: true when core canvas/DOM interaction was exercised "
            "(browse js/eval or manual attestation). Prefer leaving unset — "
            "platform auto-accepts when a core_interaction browse_e2e "
            "attestation exists for this task."
        ),
        json_schema_extra={
            "aliases": ["coreInteractionExecuted", "core_interaction_executed"]
        },
    )
    failures_acknowledged: list[dict[str, Any]] | None = Field(
        default=None,
        alias="failuresAcknowledged",
        description=(
            "VERIFY only: when testOutput reports N>0 failures, provide a "
            "structured list of {test, reason} entries (one per failing case "
            "or group). Free-text excuses are rejected."
        ),
        json_schema_extra={
            "aliases": [
                "failuresAcknowledged",
                "failures_acknowledged",
                "acknowledgedFailures",
            ]
        },
    )
    commit_hash: str | None = Field(
        default=None,
        alias="commitHash",
        description="Git commit hash on MAIN for VERIFY evidence (optional).",
        json_schema_extra={"aliases": ["commitHash", "commit_hash"]},
    )
    env_snapshot: str | None = Field(
        default=None,
        alias="envSnapshot",
        description="Optional environment snapshot for VERIFY evidence.",
        json_schema_extra={"aliases": ["envSnapshot", "env_snapshot"]},
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
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    task_id = params.task_id
    ts = _task_svc.TaskService()
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

    # B3: 归档任务写保护 —— 已归档任务不可提交
    if task.get("is_archived"):
        return ToolResult.err(
            f"Task {task_id[:8]} is archived and cannot be submitted. "
            f"Use create_task or dispatch_task for new work."
        )

    # B4: 只有 assignee 可以提交任务。creator==assignee 的自交任务
    # 在 task_assignee == agent_id 时已经通过，不需要 creator 例外。
    # creator 例外会让 CEO 代 assignee 提交（代交+自审一条龙），
    # 绕过 ASSIGNEE_MUST_SUBMIT 义务账本。
    task_assignee = task.get("assignee_id")
    if task_assignee and str(task_assignee) != str(agent_id):
        return ToolResult.err(
            f"Only the assignee can submit this task. "
            f"You are not the assignee (assignee={task_assignee[:8]}). "
            f"If you are the creator, use review_task or dispatch_task instead."
        )

    # TEST21 M2: backfill implementer lock for pre-M2 running tasks
    try:
        await ts.lock_implementer_if_needed(project_id, task_id, agent_id)
    except Exception as e:
        log.debug("submit_lock_implementer_failed", error=str(e))

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

    # TEST21 M14: UI VERIFY requires core_interaction evidence
    # TEST19 教训: 只认系统 VERIFY: 前缀（agent 自由 tag verify 不触发）
    is_verify = (task.get("title") or "").startswith("VERIFY:")
    parent_policy = policy_id
    parent_tags: list = []
    if is_verify and task.get("parent_task_id"):
        parent = await ts.get_task(project_id, task["parent_task_id"])
        if parent:
            parent_tags = parent.get("tags") or []
            if isinstance(parent_tags, str):
                try:
                    parent_tags = json.loads(parent_tags)
                except Exception:
                    parent_tags = []
            parent_policy = (
                parent.get("policy_id")
                or resolve_task_policy(
                    title=parent.get("title") or "",
                    tags=parent_tags if isinstance(parent_tags, list) else [],
                    description=parent.get("description") or "",
                )
            )
    ui_verify = is_verify and (
        "ui" in {str(t).lower() for t in (tags if isinstance(tags, list) else [])}
        or parent_policy == "ui_browser_e2e"
        or "ui" in {str(t).lower() for t in (parent_tags if isinstance(parent_tags, list) else [])}
    )
    if ui_verify:
        from hiveweave.services.attestation import find_core_interaction_attestation

        core_att = await find_core_interaction_attestation(
            project_id, task_id, agent_id
        )
        # Also accept any-agent attestation on this task (delegated VERIFY)
        if not core_att:
            core_att = await find_core_interaction_attestation(
                project_id, task_id, None
            )
        has_flag = bool(getattr(params, "core_interaction_executed", None))
        if not core_att:
            return ToolResult.err(
                "UI VERIFY submit rejected: need a browse_e2e attestation "
                "with [core_interaction=1] (boolean flags alone do not unlock).\n"
                "Do:\n"
                f"1) browse(args=[\"js\",\"() => document.querySelector("
                f"'canvas')?.dispatchEvent(new MouseEvent('click'))\"], "
                f"taskId=\"{task_id}\")\n"
                f"2) Then submit_task(..., taskId=\"{task_id}\") — platform "
                f"auto-attaches the core_interaction attestation.\n"
                + (
                    "Note: coreInteractionExecuted=true was set but no matching "
                    "attestation exists — inventing the flag is rejected."
                    if has_flag
                    else ""
                )
            )
        if core_att and core_att not in attest_ids:
            attest_ids.append(core_att)

    # TEST6 P0-3: VERIFY with reported failures must acknowledge structurally
    if is_verify:
        from hiveweave.services.attestation import count_reported_test_failures

        fail_n = count_reported_test_failures(params.test_output)
        if fail_n is not None and fail_n > 0:
            from hiveweave.services.attestation import required_failure_acks

            required = required_failure_acks(fail_n)
            acks = getattr(params, "failures_acknowledged", None) or []
            if not isinstance(acks, list) or len(acks) < required:
                return ToolResult.err(
                    f"VERIFY submit rejected: testOutput reports {fail_n} "
                    f"failed test(s); need failuresAcknowledged with at least "
                    f"{required} {{test, reason}} entries "
                    f"(got {len(acks) if isinstance(acks, list) else 0}). "
                    f"Either rework until green, or acknowledge structurally "
                    f"(free-text excuses alone are not accepted)."
                )
            bad = [
                a for a in acks
                if not isinstance(a, dict)
                or not str(a.get("test") or a.get("name") or "").strip()
                or not str(a.get("reason") or a.get("why") or "").strip()
            ]
            if bad:
                return ToolResult.err(
                    "VERIFY submit rejected: each failuresAcknowledged entry "
                    "must be {test, reason} with non-empty fields."
                )

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
                if policy_id == "docs_only":
                    opt1 = (
                        f"1) attest_doc_review(taskId=\"{task_id}\", "
                        f"files=[{{path: \"specs/...\"}}]) then "
                        f"submit_task(..., attestationIds=[...]).\n"
                    )
                elif policy_id == "ui_browser_e2e":
                    opt1 = (
                        f"1) browse(...) then browse(screenshot) then "
                        f"assert_visual(screenshotPath=..., "
                        f"observed=\"what you SEE in the image\", "
                        f"verdict=\"pass\") then "
                        f"submit_task(taskId=\"{task_id}\", "
                        f"attestationIds=[browse_e2e id, visual_check id]). "
                        f"Need BOTH kinds; verdict=fail does not unlock submit; "
                        f"a PNG path alone is rejected.\n"
                    )
                else:
                    opt1 = (
                        f"1) Run bash/tests as the assignee, then "
                        f"submit_task(taskId=\"{task_id}\", "
                        f"attestationIds=[...]).\n"
                    )
                return ToolResult.err(
                    f"submit_task attestation gate failed ({policy_id}): {err}. "
                    f"taskId={task_id} (use this full id).\n"
                    f"Options:\n"
                    + opt1
                    + (
                        f"2) Coordinator last resort: "
                        f"waive_attestation(taskId=\"{task_id}\", "
                        f"reason=\"<why exempt>\").\n"
                        f"Bare testsPassed is rejected."
                    )
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
    if getattr(params, "commit", None) or getattr(params, "commit_hash", None):
        evidence["commit"] = (
            getattr(params, "commit", None) or getattr(params, "commit_hash", None)
        )
    if getattr(params, "core_interaction_executed", None):
        evidence["core_interaction_executed"] = True
    if getattr(params, "failures_acknowledged", None):
        evidence["failures_acknowledged"] = params.failures_acknowledged
    if getattr(params, "env_snapshot", None):
        evidence["env_snapshot"] = str(params.env_snapshot)[:4000]
    if params.files_changed:
        from hiveweave.services.worktree_review import normalize_files_changed

        evidence["files_changed"] = normalize_files_changed(params.files_changed)
    if params.test_output:
        evidence["test_output"] = params.test_output[:4000]

    # P1-C/N5: code tasks require clean worktree + files_changed proof.
    tag_l = {
        str(t).strip().lower()
        for t in (tags if isinstance(tags, list) else [])
        if t
    }
    skip_delivery_gate = policy_id in ("docs_only", "explore") or bool(
        tag_l & {"docs_only", "docs", "explore", "no-code", "no_code"}
    )
    if not skip_delivery_gate:
        from hiveweave.services.worktree_review import (
            agent_worktree_path,
            effective_delivery,
            normalize_files_changed,
            project_main_workspace,
        )

        main_ws = await project_main_workspace(project_id)
        wt = await agent_worktree_path(agent_id)
        if wt and main_ws:
            delivery = await effective_delivery(main_ws, wt)
            dirty_count = int(delivery.get("dirty_count") or 0)
            if dirty_count > 0:
                return ToolResult.err(
                    "submit_task rejected: worktree has uncommitted changes. "
                    "Call git_worktree_checkpoint first, then submit_task."
                )
            if not evidence.get("files_changed"):
                from hiveweave.services.git_worktree import _git

                ok_diff, diff_out = await _git(
                    ["diff", "--name-only", "main...HEAD"], wt
                )
                if ok_diff and (diff_out or "").strip():
                    evidence["files_changed"] = normalize_files_changed(
                        [
                            ln.strip()
                            for ln in diff_out.splitlines()
                            if ln.strip()
                        ]
                    )
            if not evidence.get("files_changed") and dirty_count > 0:
                return ToolResult.err(
                    "submit_task rejected: no files_changed and worktree is dirty. "
                    "Call git_worktree_checkpoint first."
                )

    # P1-2: submit-time symmetric existence gate (mirrors approve-time
    # missing_claimed check). Catches "submit with no actual deliverable"
    # and ".hiveweave/ invisible files" at submit rather than at approve.
    fc_list = evidence.get("files_changed") or []
    if fc_list and not skip_delivery_gate:
        from pathlib import Path as _PSub

        from hiveweave.services.worktree_review import (
            agent_worktree_path as _awt,
            normalize_evidence_path,
            project_main_workspace as _pmw,
        )

        _sub_ws = await _pmw(project_id)
        _sub_wt = await _awt(agent_id)
        _roots = [r for r in (_sub_wt, _sub_ws) if r]
        if _roots:
            missing_at_submit: list[str] = []
            invisible_at_submit: list[str] = []
            for fc in fc_list[:30]:  # cap to avoid perf issues
                # Do NOT use str.lstrip("./") — strips every leading '.' and
                # turns ".hiveweave/…" into "hiveweave/…" (TEST11/TEST19).
                fc_clean = normalize_evidence_path(fc)
                if not fc_clean:
                    continue
                if ".hiveweave/" in fc_clean:
                    invisible_at_submit.append(fc_clean)
                    continue
                if not any((_PSub(r) / fc_clean).exists() for r in _roots):
                    missing_at_submit.append(fc_clean)
            if missing_at_submit:
                from hiveweave.services.worktree_review import (
                    hint_missing_file_locations as _hint,
                )

                hints = _hint(_roots, missing_at_submit)
                root_note = " | ".join(f"root: {r}" for r in _roots)
                return ToolResult.err(
                    "submit_task rejected: files_changed references paths "
                    "that do not exist on disk: "
                    + ", ".join(missing_at_submit[:8])
                    + ("…" if len(missing_at_submit) > 8 else "")
                    + f". Checked: {root_note}. "
                    + (" ".join(hints) + " " if hints else "")
                    + "Ensure all deliverables are committed in your "
                    "worktree before submitting."
                )
            if invisible_at_submit:
                log.warning(
                    "submit_files_under_hiveweave",
                    task_id=task_id,
                    agent_id=agent_id,
                    paths=invisible_at_submit[:5],
                )
                # Warning only — don't block, but inform the agent
                evidence["_hiveweave_invisible_warning"] = invisible_at_submit[:5]

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

