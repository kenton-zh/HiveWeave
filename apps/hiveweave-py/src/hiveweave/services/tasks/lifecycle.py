"""Start / block / unblock / reconcile / park helpers."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query

log = structlog.get_logger(__name__)


class LifecycleMixin:
    """start/block/unblock/reconcile + implementer lock / park."""

    async def start_task(self, project_id: str, task_id: str) -> None:
        """Start a task (claimed → running).

        If the task is currently ``blocked``, delegates to ``unblock_task`` so
        wait metadata is cleared (TEST11 #5-L1). Callers that used to rely on
        ``blocked → running`` being a legal ``_transition`` must not skip that.

        Slice P0: if ``contract_json`` present, enforce ready gate (upstream
        verified) before transitioning; then set slice_status=in_progress.
        """
        task_id = await self.require_task_id(project_id, task_id)
        rows = await _query(
            project_id, "SELECT status, assignee_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        if rows[0]["status"] == "blocked":
            # blocked must go through unblock_task to clear wait metadata
            await self.unblock_task(project_id, task_id)
            agent_id = rows[0]["assignee_id"]
            await self.emit_task_event(
                project_id,
                task_id,
                "running",
                agent_id=agent_id,
                summary=f"[running] task {task_id[:8]} unblocked via start_task",
            )
            return

        # READY GATE (slice-driven)
        task = await self.get_task(project_id, task_id)
        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                check_ready_gate,
                ensure_slice_status,
                parse_contract,
            )

            async def _lookup_tid(tid: str):
                return await self.get_task(project_id, tid)

            async def _lookup_sid(sid: str):
                return await self.find_task_by_slice_id(project_id, sid)

            ready_err = await check_ready_gate(
                project_id,
                task,
                lookup_by_slice_id=_lookup_sid,
                lookup_by_task_id=_lookup_tid,
            )
            if ready_err:
                raise ValueError(ready_err)
            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "ready")
                # Will flip to in_progress after transition succeeds

        await self._transition(project_id, task_id, "running",
                               actor_id=rows[0]["assignee_id"])
        agent_id = rows[0]["assignee_id"]

        if task and task.get("contract_json"):
            from hiveweave.services.task_contract import (
                ensure_slice_status,
                parse_contract,
            )

            contract = parse_contract(task.get("contract_json"))
            if contract:
                contract = ensure_slice_status(contract, "in_progress")
                await self._persist_contract_json(project_id, task_id, contract)

        # TEST21 M2: lock implementer on first transition to running
        if agent_id:
            await self.lock_implementer_if_needed(
                project_id, task_id, str(agent_id)
            )

        await self.emit_task_event(
            project_id,
            task_id,
            "running",
            agent_id=agent_id,
            summary=f"[running] task {task_id[:8]} started",
        )

    async def block_task(
        self,
        project_id: str,
        task_id: str,
        reason: str,
        *,
        depends_on_task_id: str | None = None,
    ) -> None:
        """Block a task (running → blocked). Sets blocked_reason.

        Prefer typed prefixes in reason: dependency: / timer: / user: / external:
        When ``depends_on_task_id`` is set, merges it into ``depends_on`` so
        ``_wake_dependent_tasks`` can auto-unblock (TEST11 #5-L2).
        """
        task_id = await self.require_task_id(project_id, task_id)
        if depends_on_task_id:
            depends_on_task_id = await self.require_task_id(
                project_id, depends_on_task_id
            )
        await self._transition(project_id, task_id, "blocked")
        now_ms = int(time.time() * 1000)
        reason = (reason or "Blocked by agent").strip()
        wait_kind = self._infer_wait_kind(reason)
        # Best-effort: set wait_kind / clear wake_at when columns exist
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, wait_kind = ?, "
                "wake_at = CASE WHEN ? = 'timer' THEN wake_at ELSE NULL END, "
                "updated_at = ? WHERE id = ?",
                [reason, wait_kind, wait_kind, now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = ?, updated_at = ? WHERE id = ?",
                [reason, now_ms, task_id],
            )
        # Structured dependency ref → merge into depends_on (auto-wake path)
        dep = (depends_on_task_id or "").strip()
        if dep:
            try:
                rows = await _query(
                    project_id,
                    "SELECT depends_on FROM tasks WHERE id = ?",
                    [task_id],
                )
                deps: list = []
                if rows and rows[0]["depends_on"]:
                    raw = rows[0]["depends_on"]
                    try:
                        deps = json.loads(raw) if isinstance(raw, str) else list(raw)
                    except (json.JSONDecodeError, TypeError):
                        deps = []
                if not isinstance(deps, list):
                    deps = []
                if dep not in deps:
                    deps.append(dep)
                    await _execute(
                        project_id,
                        "UPDATE tasks SET depends_on = ?, updated_at = ? WHERE id = ?",
                        [json.dumps(deps), now_ms, task_id],
                    )
            except Exception as e:
                log.warning(
                    "block_task_depends_on_merge_failed",
                    task_id=task_id,
                    error=str(e),
                )

    async def unblock_task(self, project_id: str, task_id: str) -> None:
        """Unblock a task (blocked → running). Clears blocked_reason."""
        task_id = await self.require_task_id(project_id, task_id)
        await self._transition(project_id, task_id, "running")
        now_ms = int(time.time() * 1000)
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = NULL, wait_kind = NULL, "
                "wake_at = NULL, updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            )
        except Exception:
            await _execute(
                project_id,
                "UPDATE tasks SET blocked_reason = NULL, updated_at = ? WHERE id = ?",
                [now_ms, task_id],
            )

    @staticmethod
    def _infer_wait_kind(reason: str) -> str | None:
        r = (reason or "").strip().lower()
        for kind in ("dependency", "timer", "user", "external"):
            if r.startswith(f"{kind}:"):
                return kind
        return None

    async def _wake_dependent_tasks(
        self, project_id: str, completed_task_id: str
    ) -> None:
        """Unblock + notify assignees whose depends_on are all approved/closed."""
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return

        completed = set()
        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        completed.add(completed_task_id)

        for row in rows:
            task = self._row(row)
            tid = task["id"]
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            reason = (task.get("blocked_reason") or "").strip()
            reason_l = reason.lower()
            mentions = completed_task_id in reason or completed_task_id[:8] in reason

            # Auto-unblock only with structured evidence (task id in depends_on
            # or dependency: reason mentioning the completed task id).
            # Agent-name-only weak match removed (TEST11 audit H3) — CEO/HR
            # and zero-assignment agents were false-positive "all done".
            if completed_task_id not in deps and not (
                reason_l.startswith("dependency:") and mentions
            ):
                continue

            # All explicit depends_on must be done (if any)
            if deps and not all(d in completed for d in deps):
                continue

            assignee = task.get("assignee_id")
            try:
                await self.unblock_task(project_id, tid)
            except Exception as e:
                log.warning(
                    "dependent_unblock_failed",
                    task_id=tid,
                    completed=completed_task_id,
                    error=str(e),
                )
                continue

            log.info(
                "dependent_task_unblocked",
                task_id=tid,
                completed=completed_task_id,
                assignee=assignee,
            )
            if not assignee:
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                await InboxService().send_message(
                    "system",
                    assignee,
                    (
                        f"[DEPENDENCY MET] Blocker {completed_task_id[:8]}… is done. "
                        f"Your blocked task '{title}' is unblocked (running). "
                        f"Continue work or submit_task."
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "dependent_wake_failed",
                    task_id=tid,
                    error=str(e),
                )

    async def reconcile_blocked_tasks(self, project_id: str) -> int:
        """Sweep blocked tasks: met deps / expired timers → unblock (TEST11 #8).

        Returns number of tasks unblocked. Idempotent.
        """
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        rows = await _query(
            project_id,
            f"SELECT {self._COLUMNS} FROM tasks "
            "WHERE status = 'blocked' AND is_archived = 0",
            [],
        )
        if not rows:
            return 0

        done_rows = await _query(
            project_id,
            "SELECT id FROM tasks WHERE status IN ('approved','closed') "
            "AND is_archived = 0",
            [],
        )
        completed = {r["id"] for r in done_rows}
        woken = 0
        for row in rows:
            task = self._row(row)
            tid = task["id"]
            wait_kind = (task.get("wait_kind") or "").lower()
            wake_at = task.get("wake_at")
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            if not isinstance(deps, list):
                deps = []

            should_wake = False
            reason_tag = ""
            if wait_kind == "timer" and wake_at is not None:
                try:
                    if int(wake_at) <= now_ms:
                        should_wake = True
                        reason_tag = "timer_expired"
                except (TypeError, ValueError):
                    pass
            if deps and all(d in completed for d in deps):
                should_wake = True
                reason_tag = reason_tag or "depends_on_met"

            if not should_wake:
                continue
            assignee = task.get("assignee_id")
            try:
                await self.unblock_task(project_id, tid)
                woken += 1
            except Exception as e:
                log.warning(
                    "reconcile_blocked_unblock_failed",
                    task_id=tid,
                    error=str(e),
                )
                continue
            log.info(
                "reconcile_blocked_unblocked",
                task_id=tid,
                reason=reason_tag,
                assignee=assignee,
            )
            if not assignee:
                continue
            try:
                from hiveweave.services.inbox import InboxService
                from hiveweave.agents.trigger import trigger_subordinate

                title = (task.get("title") or "")[:80]
                await InboxService().send_message(
                    "system",
                    assignee,
                    (
                        f"[BLOCKED RECONCILED] Task '{title}' ({tid[:8]}) "
                        f"unblocked ({reason_tag}). Continue or submit_task."
                    ),
                    message_type="system",
                    priority="urgent",
                    task_id=tid,
                )
                await trigger_subordinate(assignee)
            except Exception as e:
                log.warning(
                    "reconcile_blocked_notify_failed",
                    task_id=tid,
                    error=str(e),
                )
        return woken

    async def lock_implementer_if_needed(
        self,
        project_id: str,
        task_id: str,
        agent_id: str,
    ) -> None:
        """Pin implementer_id + worktree on first running (TEST21 M2).

        Reassign must not rewrite these — review evidence follows the
        implementer worktree, not the current assignee.
        """
        await _ensure_schema(project_id)
        rows = await _query(
            project_id,
            "SELECT implementer_id FROM tasks WHERE id = ?",
            [task_id],
        )
        if not rows:
            return
        if rows[0]["implementer_id"]:
            return
        wt: str | None = None
        try:
            from hiveweave.services.worktree_review import agent_worktree_path

            wt = await agent_worktree_path(str(agent_id))
        except Exception as e:
            log.debug(
                "lock_implementer_worktree_lookup_failed",
                task_id=task_id,
                error=str(e),
            )
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET implementer_id = ?, implementer_worktree = ?, "
            "updated_at = ? WHERE id = ? AND "
            "(implementer_id IS NULL OR implementer_id = '')",
            [agent_id, wt, now_ms, task_id],
        )
        log.info(
            "implementer_locked",
            task_id=task_id[:12],
            implementer_id=agent_id[:8],
            worktree=(wt or "")[-40:],
        )

    async def set_owner_parked(
        self,
        project_id: str,
        task_ids: list[str],
        *,
        parked: bool,
    ) -> None:
        """Mark/clear owner_parked on tasks (TEST21 M5 stall mute)."""
        if not task_ids:
            return
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        flag = 1 if parked else 0
        for tid in task_ids:
            try:
                await _execute(
                    project_id,
                    "UPDATE tasks SET owner_parked = ?, updated_at = ? "
                    "WHERE id = ?",
                    [flag, now_ms, tid],
                )
            except Exception as e:
                log.warning(
                    "set_owner_parked_failed",
                    task_id=tid[:12],
                    error=str(e),
                )

    async def clear_owner_parked_for_agent(
        self, project_id: str, agent_id: str
    ) -> None:
        """Clear owner_parked on recovery (agent completed a turn)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        try:
            await _execute(
                project_id,
                "UPDATE tasks SET owner_parked = 0, updated_at = ? "
                "WHERE assignee_id = ? AND owner_parked = 1 AND is_archived = 0",
                [now_ms, agent_id],
            )
        except Exception as e:
            log.warning(
                "clear_owner_parked_failed",
                agent_id=agent_id[:8],
                error=str(e),
            )

    async def set_wake_at(
        self, project_id: str, task_id: str, wake_at_ms: int | None
    ) -> None:
        """Set or clear wake_at (real-time ms) for timer waits."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET wake_at = ?, updated_at = ? WHERE id = ?",
            [wake_at_ms, now_ms, task_id],
        )

