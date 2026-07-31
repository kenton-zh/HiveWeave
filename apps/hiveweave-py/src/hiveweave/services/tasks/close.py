"""Close, merge gate, archive, wait-contract clear."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .errors import MergeRequiredError
from .verify import VerificationCaseService  # noqa: F401

log = structlog.get_logger(__name__)


class CloseMixin:
    """close / merge gate / archive / umbrella / wait contracts."""

    async def close_task(
        self,
        project_id: str,
        task_id: str,
        *,
        skip_merge_gate: bool = False,
        reason_code: str | None = None,
    ) -> None:
        """Close a task (approved|verifying → closed). Sets closed_at.

        Non-VERIFY code tasks must pass the merge/delivery hard gate
        (``_enforce_merge_on_close``). Detection alone used to stamp and
        still close — that fail-open path is gone (TEST20 P0-A / N1).

        ``skip_merge_gate`` is for ledger hygiene migrations only
        (e.g. ``migrate_orphan_approved``).
        """
        task_id = await self.require_task_id(project_id, task_id)

        task = await self.get_task(project_id, task_id)
        if task and not self._is_verify_task(task) and not skip_merge_gate:
            await self._enforce_merge_on_close(project_id, task)

        await self._transition(
            project_id, task_id, "closed", reason_code=reason_code
        )
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET closed_at = ?, updated_at = ? WHERE id = ?",
            [now_ms, now_ms, task_id])
        await self.emit_task_event(
            project_id,
            task_id,
            "closed",
            summary=f"[closed] task {task_id[:8]}",
        )
        await self._wake_dependent_tasks(project_id, task_id)
        try:
            await self._maybe_close_umbrella_parent(project_id, task_id)
        except Exception as e:
            log.warning(
                "umbrella_parent_close_failed",
                task_id=task_id,
                error=str(e),
            )
        try:
            await self._gc_assignee_worktree_if_idle(project_id, task)
        except Exception as e:
            log.warning(
                "worktree_gc_on_close_failed",
                task_id=task_id,
                error=str(e),
            )
        # TEST6 evening P1-1: closed ⇒ no pending obligations (fail-open)
        try:
            from hiveweave.services.obligation import ObligationLedger

            await ObligationLedger().reconcile_closed_task(project_id, task_id)
        except Exception as e:
            log.warning(
                "obligation.reconcile_closed_failed",
                task_id=task_id,
                error=str(e),
            )

    async def _gc_assignee_worktree_if_idle(
        self, project_id: str, task: dict | None
    ) -> None:
        """TEST6 P2: after close, reclaim the assignee's write worktree when
        they have no in-flight tasks left.

        Merge-time cleanup skips teardown while the assignee has open tasks
        — but nothing re-triggered it afterwards, so TEST6 ended with 5
        worktrees + branches still checked out after 8/8 tasks closed.
        Safety is inherited from ``delete()``: the branch is disposed with
        ``git branch -d`` and unmerged branches are preserved + reported
        (never force-deleted), so evidence-only branches survive.
        """
        if not task:
            return
        assignee_id = task.get("assignee_id")
        if not assignee_id:
            return
        from hiveweave.db import meta as meta_db

        ws = await meta_db.get_project_workspace(project_id)
        if not ws:
            return
        rows = await _query(
            project_id,
            "SELECT id, short_id, status, role, permission_type "
            "FROM agents WHERE id = ?",
            [str(assignee_id)],
        )
        if not rows:
            return
        agent = dict(rows[0])
        if (agent.get("status") or "").lower() != "active":
            return  # dismiss path already owns teardown
        from hiveweave.services.git_worktree.ensure import (
            agent_gets_write_worktree,
        )

        if not agent_gets_write_worktree(agent):
            return
        short_id = (agent.get("short_id") or "").strip()
        if not short_id:
            return
        from hiveweave.services.git_worktree.reconcile import (
            _assignee_has_open_tasks,
        )

        if await _assignee_has_open_tasks(ws, short_id):
            return
        from hiveweave.services.git_worktree import GitWorktreeService

        res = await GitWorktreeService().delete(ws, short_id)
        log.info(
            "worktree_gc_on_close",
            task_id=task.get("id"),
            short_id=short_id,
            branch=res.get("branch"),
            preserved_branch=res.get("preserved_branch"),
        )

    def _task_skips_merge_gate(self, task: dict) -> bool:
        """docs/explore / explicit no-code / already verifying after merge."""
        status = (task.get("status") or "").lower()
        # verifying ⇒ merge already landed and VERIFY was spawned
        if status == "verifying":
            return True
        tags = task.get("tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        tag_l = {str(t).lower() for t in tags} if isinstance(tags, list) else set()
        if tag_l & {"docs_only", "docs", "explore", "no-code", "no_code"}:
            return True
        policy = (task.get("policy_id") or "").lower()
        if policy in ("docs_only", "explore"):
            return True
        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if isinstance(ev, dict):
            from hiveweave.services.worktree_review import (
                evidence_has_merge_fact,
                evidence_merge_waived,
            )

            if evidence_merge_waived(ev) or evidence_has_merge_fact(ev):
                return True
            for key in (
                "no_code_change",
                "noCodeChange",
                "verification_only",
                "verificationOnly",
            ):
                if ev.get(key) is True:
                    return True
        return False

    async def _enforce_merge_on_close(
        self, project_id: str, task: dict
    ) -> None:
        """Hard gate: refuse close when worktree still has effective output.

        On block: restore approved(95), rebuild MERGE obligation, wake
        merge_proxy. Explicit ``waive_merge`` / merge facts / verifying
        status / docs-only skip the gate.
        """
        from hiveweave.services.worktree_review import (
            agent_worktree_path,
            effective_delivery,
            evidence_has_merge_fact,
            evidence_merge_waived,
            project_main_workspace,
        )

        if self._task_skips_merge_gate(task):
            return

        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if not isinstance(ev, dict):
            ev = {}

        if evidence_merge_waived(ev):
            return

        if evidence_has_merge_fact(ev):
            # P0-1 is-ancestor companion: merge fact exists, but verify the
            # branch tip is actually in main. Catches "new commits added after
            # merge" (like 5510049 stranded on hw/A003/work post-close).
            assignee = task.get("assignee_id")
            tid = str(task.get("id") or "")
            main_ws = await project_main_workspace(project_id)
            if main_ws and assignee:
                try:
                    from hiveweave.services.git_worktree import (
                        GitWorktreeService,
                        _git,
                        _has_git,
                        _resolve_base_branch,
                        _worktree_path,
                    )
                    from hiveweave.services.org import OrgService

                    org = OrgService()
                    agent_rec = await org.resolve_agent(str(assignee))
                    sid = (agent_rec or {}).get("short_id", "")
                    if sid:
                        # Resolve effective path (P0-1 single source)
                        eff_path = await GitWorktreeService._resolve_effective_worktree_path(
                            main_ws, sid
                        )
                        branch = None
                        if _has_git(eff_path):
                            from hiveweave.services.git_worktree import (
                                _current_branch,
                            )
                            branch = await _current_branch(eff_path)
                        if not branch:
                            branch = f"hw/{sid}/work"
                        # Resolve base branch (W2: don't hardcode "main")
                        base_br = await _resolve_base_branch(main_ws) or "main"
                        # Check if branch still exists and is NOT ancestor of base
                        ok_exists, _ = await _git(
                            ["rev-parse", "--verify", branch], main_ws
                        )
                        if ok_exists:
                            ok_anc, _ = await _git(
                                ["merge-base", "--is-ancestor", branch, base_br],
                                main_ws,
                            )
                            if not ok_anc:
                                # Branch has commits not in main — block close
                                log.warning(
                                    "task.close_blocked_tip_not_ancestor",
                                    task_id=tid,
                                    branch=branch,
                                    assignee=assignee,
                                )
                                await self._rollback_close_to_approved(
                                    project_id,
                                    task,
                                    reason="branch_tip_not_in_main",
                                    commits_ahead=None,
                                    dirty_count=0,
                                )
                                raise MergeRequiredError(
                                    f"Cannot close task {tid[:8]}: branch "
                                    f"{branch} has commits not in main "
                                    f"(merge-base --is-ancestor failed). "
                                    f"Merge the branch first.",
                                    reason="branch_tip_not_in_main",
                                    task_id=tid,
                                )
                except MergeRequiredError:
                    raise
                except Exception as anc_err:
                    log.warning(
                        "task.is_ancestor_check_failed",
                        task_id=str(task.get("id") or ""),
                        error=str(anc_err),
                    )
            # P0-1 variant safety net (audit 2026-07-28): merge landed in git
            # but the merge obligation was not cleared (merge via merge_proxy /
            # service-direct / fulfill failed silently). The merge tool's
            # fulfill (misc_tools.py) is the primary path; this is the backstop
            # so a closed task can never leave a pending merge obligation that
            # keeps escalating (CEO had to cancel_task to clear 81b43baa).
            if tid:
                try:
                    from hiveweave.services.obligation import ObligationLedger

                    fulfilled = await ObligationLedger().fulfill(
                        project_id, tid, "merge"
                    )
                    if fulfilled:
                        log.info(
                            "task.close_merge_obligation_cleared",
                            task_id=tid,
                            count=fulfilled,
                            source="close_safety_net",
                        )
                except Exception as ob_err:
                    log.warning(
                        "task.close_merge_obligation_clear_failed",
                        task_id=tid,
                        error=str(ob_err),
                    )
            return

        assignee = task.get("assignee_id")
        tid = str(task.get("id") or "")
        main_ws = await project_main_workspace(project_id)
        wt = await agent_worktree_path(str(assignee)) if assignee else None

        # Worktree already cleaned after a real merge but evidence lacked
        # merge stamp — allow only when status was verifying (handled above)
        # or a verification case records the merge hash.
        if not wt or not main_ws:
            has_case_merge = False
            try:
                rows = await _query(
                    project_id,
                    "SELECT merge_commit_hash, status FROM verification_cases "
                    "WHERE original_task_id = ? ORDER BY created_at DESC LIMIT 1",
                    [tid],
                )
                if rows:
                    case = rows[0]
                    if case.get("merge_commit_hash") or case.get("status") in (
                        "passed",
                        "in_review",
                        "pending",
                    ):
                        # Case exists ⇒ merge path already ran (VERIFY spawned)
                        has_case_merge = True
            except Exception:
                has_case_merge = False
            if has_case_merge:
                return
            # No worktree + no merge fact while still approved = suspicious.
            if (task.get("status") or "").lower() == "approved":
                await self._rollback_close_to_approved(
                    project_id,
                    task,
                    reason="no_worktree_no_merge_fact",
                    commits_ahead=None,
                    dirty_count=0,
                )
                raise MergeRequiredError(
                    f"Cannot close task {tid[:8]}: assignee worktree gone and "
                    f"no merge fact on evidence. Merge first "
                    f"(git_worktree_merge) or waive_merge with audit reason.",
                    reason="no_worktree_no_merge_fact",
                    task_id=tid,
                )
            return

        delivery = await effective_delivery(main_ws, wt)
        ahead = delivery.get("commits_ahead")
        dirty = int(delivery.get("dirty_count") or 0)
        has_output = bool(delivery.get("has_effective_output"))

        if has_output:
            reason = (
                "unmerged_commits"
                if (ahead is not None and int(ahead) > 0)
                else "uncommitted_dirty"
            )
            log.warning(
                "task.close_blocked_unmerged",
                task_id=tid,
                assignee_id=assignee,
                commits_ahead=ahead,
                dirty_count=dirty,
                reason=reason,
            )
            await self._rollback_close_to_approved(
                project_id,
                task,
                reason=reason,
                commits_ahead=ahead if isinstance(ahead, int) else None,
                dirty_count=dirty,
            )
            raise MergeRequiredError(
                f"Cannot close task {tid[:8]}: worktree still has delivery "
                f"(commits_ahead={ahead}, dirty={dirty}). "
                f"git_worktree_checkpoint if dirty, then git_worktree_merge, "
                f"or waive_merge(reason=…) as last resort.",
                reason=reason,
                task_id=tid,
                commits_ahead=ahead if isinstance(ahead, int) else None,
                dirty_count=dirty,
            )

        # Clean + 0 ahead + no merge fact = zero delivery (Rita escape)
        log.warning(
            "task.close_blocked_no_delivery",
            task_id=tid,
            assignee_id=assignee,
        )
        await self._rollback_close_to_approved(
            project_id,
            task,
            reason="no_delivery",
            commits_ahead=0,
            dirty_count=0,
        )
        raise MergeRequiredError(
            f"Cannot close task {tid[:8]}: no effective delivery "
            f"(0 commits ahead, clean worktree, no merge fact). "
            f"Implement + checkpoint + merge, mark no_code_change in "
            f"evidence, or waive_merge with audit reason.",
            reason="no_delivery",
            task_id=tid,
            commits_ahead=0,
            dirty_count=0,
        )

    async def _rollback_close_to_approved(
        self,
        project_id: str,
        task: dict,
        *,
        reason: str,
        commits_ahead: int | None,
        dirty_count: int,
    ) -> None:
        """Stamp evidence + ensure approved status + rebuild MERGE obligation."""
        tid = str(task.get("id") or "")
        if not tid:
            return
        ev = task.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        if not isinstance(ev, dict):
            ev = {}
        ev["close_blocked"] = True
        ev["close_blocked_reason"] = reason
        if commits_ahead is not None:
            ev["unmerged_commits_ahead"] = commits_ahead
            if commits_ahead > 0:
                ev["closed_with_unmerged_branch"] = True  # legacy stamp name
        if dirty_count:
            ev["uncommitted_dirty_count"] = dirty_count
        now_ms = int(time.time() * 1000)
        status = (task.get("status") or "").lower()
        # Keep / restore approved so CREATOR_MUST_MERGE stays actionable
        if status != "approved":
            try:
                await _execute(
                    project_id,
                    "UPDATE tasks SET status = 'approved', progress = MAX(progress, 95), "
                    "evidence = ?, updated_at = ? WHERE id = ?",
                    [json.dumps(ev), now_ms, tid],
                )
            except Exception:
                await _execute(
                    project_id,
                    "UPDATE tasks SET status = 'approved', progress = 95, "
                    "evidence = ?, updated_at = ? WHERE id = ?",
                    [json.dumps(ev), now_ms, tid],
                )
        else:
            await _execute(
                project_id,
                "UPDATE tasks SET evidence = ?, updated_at = ? WHERE id = ?",
                [json.dumps(ev), now_ms, tid],
            )

        # Rebuild merge obligation + proxy wake
        try:
            from hiveweave.services.obligation import ObligationLedger

            creator = task.get("creator_id") or task.get("reviewer_id")
            if creator:
                await ObligationLedger().create(
                    project_id,
                    str(creator),
                    "merge",
                    task_id=tid,
                    context={"reason": reason, "source": "close_blocked"},
                )
        except Exception as e:
            log.warning(
                "close_blocked_merge_obligation_failed",
                task_id=tid,
                error=str(e),
            )
        try:
            from hiveweave.services.merge_proxy import escalate_merge_proxy

            await escalate_merge_proxy(
                project_id, {**task, "id": tid, "status": "approved"},
                reason=f"close_blocked:{reason}",
            )
        except Exception as e:
            log.warning(
                "close_blocked_merge_proxy_failed",
                task_id=tid,
                error=str(e),
            )

    # Back-compat alias (tests / callers may still import the old name)
    async def _stamp_merge_status_on_close(
        self, project_id: str, task: dict
    ) -> None:
        await self._enforce_merge_on_close(project_id, task)

    async def _clear_task_wait_contracts(
        self, project_id: str, task_id: str
    ) -> None:
        """TEST17 fix: clear agent_waits referencing a task on any transition.

        wake_on=["task_transition"] on agent_waits was dead code — no
        production code ever matched it. This method wires it up: when a
        task transitions, any agent waiting on that task (kind='task',
        ref=task_id) gets their wait cleared and is triggered to resume.

        Does NOT touch the trigger.py task_event filter (TEST3 busy-wait
        guard) — this is a targeted wake for explicit waiters only.
        """
        try:
            now_ms = int(time.time() * 1000)
            rows = await _query(
                project_id,
                "SELECT id, agent_id FROM agent_waits "
                "WHERE kind = 'task' AND ref = ? AND cleared_at IS NULL",
                [task_id],
            )
            if not rows:
                return
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            await _execute(
                project_id,
                f"UPDATE agent_waits SET cleared_at = ? "
                f"WHERE id IN ({placeholders})",
                [now_ms] + ids,
            )
            log.info(
                "task_wait_contracts_cleared",
                project_id=project_id,
                task_id=task_id,
                cleared=len(ids),
                agents=[r["agent_id"] for r in rows],
            )
            # Trigger each waiting agent to resume
            for row in rows:
                agent_id = row["agent_id"]
                try:
                    from hiveweave.agents.trigger import trigger_subordinate

                    await trigger_subordinate(agent_id)
                except Exception as e:
                    log.warning(
                        "task_wait_wake_trigger_failed",
                        agent_id=agent_id,
                        task_id=task_id,
                        error=str(e),
                    )
        except Exception as e:
            log.warning(
                "clear_task_wait_contracts_failed",
                project_id=project_id,
                task_id=task_id,
                error=str(e),
            )

    async def _maybe_close_umbrella_parent(
        self, project_id: str, closed_child_id: str
    ) -> None:
        """Archive/close non-VERIFY parent when all sibling children are done.

        Dogfood: Phase 3 BUILD umbrella hung ~80min after children closed.
        VERIFY parents are handled by ``_close_verify_and_parent`` — skip them.
        """
        child = await self.get_task(project_id, closed_child_id)
        if not child:
            return
        if self._is_verify_task(child):
            return
        parent_id = child.get("parent_task_id")
        if not parent_id:
            return
        parent = await self.get_task(project_id, parent_id)
        if not parent or parent.get("is_archived"):
            return
        if self._is_verify_task(parent):
            return
        pst = parent.get("status")
        if pst in ("closed",):
            return
        tasks = await self.list_tasks(project_id)
        siblings = [
            t for t in tasks
            if t.get("parent_task_id") == parent_id
            and not self._is_verify_task(t)
            and not t.get("is_archived")
        ]
        if not siblings:
            return
        if not all(t.get("status") == "closed" for t in siblings):
            return
        # Do not close umbrella while a VERIFY child of the same parent is open
        open_verify = [
            t for t in tasks
            if t.get("parent_task_id") == parent_id
            and self._is_verify_task(t)
            and not t.get("is_archived")
            and t.get("status") not in ("closed",)
        ]
        if open_verify:
            return
        # Children done — archive running umbrella or close if already approved
        if pst in ("approved", "verifying"):
            await self.close_task(project_id, parent_id)
            log.info(
                "umbrella_parent_closed",
                parent_id=parent_id,
                via_child=closed_child_id[:8],
            )
        elif pst in ("running", "claimed", "submitted", "reviewing", "created"):
            await self.archive_task(
                project_id,
                parent_id,
                archived_by="system",
                reason="all child tasks closed — umbrella auto-archived",
            )
            log.info(
                "umbrella_parent_archived",
                parent_id=parent_id,
                via_child=closed_child_id[:8],
            )

    async def archive_task(
        self,
        project_id: str,
        task_id: str,
        *,
        archived_by: str,
        reason: str,
        reason_code: str = "agent_cancel",
    ) -> str:
        """废弃任务（任意非 closed 状态 → archived）。coordinator 纠错通道。

        背景（井字棋实测 #5）：误绑的 task 卡在 claimed，状态机无出口
        （claimed 只能 →running/created），没有废弃路径 → 僵尸任务永远挂着，
        还会一直占据 assignee 的 obligations 导致 exit-gate 误判。

        archive 不走 _TRANSITIONS（它是生命周期外的纠偏操作，不是状态机的一环），
        但必须留审计痕迹：archived_by / archived_reason / archived_at。
        所有查询（list/obligations/stall）已过滤 is_archived=0，立即生效。

        Returns: 任务废弃前的状态。
        """
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("archive_task requires a non-empty reason (audit)")
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT status, is_archived FROM tasks WHERE id = ?", [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current, is_arch = rows[0]["status"], rows[0]["is_archived"]
        if is_arch:
            raise ValueError(f"Task {task_id[:8]} is already archived")
        if current == "closed":
            raise ValueError(
                f"Task {task_id[:8]} is already closed; archiving is a no-op. "
                "Closed tasks are the terminal success state."
            )
        now_ms = int(time.time() * 1000)
        event_id = str(uuid.uuid4())
        code = (reason_code or "agent_cancel").strip() or "agent_cancel"
        arch_payload = json.dumps({
            "archived_by": archived_by,
            "reason": reason[:500],
            "reason_code": code,
            "detail": reason[:500],
        })
        await _execute_tx(project_id, [
            # 根因修复：归档时同步置终态 status='cancelled'，避免
            # archived=1 但 status 停留在 verifying/submitted 等非终态
            # 导致数据矛盾（直接查 DB / task_events 审计 / 外部脚本困惑）
            # P2-3: reset progress to 0 — cancelled tasks must not retain
            # stale progress (e.g. 90 from submitted state).
            ("UPDATE tasks SET is_archived = 1, status = 'cancelled', "
            "progress = 0, "
            "archived_by = ?, archived_reason = ?, archived_at = ?, "
            "wake_at = NULL, updated_at = ? WHERE id = ?",
            [archived_by, reason[:500], now_ms, now_ms, task_id]),
            ("INSERT INTO task_events (id, project_id, task_id, event_type, "
             "from_status, to_status, actor_id, payload, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [event_id, project_id, task_id, "task.archived",
              current, "cancelled", archived_by, arch_payload, now_ms]),
        ])
        log.info(
            "task_archived",
            project_id=project_id,
            task_id=task_id,
            from_status=current,
            archived_by=archived_by,
            reason=reason[:120],
            reason_code=code,
        )

        # B2: VERIFY 归档时级联父任务 —— 如果归档的是 VERIFY 子任务，
        # 其父任务可能卡在 verifying 状态无法前进。回退到 approved，
        # 让 CEO/coordinator 可以重新走 merge+VERIFY 流程或直接 close。
        # （archive_task 在 current=="closed" 时已 raise，此处 current 必非 closed）
        archived_task = await self.get_task(project_id, task_id)
        if archived_task and self._is_verify_task(archived_task):
            # TEST13 P1-3: cascade close verification_case
            try:
                await VerificationCaseService().mark_cancelled(
                    project_id,
                    task_id,
                    reason=f"VERIFY archived: {reason[:200]}",
                )
            except Exception as e:
                log.warning(
                    "verify_case_cancel_on_archive_failed",
                    task_id=task_id,
                    error=str(e),
                )
            parent_id = archived_task.get("parent_task_id")
            if parent_id:
                parent_rows = await _query(
                    project_id,
                    "SELECT status FROM tasks WHERE id = ?",
                    [parent_id],
                )
                if parent_rows and parent_rows[0]["status"] == "verifying":
                    try:
                        await self._transition(project_id, parent_id, "approved")
                        log.info(
                            "verify_archived_parent_reverted",
                            project_id=project_id,
                            verify_task_id=task_id,
                            parent_task_id=parent_id,
                            from_status="verifying",
                            to_status="approved",
                        )
                    except Exception as e:
                        log.warning(
                            "verify_archived_parent_revert_failed",
                            parent_task_id=parent_id,
                            error=str(e),
                        )

        # L3: clear wait contracts referencing this task (waiters must wake)
        await self._clear_task_wait_contracts(project_id, task_id)

        # L1: detect reverse dependents — tasks whose depends_on contains
        # this task will have a dangling reference (cancelled ∉ completed).
        # Log warning + cancel obligations so downstream doesn't silently hang.
        try:
            dependents = await _query(
                project_id,
                "SELECT id, title, status FROM tasks "
                "WHERE is_archived = 0 AND status NOT IN ('closed', 'cancelled') "
                "AND depends_on IS NOT NULL AND depends_on != '[]'",
            )
            dangling = []
            for dep in dependents:
                deps_raw = dep.get("depends_on") or "[]"
                try:
                    deps_list = json.loads(deps_raw) if isinstance(deps_raw, str) else deps_raw
                except (json.JSONDecodeError, TypeError):
                    deps_list = []
                if task_id in (deps_list or []):
                    dangling.append(dep)
            if dangling:
                log.warning(
                    "task_archived_dangling_dependents",
                    project_id=project_id,
                    task_id=task_id,
                    dependents=[d["id"] for d in dangling],
                )
        except Exception as e:
            log.warning("archive_reverse_dep_check_failed", error=str(e))

        # Cancel pending obligations for this task
        try:
            from hiveweave.services.obligation import ObligationLedger

            await ObligationLedger().cancel_for_task(project_id, task_id)
        except Exception:
            pass

        return current

