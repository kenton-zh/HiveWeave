"""Obligation Ledger — structured obligations with deadlines and escalation.

TEST16 D2: Replaces pure message-driven coordination ("hope they read inbox")
with platform-enforced obligations. The game tick scans for overdue obligations
and escalates to the org parent automatically.

Obligation types:
- merge: reviewer (or MERGE-capable ancestor) must merge assignee worktree
- review: reviewer must review a submitted task
- verify: QA must verify post-merge evidence

Lifecycle: pending → fulfilled | escalated (→ re-assigned to parent)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db

log = structlog.get_logger(__name__)

# Default deadlines (ms from creation)
MERGE_DEADLINE_MS = 10 * 60 * 1000  # 10 minutes
REVIEW_DEADLINE_MS = 15 * 60 * 1000  # 15 minutes
VERIFY_DEADLINE_MS = 20 * 60 * 1000  # 20 minutes
# Dispatch registers review obligation but does not start the clock
# (TEST18 P0-1). Submit activates by resetting to REVIEW_DEADLINE_MS.
_REVIEW_PARKED_DEADLINE_MS = 365 * 24 * 60 * 60 * 1000  # 1 year

# Escalation: after deadline passes, escalate every N ms
ESCALATION_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes between escalations
MAX_ESCALATIONS = 3  # stop escalating after 3 levels

# Review escalate only when the task is actually awaiting review
_REVIEW_ESCALATABLE_STATUSES = frozenset({"submitted", "reviewing"})


# ── DB helpers (Pattern B: keyed by project_id) ──────────────


async def _conn(project_id: str):
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str, params: list[Any] | None = None):
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(cols, r)) for r in rows]


async def _execute(project_id: str, sql: str, params: list[Any] | None = None):
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


# ── Service ──────────────────────────────────────────────────


async def _normalize_task_id(
    project_id: str, task_id: str | None
) -> str | None:
    """Canonicalize task_id (full UUID or unique prefix) for ledger keys.

    TEST6 evening P1-1: agents often pass 8-char prefixes that get_task
    resolves, but obligations stored full UUIDs — exact-match fulfill
    silently hit 0 rows. Normalize at the ledger boundary so every
    create/fulfill/cancel shares one hygiene standard.
    """
    if not task_id:
        return None
    raw = str(task_id).strip()
    if not raw:
        return None
    try:
        from hiveweave.services.task import TaskService

        resolved = await TaskService().resolve_task_id(project_id, raw)
        if resolved:
            if resolved != raw:
                log.debug(
                    "obligation.task_id_normalized",
                    project_id=project_id,
                    raw=raw,
                    resolved=resolved,
                )
            return resolved
    except Exception as e:
        log.warning(
            "obligation.task_id_normalize_failed",
            project_id=project_id,
            raw=raw,
            error=str(e),
        )
    return raw


class ObligationLedger:
    """Platform-level obligation tracking with deadline enforcement."""

    async def create(
        self,
        project_id: str,
        owner_agent_id: str,
        obligation_type: str,
        task_id: str | None = None,
        context: dict | None = None,
        deadline_ms: int | None = None,
    ) -> str:
        """Create a new obligation. Returns obligation id.

        If a pending obligation of the same type+task already exists for
        this owner, returns the existing id (idempotent).
        """
        now = int(time.time() * 1000)
        if deadline_ms is None:
            deadline_ms = {
                "merge": MERGE_DEADLINE_MS,
                "review": REVIEW_DEADLINE_MS,
                "verify": VERIFY_DEADLINE_MS,
            }.get(obligation_type, REVIEW_DEADLINE_MS)

        task_id = await _normalize_task_id(project_id, task_id)

        # TEST18 P0-1: dispatch only registers; clock starts on submit.
        # Far-future deadline until submit activates/resets it.
        ctx = context or {}
        if obligation_type == "review" and ctx.get("source") == "dispatch":
            deadline_ms = _REVIEW_PARKED_DEADLINE_MS

        # Idempotency:
        # - review: one pending per task (any owner) — avoid dispatch+submit dual owners
        # - other types: same owner + type + task
        if task_id:
            if obligation_type == "review":
                existing = await _query(
                    project_id,
                    "SELECT id, owner_agent_id FROM obligations "
                    "WHERE obligation_type = 'review' AND task_id = ? "
                    "AND status = 'pending' LIMIT 1",
                    [task_id],
                )
                if existing:
                    ob_id = existing[0]["id"]
                    prev_owner = str(existing[0].get("owner_agent_id") or "")
                    # Submit activates the clock: reset deadline + clear escalations.
                    # Also retarget owner when pinned reviewer differs from dispatch.
                    if ctx.get("source") == "submit":
                        new_deadline = now + (
                            REVIEW_DEADLINE_MS
                            if deadline_ms == _REVIEW_PARKED_DEADLINE_MS
                            else deadline_ms
                        )
                        new_owner = (
                            owner_agent_id
                            if owner_agent_id
                            else prev_owner
                        )
                        await _execute(
                            project_id,
                            "UPDATE obligations SET owner_agent_id = ?, "
                            "context_json = ?, deadline = ?, "
                            "escalation_count = 0, escalated_at = NULL, "
                            "escalated_to = NULL WHERE id = ?",
                            [
                                new_owner,
                                json.dumps(ctx),
                                new_deadline,
                                ob_id,
                            ],
                        )
                        if prev_owner and new_owner and prev_owner != str(new_owner):
                            log.info(
                                "obligation.review_owner_retargeted",
                                obligation_id=ob_id,
                                from_owner=prev_owner,
                                to_owner=new_owner,
                                task_id=task_id,
                            )
                        log.info(
                            "obligation.review_deadline_activated",
                            obligation_id=ob_id,
                            task_id=task_id,
                            deadline=new_deadline,
                        )
                    return ob_id
            else:
                existing = await _query(
                    project_id,
                    "SELECT id FROM obligations WHERE owner_agent_id = ? "
                    "AND obligation_type = ? AND task_id = ? AND status = 'pending' "
                    "LIMIT 1",
                    [owner_agent_id, obligation_type, task_id],
                )
                if existing:
                    return existing[0]["id"]

        ob_id = str(uuid.uuid4())
        await _execute(
            project_id,
            "INSERT INTO obligations "
            "(id, project_id, owner_agent_id, obligation_type, task_id, "
            " context_json, status, created_at, deadline, escalation_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0)",
            [
                ob_id, project_id, owner_agent_id, obligation_type,
                task_id, json.dumps(ctx),
                now, now + deadline_ms,
            ],
        )
        log.info(
            "obligation.created",
            project_id=project_id,
            obligation_id=ob_id,
            owner=owner_agent_id,
            type=obligation_type,
            task_id=task_id,
            deadline_ms=deadline_ms,
        )
        return ob_id

    async def fulfill(
        self, project_id: str, task_id: str, obligation_type: str
    ) -> int:
        """Mark all pending obligations of this type+task as fulfilled.

        Returns count of obligations fulfilled.
        """
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        now = int(time.time() * 1000)
        # Match canonical UUID and any legacy prefix rows (audit P1-6)
        id_candidates = [task_id]
        if raw_ref and raw_ref != task_id:
            id_candidates.append(raw_ref)
        placeholders = ",".join("?" * len(id_candidates))
        rows = await _query(
            project_id,
            f"SELECT id FROM obligations WHERE task_id IN ({placeholders}) "
            "AND obligation_type = ? AND status = 'pending'",
            [*id_candidates, obligation_type],
        )
        if not rows:
            log.warning(
                "obligation.fulfill_miss",
                project_id=project_id,
                task_id=task_id,
                raw_ref=raw_ref if raw_ref != task_id else None,
                type=obligation_type,
            )
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        log.info(
            "obligation.fulfilled",
            project_id=project_id,
            task_id=task_id,
            type=obligation_type,
            count=len(ids),
        )

        # TEST16 D1: immediately wake blocked tasks that depend on this task.
        # Don't wait for the 2-min reconcile tick — merge landed, QA can go.
        if obligation_type == "merge" and task_id:
            await self._wake_dependent_tasks(project_id, task_id)

        return len(ids)

    async def fulfill_by_owner(
        self, project_id: str, owner_agent_id: str, obligation_type: str
    ) -> int:
        """Fulfill all pending obligations of a type for a given owner."""
        now = int(time.time() * 1000)
        rows = await _query(
            project_id,
            "SELECT id FROM obligations WHERE owner_agent_id = ? "
            "AND obligation_type = ? AND status = 'pending'",
            [owner_agent_id, obligation_type],
        )
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        return len(ids)

    async def scan_overdue(self, project_id: str) -> list[dict]:
        """Scan for overdue pending obligations. Returns list of overdue rows.

        Called by game tick. For each overdue obligation:
        - If escalation_count < MAX_ESCALATIONS and cooldown passed:
          escalate to org parent, send inbox notification.
        """
        now = int(time.time() * 1000)
        overdue = await _query(
            project_id,
            "SELECT * FROM obligations "
            "WHERE status = 'pending' AND deadline < ?",
            [now],
        )
        if not overdue:
            return []

        escalated = []
        for ob in overdue:
            # Check escalation cooldown
            last_esc = ob.get("escalated_at") or 0
            if now - last_esc < ESCALATION_INTERVAL_MS:
                continue
            if (ob.get("escalation_count") or 0) >= MAX_ESCALATIONS:
                continue

            # TEST18 P0-1: review obligations only escalate when the task is
            # actually awaiting review — never while running/claimed/created.
            task_status: str | None = None
            if ob.get("obligation_type") == "review" and ob.get("task_id"):
                task_status = await self._task_status(
                    project_id, str(ob["task_id"])
                )
                if task_status not in _REVIEW_ESCALATABLE_STATUSES:
                    log.debug(
                        "obligation.review_escalate_skipped",
                        obligation_id=ob["id"],
                        task_id=ob.get("task_id"),
                        task_status=task_status,
                    )
                    continue

            parent_id = await self._find_escalation_target(
                project_id, ob["owner_agent_id"]
            )
            if not parent_id:
                continue

            # Escalate: notify parent, update obligation
            esc_count = (ob.get("escalation_count") or 0) + 1
            await _execute(
                project_id,
                "UPDATE obligations SET escalated_to = ?, escalated_at = ?, "
                "escalation_count = ? WHERE id = ?",
                [parent_id, now, esc_count, ob["id"]],
            )

            # Send inbox notification to the escalation target
            await self._notify_escalation(
                project_id, ob, parent_id, esc_count, task_status=task_status
            )
            escalated.append(ob)
            log.warning(
                "obligation.escalated",
                project_id=project_id,
                obligation_id=ob["id"],
                owner=ob["owner_agent_id"],
                escalated_to=parent_id,
                escalation_count=esc_count,
                type=ob["obligation_type"],
                task_id=ob.get("task_id"),
                task_status=task_status,
            )

        return escalated

    async def get_pending_for_agent(
        self, project_id: str, agent_id: str
    ) -> list[dict]:
        """Get all pending obligations for an agent."""
        return await _query(
            project_id,
            "SELECT * FROM obligations WHERE owner_agent_id = ? "
            "AND status = 'pending' ORDER BY deadline ASC",
            [agent_id],
        )

    async def cancel_for_task(self, project_id: str, task_id: str) -> int:
        """Cancel all pending obligations for a task (e.g., task cancelled)."""
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        rows = await _query(
            project_id,
            "SELECT id FROM obligations WHERE task_id = ? AND status = 'pending'",
            [task_id],
        )
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'cancelled' "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        return len(ids)

    async def reconcile_closed_task(
        self, project_id: str, task_id: str
    ) -> int:
        """Fail-open: closed tasks must not leave pending obligations.

        TEST6 evening P1-1 backstop — prefix-miss or missed fulfill paths
        leave pending rows that keep escalating. Fulfill all pending for
        the task and warn.
        """
        raw_ref = (task_id or "").strip()
        task_id = await _normalize_task_id(project_id, task_id) or raw_ref
        if not task_id:
            return 0
        id_candidates = [task_id]
        if raw_ref and raw_ref != task_id:
            id_candidates.append(raw_ref)
        # Also match legacy 8-char prefix stored as task_id
        if len(task_id) >= 8:
            id_candidates.append(task_id[:8])
        # Dedupe while preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for c in id_candidates:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        placeholders = ",".join("?" * len(uniq))
        rows = await _query(
            project_id,
            f"SELECT id, obligation_type FROM obligations "
            f"WHERE task_id IN ({placeholders}) AND status = 'pending'",
            uniq,
        )
        if not rows:
            return 0
        now = int(time.time() * 1000)
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await _execute(
            project_id,
            f"UPDATE obligations SET status = 'fulfilled', fulfilled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now] + ids,
        )
        types = sorted({str(r.get("obligation_type") or "") for r in rows})
        log.warning(
            "obligation.reconcile_closed_fulfilled",
            project_id=project_id,
            task_id=task_id,
            count=len(ids),
            types=types,
        )
        return len(ids)

    async def audit_missing_review_obligations(
        self, project_id: str, *, limit: int = 40
    ) -> list[str]:
        """Backfill review obligations for open submitted/reviewing tasks.

        TEST6 S11: status≠closed tasks in the review pipe should have a
        pending review obligation. Creates missing ones (fail-open).
        Returns task ids that were backfilled.
        """
        rows = await _query(
            project_id,
            "SELECT id, creator_id, reviewer_id, assignee_id, status "
            "FROM tasks WHERE is_archived = 0 "
            "AND status IN ('submitted', 'reviewing') "
            "ORDER BY updated_at DESC LIMIT ?",
            [max(1, int(limit))],
        )
        fixed: list[str] = []
        for row in rows or []:
            tid = str(row.get("id") or "")
            if not tid:
                continue
            existing = await _query(
                project_id,
                "SELECT id FROM obligations WHERE task_id = ? "
                "AND obligation_type = 'review' AND status = 'pending' "
                "LIMIT 1",
                [tid],
            )
            if existing:
                continue
            owner = (
                row.get("reviewer_id")
                or row.get("creator_id")
                or row.get("assignee_id")
            )
            if not owner:
                continue
            try:
                await self.create(
                    project_id,
                    str(owner),
                    "review",
                    task_id=tid,
                    context={"source": "audit_backfill"},
                )
                fixed.append(tid)
            except Exception as e:
                log.warning(
                    "obligation.audit_backfill_failed",
                    task_id=tid,
                    error=str(e),
                )
        return fixed

    # ── Internal helpers ─────────────────────────────────────

    async def _wake_dependent_tasks(
        self, project_id: str, fulfilled_task_id: str
    ) -> None:
        """TEST16 D1: wake blocked tasks that depend on a fulfilled merge.

        Finds tasks in 'blocked' state with fulfilled_task_id in their
        depends_on list, unblocks them, and triggers their assignee.
        """
        try:
            rows = await _query(
                project_id,
                "SELECT id, assignee_id, depends_on FROM tasks "
                "WHERE status = 'blocked' AND is_archived = 0",
            )
            if not rows:
                return

            from hiveweave.services.task import TaskService

            ts = TaskService()
            woken = 0
            for row in rows:
                deps = row.get("depends_on") or "[]"
                if isinstance(deps, str):
                    try:
                        deps = json.loads(deps)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                if fulfilled_task_id not in deps:
                    continue
                # Check ALL deps are satisfied (not just this one)
                all_met = await self._all_deps_met(project_id, deps)
                if not all_met:
                    continue
                # Unblock and trigger
                # Timeline v4 §4.6: 走 _transition 而非裸 UPDATE ——
                # blocked→running 在 _TRANSITIONS 合法，_transition 顺带
                # 清 blocked_reason/wait_kind/wake_at 并写 task_events。
                tid = row["id"]
                try:
                    await ts._transition(
                        project_id,
                        tid,
                        "running",
                        reason_code="dependency_fulfilled",
                        detail=f"deps fulfilled by {fulfilled_task_id[:8]}",
                    )
                except Exception as e:
                    # 并发漂移可能使该任务状态已变（IllegalTransition 等）：
                    # 单任务失败不得中断整批唤醒（原裸 UPDATE 无此风险）。
                    log.warning(
                        "obligation.merge_dependent_wake_failed",
                        project_id=project_id,
                        task_id=tid[:12],
                        error=str(e),
                    )
                    continue
                assignee = row.get("assignee_id")
                if assignee:
                    try:
                        from hiveweave.agents.trigger import trigger_subordinate

                        await trigger_subordinate(assignee)
                    except Exception:
                        pass
                woken += 1
                log.info(
                    "obligation.merge_dependent_woken",
                    project_id=project_id,
                    task_id=tid,
                    fulfilled_task=fulfilled_task_id,
                    assignee=assignee,
                )
            if woken:
                log.info(
                    "obligation.merge_wake_summary",
                    project_id=project_id,
                    fulfilled_task=fulfilled_task_id,
                    woken=woken,
                )
        except Exception as e:
            log.warning(
                "obligation.wake_dependent_failed",
                project_id=project_id,
                task_id=fulfilled_task_id,
                error=str(e),
            )

    async def _all_deps_met(
        self, project_id: str, deps: list[str]
    ) -> bool:
        """Check if all dependency task IDs are in a completed state."""
        if not deps:
            return True
        placeholders = ",".join("?" * len(deps))
        rows = await _query(
            project_id,
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
            "AND status IN ('approved', 'verifying', 'closed') "
            "AND is_archived = 0",
            deps,
        )
        return len(rows) >= len(deps)

    async def _task_status(
        self, project_id: str, task_id: str
    ) -> str | None:
        """Lookup task status for escalate gating (fail-open → None)."""
        try:
            rows = await _query(
                project_id,
                "SELECT status FROM tasks WHERE id = ? LIMIT 1",
                [task_id],
            )
            if rows:
                return str(rows[0].get("status") or "") or None
            # Prefix fallback for legacy short refs
            if len(task_id) >= 8:
                rows = await _query(
                    project_id,
                    "SELECT status FROM tasks WHERE id LIKE ? LIMIT 1",
                    [task_id[:8] + "%"],
                )
                if rows:
                    return str(rows[0].get("status") or "") or None
        except Exception as e:
            log.warning(
                "obligation.task_status_lookup_failed",
                task_id=task_id,
                error=str(e),
            )
        return None

    async def _find_escalation_target(
        self, project_id: str, owner_agent_id: str
    ) -> str | None:
        """Find the org parent to escalate to."""
        from hiveweave.services.org import OrgService

        org = OrgService()
        agent = await org.get_agent(owner_agent_id)
        if not agent:
            return None
        parent_id = agent.get("parent_id")
        if not parent_id:
            return None
        # Verify parent is active
        parent = await org.get_agent(parent_id)
        if not parent or parent.get("is_archived"):
            return None
        return parent_id

    async def _notify_escalation(
        self,
        project_id: str,
        ob: dict,
        parent_id: str,
        esc_count: int,
        *,
        task_status: str | None = None,
    ) -> None:
        """Send inbox notification about an escalated obligation."""
        from hiveweave.services.inbox import InboxService

        ob_type = ob.get("obligation_type", "unknown")
        task_id = ob.get("task_id", "?")
        owner = ob.get("owner_agent_id", "?")
        status_note = f" status={task_status}" if task_status else ""

        msg = (
            f"[OBLIGATION ESCALATION #{esc_count}] "
            f"Agent {owner[:8]} has an overdue {ob_type} obligation "
            f"(task {task_id[:8] if task_id else '?'}{status_note}). "
            f"Deadline passed. Please intervene: "
        )
        if ob_type == "merge":
            msg += "run git_worktree_merge on the assignee's worktree, or reassign the merge duty."
        elif ob_type == "review":
            # TEST18 P0-1: never claim "submitted" unless status confirms it
            if task_status in _REVIEW_ESCALATABLE_STATUSES:
                msg += (
                    f"review the {task_status} task or reassign the reviewer."
                )
            else:
                msg += (
                    f"task status is {task_status or 'unknown'} — "
                    f"confirm it is awaiting review, or reassign."
                )
        else:
            msg += "ensure the obligation is fulfilled or reassign."

        try:
            inbox = InboxService()
            await inbox.send_message(
                from_agent_id="system",
                to_agent_id=parent_id,
                message=msg,
                message_type="escalation",
                priority="urgent",
                task_id=task_id,
                wake=True,
                idempotency_key=f"ob-esc-{ob['id']}-{esc_count}",
            )
        except Exception as e:
            log.warning(
                "obligation.escalation_notify_failed",
                obligation_id=ob["id"],
                error=str(e),
            )
