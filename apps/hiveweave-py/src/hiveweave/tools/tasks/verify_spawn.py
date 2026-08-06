"""VERIFY spawn / retry helpers.

Split from tools/tasks/verify.py. Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

from hiveweave.services import task as _task_svc
from hiveweave.tools import helpers as _helpers

log = structlog.get_logger(__name__)


def _verify_required_capabilities(parent_policy: str) -> list[str]:
    """Capabilities an independent QA must have for VERIFY (TEST21 M12)."""
    if parent_policy == "ui_browser_e2e":
        return ["browse", "browser_acceptance"]
    if parent_policy == "docs_only":
        return ["source_read"]
    return ["test_run", "source_read"]


async def _spawn_post_approve_verify_task(
    ts: _task_svc.TaskService,
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
    # its title "VERIFY:" prefix), do NOT spawn another VERIFY.
    # The original engineering task already has its VERIFY; allowing a
    # VERIFY-of-VERIFY-of-VERIFY… chain wastes architect/CEO review
    # cycles indefinitely.
    # TEST19 教训: 只认前缀 —— agent 自由 tag "verify" 不构成 VERIFY。
    parent_title = parent_task.get("title") or ""
    if isinstance(parent_title, str) and parent_title.startswith("VERIFY:"):
        log.info(
            "verify_chain_stopped",
            parent_task_id=parent_id,
            parent_title=parent_title[:80],
            reason="parent is already a VERIFY task",
        )
        return None
    # ────────────────────────────────────────────────────────────────
    # Avoid spawning duplicate VERIFY children for the same parent.
    # B1 fix: 包含已归档任务 —— 否则已归档的 VERIFY 对去重不可见，
    # 同一父任务会重复 spawn 新 VERIFY。已归档 VERIFY 如果 status
    # 仍非 closed/approved，应阻止重复 spawn（它被取消了不代表可以无限重建）。
    existing = await ts.list_tasks(project_id, include_archived=True)
    for t in existing:
        # TEST19 教训: 只认系统 VERIFY: 前缀（agent 自由 tag verify 不算）
        if (
            t.get("parent_task_id") == parent_id
            and isinstance(t.get("title") or "", str)
            and (t.get("title") or "").startswith("VERIFY:")
            and t.get("status") not in ("closed", "approved")
        ):
            try:
                await ts.mark_verifying(project_id, parent_id)
            except Exception:
                pass
            return t.get("id")

    title = parent_task.get("title") or "task"
    original_assignee = parent_task.get("assignee_id")
    from hiveweave.services.attestation import resolve_task_policy

    parent_tags = parent_task.get("tags") or []
    parent_policy = parent_task.get("policy_id") or resolve_task_policy(
        title=parent_task.get("title") or "",
        tags=parent_tags if isinstance(parent_tags, list) else [],
        description=parent_task.get("description") or "",
    )
    required_caps = _verify_required_capabilities(parent_policy)
    qa_assignee = await _find_independent_qa(
        project_id,
        original_assignee=original_assignee,
        # 合并人（通常=中层 builder）也不得自验 VERIFY
        exclude_ids={str(reviewer_id)} if reviewer_id else None,
        required_capabilities=required_caps,
    )
    caps_label = ", ".join(required_caps)
    blocked_note = ""
    if not qa_assignee:
        blocked_note = (
            f" No QA with capabilities [{caps_label}]; "
            f"hire QA with {required_caps[0]} before VERIFY can run."
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

    # Machine facts for QA — never claim "Work is already on MAIN" without proof
    merge_sha = ""
    branch_contains = "unknown"
    try:
        pev = parent_task.get("evidence") or {}
        if isinstance(pev, str):
            try:
                pev = json.loads(pev)
            except Exception:
                pev = {}
        if isinstance(pev, dict):
            merge_sha = str(
                pev.get("merge_commit")
                or pev.get("merge_commit_hash")
                or pev.get("commit")
                or ""
            ).strip()
        if not merge_sha:
            try:
                from hiveweave.services import task as task_module

                rows = await task_module._query(
                    project_id,
                    "SELECT merge_commit_hash FROM verification_cases "
                    "WHERE original_task_id = ? AND merge_commit_hash IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    [parent_id],
                )
                if rows and rows[0].get("merge_commit_hash"):
                    merge_sha = str(rows[0]["merge_commit_hash"]).strip()
            except Exception:
                pass
        try:
            from hiveweave.services.worktree_review import project_main_workspace
            from hiveweave.services.git_worktree import _git

            main_ws = await project_main_workspace(project_id)
            if main_ws and merge_sha:
                ok_c, contains_out = await _git(
                    ["branch", "--contains", merge_sha],
                    main_ws,
                )
                if ok_c and contains_out:
                    lines = [
                        ln.strip().lstrip("*").strip()
                        for ln in contains_out.splitlines()
                        if ln.strip()
                    ]
                    branch_contains = (
                        "yes" if any(ln == "main" or ln.endswith("/main") for ln in lines)
                        else "no"
                    )
        except Exception:
            branch_contains = "unknown"
    except Exception:
        pass

    platform_facts_block = (
        "[PLATFORM FACTS — do not contradict; re-verify with tools]\n"
        f"- parent_task_id: {parent_id}\n"
        f"- merge_commit: {merge_sha or '(not recorded — confirm on MAIN yourself)'}\n"
        f"- branch_contains_on_main: {branch_contains}\n"
        f"- merged_by: {reviewer_id or '(unknown)'}\n"
        "Your job: run tests on MAIN and report against THESE facts.\n\n"
    )

    verify_tags = ["verify", "mandatory", "post-merge"]
    if parent_policy == "ui_browser_e2e":
        verify_tags.append("ui")
    if parent_policy == "docs_only":
        verify_tags.append("docs_only")

    verify_evidence: dict[str, Any] = {"required_capabilities": required_caps}
    if reviewer_id:
        verify_evidence["merged_by"] = str(reviewer_id)
    # TEST6 evening P1-3: machine-readable verify baseline (not prose-only)
    if merge_sha:
        verify_evidence["target_merge_commit"] = merge_sha
        verify_evidence["merge_commit"] = merge_sha

    verify_id = await ts.create_task(
        project_id,
        title=f"VERIFY: {title}"[:200],
        description=(
            f"Mandatory post-merge verification for parent task {parent_id}.\n"
            f"{platform_facts_block}"
            "1. Confirm PLATFORM FACTS above (do not trust free-text claims that "
            "work is on MAIN — re-check with git log / branch --contains).\n"
            "2. SPECS CONSISTENCY (check first): if docs/ contains spec files, "
            "verify the implementation matches them — dependency list, API "
            "contract, data model. Mismatch = fail (or escalate if specs were "
            "formally updated). 'It runs' is not acceptance.\n"
            "3. On the MAIN workspace only, run the project test suite "
            "(npm test / pytest / etc.). If the project has NO test framework, "
            "write a throwaway verification script (bash + curl / node / python) "
            "driving the code directly: happy path PLUS at least 2 edge/error "
            "cases (invalid input, duplicate submission, inconsistent state). "
            "Attach output, then delete the script.\n"
            "4. If the parent task touched user-visible screens, also run visual "
            "acceptance on main.\n"
            "5. submit_task with attestationIds from those runs.\n"
            "If checks fail, report blockers — do not silently pass.\n"
            f"Original implementer must NOT self-verify (was: {original_assignee})."
        ),
        creator_id=creator_id,
        assignee_id=qa_assignee,
        priority=1,
        acceptance_criteria=[
            {"text": "Implementation matches docs/ specs (deps, API, data model)", "required": True},
            {"text": "Final version on main passes project tests (or throwaway script with edge cases if no framework)", "required": True},
            {"text": "submit_task includes attestationIds", "required": True},
        ],
        parent_task_id=parent_id,
        tags=verify_tags,
        source="system",
        evidence=verify_evidence,
    )

    # Pin reviewer_id = CEO creator at spawn (TEST11 audit H5) so review
    # obligations are visible before QA submit; QA is assignee, not reviewer.
    if verify_id and creator_id:
        try:
            import time as _time

            now_ms = int(_time.time() * 1000)
            from hiveweave.services import task as task_module

            await task_module._execute(
                project_id,
                "UPDATE tasks SET reviewer_id = ?, updated_at = ? WHERE id = ?",
                [creator_id, now_ms, verify_id],
            )
        except Exception as e:
            log.warning(
                "verify_reviewer_pin_failed",
                verify_id=verify_id,
                error=str(e),
            )

    # Create verification case — single authoritative entity linking
    # original_task → verify_task → merger → QA
    if verify_id:
        try:
            from hiveweave.services.task import VerificationCaseService

            vcs = VerificationCaseService()
            await vcs.create_case(
                project_id=project_id,
                original_task_id=parent_id,
                verify_task_id=verify_id,
                merger_agent_id=str(reviewer_id) if reviewer_id else None,
            )
            if qa_assignee:
                await vcs.set_reviewer(project_id, verify_id, qa_assignee)
            # Persist merge HEAD when available (main after merge)
            try:
                from hiveweave.db import meta as meta_db
                from hiveweave.services.git_worktree import _git

                ws = await meta_db.get_project_workspace(project_id)
                if ws:
                    ok, out = await _git(
                        ["rev-parse", "HEAD"], ws
                    )
                    head = (out or "").strip() if ok else ""
                    if head:
                        await vcs.set_merge_commit(
                            project_id, parent_id, head
                        )
                        # Backfill structured baseline when parent evidence
                        # lacked merge_commit at spawn time.
                        if not merge_sha and verify_id:
                            try:
                                from hiveweave.services import task as task_module

                                await task_module._execute(
                                    project_id,
                                    "UPDATE tasks SET evidence = json_set("
                                    "COALESCE(evidence, '{}'), '$.target_merge_commit', ?, "
                                    "'$.merge_commit', ?), updated_at = ? WHERE id = ?",
                                    [
                                        head,
                                        head,
                                        int(time.time() * 1000),
                                        verify_id,
                                    ],
                                )
                            except Exception as bf_err:
                                # json_set may be unavailable — best-effort merge dict
                                log.debug(
                                    "verify_baseline_backfill_json_set_failed",
                                    error=str(bf_err),
                                )
                                try:
                                    row = await ts.get_task(project_id, verify_id)
                                    ev = (row or {}).get("evidence") or {}
                                    if isinstance(ev, str):
                                        ev = json.loads(ev)
                                    if not isinstance(ev, dict):
                                        ev = {}
                                    ev["target_merge_commit"] = head
                                    ev["merge_commit"] = head
                                    await task_module._execute(
                                        project_id,
                                        "UPDATE tasks SET evidence = ?, updated_at = ? "
                                        "WHERE id = ?",
                                        [
                                            json.dumps(ev),
                                            int(time.time() * 1000),
                                            verify_id,
                                        ],
                                    )
                                except Exception as e2:
                                    log.warning(
                                        "verify_baseline_backfill_failed",
                                        verify_id=verify_id,
                                        error=str(e2),
                                    )
            except Exception as e:
                log.warning("verification_case_merge_hash_failed", error=str(e))
        except Exception as e:
            log.warning("verification_case_create_at_spawn_failed", error=str(e))
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
        # Create staffing demand — structured signal for HR
        try:
            from hiveweave.services.staffing import StaffingDemandService

            sds = StaffingDemandService()
            await sds.create_demand(
                project_id=project_id,
                role_needed="qa_engineer",
                reason=f"VERIFY task {verify_id[:8]} blocked — no independent QA",
                task_id=verify_id,
                priority="high",
            )
        except Exception as e:
            log.warning("staffing_demand_create_failed", error=str(e))
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
    required_capabilities: list[str] | None = None,
) -> str | None:
    """Pick independent QA ≠ original implementer / merger.

    Prefer fam=qa over same-parent executors that merely match caps.
    Among QA peers, prefer a *different* parent (independence); same-parent
    is only a last-resort tie-break (TEST18 P0-2).
    """
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

    caps = [str(c) for c in (required_capabilities or []) if c]

    def is_qa(a: dict) -> bool:
        if infer_role_family(a) == "qa":
            return True
        return has_capability(a, Capability.BROWSER_ACCEPTANCE)

    def matches_caps(a: dict) -> bool:
        if caps:
            return all(has_capability(a, Capability(c)) for c in caps)
        return is_qa(a)

    qa_agents = [a for a in active if matches_caps(a)]
    if not qa_agents:
        return None

    # TEST18 P0-2: when caps match both executor and QA, prefer fam=qa.
    # Same-parent is only a tie-break among QA peers — never prefer a
    # same-parent implementer teammate over an independent QA.
    qa_family = [a for a in qa_agents if is_qa(a)]
    pool = qa_family if qa_family else qa_agents

    if original_parent:
        same = [a for a in pool if a.get("parent_id") == original_parent]
        # Prefer SAME parent only when both candidates are fam=qa (tie-break).
        # Prefer DIFFERENT parent when pool still has non-same options among QA —
        # independence > same-team familiarity for VERIFY.
        if qa_family:
            other = [a for a in qa_family if a.get("parent_id") != original_parent]
            if other:
                return other[0]["id"]
            if same:
                return same[0]["id"]
        elif same:
            return same[0]["id"]
    return pool[0]["id"]


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

    ts = _task_svc.TaskService()
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
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = {}
            if isinstance(ev, dict) and ev.get("merged_by"):
                ex.add(str(ev["merged_by"]))
            req_caps = None
            if isinstance(ev, dict):
                req_caps = ev.get("required_capabilities")
            qa = await _find_independent_qa(
                project_id,
                original_assignee=original,
                exclude_ids=ex or None,
                required_capabilities=req_caps if isinstance(req_caps, list) else None,
            )
            if not qa:
                missing = req_caps if isinstance(req_caps, list) else None
                log.info(
                    "verify_retry_no_qa",
                    project_id=project_id,
                    verify_task_id=tid,
                    required_capabilities=missing,
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
            from hiveweave.services.tasks.db import insert_task_event

            try:
                await insert_task_event(
                    project_id,
                    tid,
                    "task.verify_rehang",
                    "blocked",
                    "created",
                    actor_id="system",
                    payload={"reason_code": "verify_rehang"},
                    now_ms=now_ms,
                )
            except Exception as ev_err:
                log.debug("verify_rehang_event_failed", error=str(ev_err))
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
            await _task_svc.TaskService().claim_task(project_id, tid, assignee)
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
        # TEST6 P1-2: time-bucketed idempotency so stale re-nudges can land
        # after VERIFY_STALE_COOLDOWN (content-hash alone permanently deduped).
        bucket = int(time.time() * 1000) // VERIFY_STALE_COOLDOWN_MS
        idem_key = f"verify_stale:{tid}:{bucket}" if reason == "stale" else None
        await inbox.send_message(
            from_agent_id=from_agent_id,
            to_agent_id=assignee,
            message=body,
            message_type="task",
            priority="urgent",
            task_id=tid,
            idempotency_key=idem_key,
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

    ts = _task_svc.TaskService()
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


