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

# Escalation: after deadline passes, escalate every N ms
ESCALATION_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes between escalations
MAX_ESCALATIONS = 3  # stop escalating after 3 levels


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

        # Idempotency: same owner + type + task → reuse existing pending
        if task_id:
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
                task_id, json.dumps(context or {}),
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
        now = int(time.time() * 1000)
        rows = await _query(
            project_id,
            "SELECT id FROM obligations WHERE task_id = ? "
            "AND obligation_type = ? AND status = 'pending'",
            [task_id, obligation_type],
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
            await self._notify_escalation(project_id, ob, parent_id, esc_count)
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
                tid = row["id"]
                await _execute(
                    project_id,
                    "UPDATE tasks SET status = 'running', wait_kind = NULL, "
                    "wake_at = NULL, updated_at = ? WHERE id = ?",
                    [int(time.time() * 1000), tid],
                )
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
        self, project_id: str, ob: dict, parent_id: str, esc_count: int
    ) -> None:
        """Send inbox notification about an escalated obligation."""
        from hiveweave.services.inbox import InboxService

        ob_type = ob.get("obligation_type", "unknown")
        task_id = ob.get("task_id", "?")
        owner = ob.get("owner_agent_id", "?")

        msg = (
            f"[OBLIGATION ESCALATION #{esc_count}] "
            f"Agent {owner[:8]} has an overdue {ob_type} obligation "
            f"(task {task_id[:8] if task_id else '?'}). "
            f"Deadline passed. Please intervene: "
        )
        if ob_type == "merge":
            msg += "run git_worktree_merge on the assignee's worktree, or reassign the merge duty."
        elif ob_type == "review":
            msg += "review the submitted task or reassign the reviewer."
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
