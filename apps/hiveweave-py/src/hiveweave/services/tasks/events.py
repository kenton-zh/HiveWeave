"""TaskEventService — outbox read API."""
from __future__ import annotations

import time

import structlog

from .db import _ensure_schema, _execute, _query

log = structlog.get_logger(__name__)


class TaskEventService:
    """Read task_events outbox — for relay (step 3) and audit queries.

    Event types: task.created, task.claimed, task.running, task.blocked,
    task.submitted, task.reviewing, task.approved, task.rework, task.closed,
    task.archived, task.verifying
    """

    async def get_undelivered(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        """Fetch undelivered task events (for relay consumption)."""
        try:
            rows = await _query(
                project_id,
                "SELECT id, task_id, event_type, from_status, to_status, "
                "actor_id, payload, created_at "
                "FROM task_events WHERE project_id = ? AND delivered = 0 "
                "ORDER BY created_at ASC LIMIT ?",
                [project_id, limit],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("task_events_query_failed", error=str(e))
            return []

    async def mark_delivered(self, project_id: str, event_ids: list[str]) -> None:
        """Mark events as delivered (idempotent)."""
        if not event_ids:
            return
        now_ms = int(time.time() * 1000)
        placeholders = ",".join("?" * len(event_ids))
        try:
            await _execute(
                project_id,
                f"UPDATE task_events SET delivered = 1, delivered_at = ? "
                f"WHERE id IN ({placeholders})",
                [now_ms, *event_ids],
            )
        except Exception as e:
            log.warning("task_events_mark_delivered_failed", error=str(e))

    async def get_task_history(
        self, project_id: str, task_id: str, limit: int = 50
    ) -> list[dict]:
        """Get event history for a single task (audit trail)."""
        try:
            rows = await _query(
                project_id,
                "SELECT id, event_type, from_status, to_status, actor_id, "
                "payload, created_at, delivered, delivered_at "
                "FROM task_events WHERE task_id = ? "
                "ORDER BY created_at ASC LIMIT ?",
                [task_id, limit],
            )
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("task_events_history_failed", error=str(e))
            return []


