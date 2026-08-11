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
    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description=(
            "Preflight (dry-run): when true, run ONLY the precondition checks "
            "(attestation ids / core interaction / failures acknowledged / "
            "delivery gate / files_changed existence) and return the complete "
            "missing-items list. NO mutations: no submit, no claim/start, "
            "no notifications. Default false = real submit."
        ),
        json_schema_extra={"aliases": ["dryRun", "dry_run", "preflight", "check"]},
    )

    @field_validator("files_changed", mode="before")
    @classmethod
    def _coerce_files_changed(cls, v: Any) -> Any:
        return _coerce_to_list(v)

    @field_validator("attestation_ids", mode="before")
    @classmethod
    def _coerce_attestation_ids(cls, v: Any) -> Any:
        return _coerce_to_list(v)


async def _submit_preflight(
    project_id: str,
    agent_id: str,
    task_id: str,
    task: dict,
    params: SubmitTaskParams,
) -> dict:
    """只读 submit 预检 — 收集全部前置缺失项，不写库/不发通知。

    镜像 submit_task_tool 的检查链（同序同文案）：UI core interaction /
    failures acknowledged / attestation 门 / docs tests_passed / 交付门
    （worktree 脏）/ files_changed 存在性。真实路径复用返回的中间量
    （attest_ids/policy_id/skip_delivery_gate/evidence），保证一次检查、
    行为一致。
    """
    from hiveweave.services.attestation import (
        attestation_service,
        required_attestation_kinds,
        resolve_task_policy,
    )

    ts = _task_svc.TaskService()
    issues: list[dict] = []

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

    # 审计结论：agent 把 code_audit 凭证塞进 attestationIds 时，strict 策略
    # 会以 "kind not in expected" 硬拒 —— 审计凭证不是交付证据，先剔除，
    # 其它 kind 行为不变（查不到 kind 时 fail-open 保持原样）。
    if attest_ids:
        try:
            try:
                from hiveweave.services.code_audit import CODE_AUDIT_KIND
            except Exception:  # noqa: BLE001
                CODE_AUDIT_KIND = "code_audit"
            kept = []
            for _aid in attest_ids:
                _row = await attestation_service.get(project_id, str(_aid))
                if _row and str(_row.get("kind") or "") == CODE_AUDIT_KIND:
                    continue
                kept.append(_aid)
            if len(kept) != len(attest_ids):
                log.info(
                    "submit_filtered_code_audit_attestations",
                    agent_id=agent_id,
                    task_id=task_id,
                    removed=len(attest_ids) - len(kept),
                )
            attest_ids = kept
        except Exception as _fe:  # noqa: BLE001
            log.debug("submit_code_audit_filter_failed", error=str(_fe))

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
            issues.append({
                "code": "ui_core_interaction",
                "message": (
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
                ),
            })
        elif core_att not in attest_ids:
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
                issues.append({
                    "code": "failures_acknowledged",
                    "message": (
                        f"VERIFY submit rejected: testOutput reports {fail_n} "
                        f"failed test(s); need failuresAcknowledged with at least "
                        f"{required} {{test, reason}} entries "
                        f"(got {len(acks) if isinstance(acks, list) else 0}). "
                        f"Either rework until green, or acknowledge structurally "
                        f"(free-text excuses alone are not accepted)."
                    ),
                })
            else:
                bad = [
                    a for a in acks
                    if not isinstance(a, dict)
                    or not str(a.get("test") or a.get("name") or "").strip()
                    or not str(a.get("reason") or a.get("why") or "").strip()
                ]
                if bad:
                    issues.append({
                        "code": "failures_acknowledged_invalid",
                        "message": (
                            "VERIFY submit rejected: each failuresAcknowledged "
                            "entry must be {test, reason} with non-empty fields."
                        ),
                    })

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
                full_tid = task.get("id") or task_id
                issues.append({
                    "code": "attestation",
                    "message": (
                        f"submit_task attestation gate failed ({policy_id}): {err}. "
                        f"taskId={full_tid} (use this full id).\n"
                        f"Options:\n"
                        + opt1
                        + (
                            f"2) Coordinator last resort: "
                            f"waive_attestation(taskId=\"{task_id}\", "
                            f"reason=\"<why exempt>\").\n"
                            f"Bare testsPassed is rejected."
                        )
                    ),
                })
    elif params.tests_passed is not True:
        # docs_only still asks for explicit ack
        issues.append({
            "code": "docs_tests_passed",
            "message": (
                "docs_only submit still requires testsPassed=true "
                "(note N/A in summary)."
            ),
        })

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
                issues.append({
                    "code": "worktree_dirty",
                    "message": (
                        "submit_task rejected: worktree has uncommitted changes. "
                        "Call git_worktree_checkpoint first, then submit_task."
                    ),
                })
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
                issues.append({
                    "code": "worktree_dirty_no_files",
                    "message": (
                        "submit_task rejected: no files_changed and worktree is dirty. "
                        "Call git_worktree_checkpoint first."
                    ),
                })

        # BUG-ORGWT 疏通（2026-08-05 feature-test 死锁）：attestation 背书但
        # 无任何代码变更的提交 = verification-only 交付（平台功能测试/纯验证类
        # 任务，交付物是 .hiveweave/reports/ 下的报告 + 真实 test_run 凭证，
        # 按规则 .hiveweave/ 文件对 files_changed 不可见）。此前这类提交在
        # submit 被放行，到 approve 却被 review_worktree_gate 以
        # "no worktree path / no diverged files" 硬拒，agent 侧无解法
        # （不知道要显式打 no_code_change 旗标）。submit 是最后能一致化
        # evidence 的位置——此处自动补旗标，与上方自动回填 files_changed
        # 对称。安全边界：仅当 attestations 经平台验真（存在、未过期、
        # 归属本 agent/本任务、stdout_hash 齐备）且自动 diff 确实挖不到
        # 文件时触发；approve 侧审查方 fresh test_run 硬闸（P0-2）不受
        # 影响，仍独立生效。
        # 审计 P1（2026-08-05）：软策略（generic_tests/coordinator_review）
        # 下 attestation_ids 是 agent 自述、上方 strict 门不校验——必须先
        # verify_ids 再信，否则伪造 ID + 空交付即可借自动旗标绕过
        # review/close 双侧 merge gate（TEST20 N1 "Rita escape" 复活）。
        if not evidence.get("files_changed"):
            _aids = [str(x) for x in (evidence.get("attestation_ids") or []) if x]
            if _aids and evidence.get("no_code_change") is not True:
                try:
                    _aok, _aerr = await attestation_service.verify_ids(
                        project_id,
                        _aids,
                        expected_agent_id=agent_id,
                        task_id=task_id,
                    )
                except Exception as _ve:
                    _aok, _aerr = False, f"verify_error: {_ve}"
                if _aok:
                    evidence["no_code_change"] = True
                    evidence["_auto_no_code_change"] = "attestation_only_delivery"
                    log.info(
                        "submit_auto_no_code_change",
                        task_id=task_id,
                        agent_id=agent_id,
                        attestations=len(_aids),
                    )
                else:
                    log.warning(
                        "submit_auto_no_code_change_rejected_unverified",
                        task_id=task_id,
                        agent_id=agent_id,
                        attestations=len(_aids),
                        error=_aerr,
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
                issues.append({
                    "code": "files_changed_missing",
                    "message": (
                        "submit_task rejected: files_changed references paths "
                        "that do not exist on disk: "
                        + ", ".join(missing_at_submit[:8])
                        + ("…" if len(missing_at_submit) > 8 else "")
                        + f". Checked: {root_note}. "
                        + (" ".join(hints) + " " if hints else "")
                        + "Ensure all deliverables are committed in your "
                        "worktree before submitting."
                    ),
                })
            if invisible_at_submit:
                log.warning(
                    "submit_files_under_hiveweave",
                    task_id=task_id,
                    agent_id=agent_id,
                    paths=invisible_at_submit[:5],
                )
                # Warning only — don't block, but inform the agent
                evidence["_hiveweave_invisible_warning"] = invisible_at_submit[:5]

    return {
        "ok": not issues,
        "issues": issues,
        "attest_ids": attest_ids,
        "policy_id": policy_id,
        "ui_verify": ui_verify,
        "skip_delivery_gate": skip_delivery_gate,
        "evidence": evidence,
    }


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
    if not getattr(params, "dry_run", False):
        try:
            await ts.lock_implementer_if_needed(project_id, task_id, agent_id)
        except Exception as e:
            log.debug("submit_lock_implementer_failed", error=str(e))

    preflight = await _submit_preflight(
        project_id, agent_id, task_id, task, params
    )

    # ── dry-run：只读预检，列出全部缺失项，零写操作 ──
    if getattr(params, "dry_run", False):
        if preflight["ok"]:
            return ToolResult.ok(
                "submit_task dry-run: 所有前置条件已满足，可以提交。",
                dry_run=True,
                missing=[],
            )
        return ToolResult.ok(
            "submit_task dry-run: 以下前置条件未满足，提交将被拒绝：\n"
            + "\n".join(
                f"- [{i['code']}] {i['message']}" for i in preflight["issues"]
            ),
            dry_run=True,
            missing=preflight["issues"],
        )

    # ── 聚合：一次列出全部缺失前置条件（不再第一个失败就停）──
    if preflight["issues"]:
        first = preflight["issues"][0]
        msg = str(first["message"])
        if len(preflight["issues"]) > 1:
            msg += "\n\n[additional blockers]\n" + "\n".join(
                f"- [{i['code']}] {i['message']}"
                for i in preflight["issues"][1:]
            )
        return ToolResult.err(msg)

    evidence: dict[str, Any] = preflight["evidence"]

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

        # ── code_audit：账本超阈且近期无审计凭证 → 软提醒（不阻断、不改
        #    状态流）；无论是否提醒，真实提交成功即重置账本 ──
        audit_reminder = ""
        try:
            from hiveweave.services.attestation import find_latest_attestation_by_kind
            from hiveweave.services.code_audit import (
                CODE_AUDIT_KIND,
                CODE_AUDIT_LINE_THRESHOLD,
                CODE_AUDIT_REMINDER,
                get_last_change_ts,
                get_unaudited_lines,
                reset_ledger,
            )

            if get_unaudited_lines(agent_id) > CODE_AUDIT_LINE_THRESHOLD:
                latest = await find_latest_attestation_by_kind(
                    project_id, agent_id=agent_id, kind=CODE_AUDIT_KIND
                )
                audited_after_changes = (
                    latest is not None
                    and latest.get("created_at", 0) >= get_last_change_ts(agent_id) * 1000
                )
                if not audited_after_changes:
                    audit_reminder = f"\n{CODE_AUDIT_REMINDER}"
            reset_ledger(agent_id)
        except Exception as e:  # noqa: BLE001
            log.debug("submit_code_audit_reminder_failed", error=str(e))

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

        return ToolResult.ok(f"Task {task_id} submitted for review.{audit_reminder}")
    except Exception as e:
        return ToolResult.err(f"Failed to submit task: {e}")

