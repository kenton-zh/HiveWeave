"""Progress floors and task event emission."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .constants import _PROGRESS_FLOORS

log = structlog.get_logger(__name__)


class ProgressMixin:
    """Progress floor + emit_task_event + update_progress."""

    async def _raise_progress_floor(
        self, project_id: str, task_id: str, floor: int
    ) -> None:
        """Raise progress to at least ``floor`` (never decrease)."""
        if floor <= 0:
            return
        await _ensure_schema(project_id)
        rows = await _query(
            project_id, "SELECT progress FROM tasks WHERE id = ?", [task_id]
        )
        if not rows:
            return
        current = int(rows[0]["progress"] or 0)
        if current >= floor:
            return
        now_ms = int(time.time() * 1000)
        await _execute(
            project_id,
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
            [floor, now_ms, task_id],
        )

    async def emit_task_event(
        self,
        project_id: str,
        task_id: str,
        event: str,
        *,
        agent_id: str | None = None,
        summary: str | None = None,
    ) -> None:
        """System event: progress floor + optional work_log (best-effort).

        ``event`` keys match ``_PROGRESS_FLOORS`` (claimed/running/submitted/…).
        """
        floor = _PROGRESS_FLOORS.get(event, 0)
        try:
            if floor:
                await self._raise_progress_floor(project_id, task_id, floor)
        except Exception as e:
            log.warning(
                "emit_task_event_progress_failed",
                task_id=task_id,
                event=event,
                error=str(e),
            )
        if not agent_id:
            return
        try:
            from hiveweave.services.work_log import WorkLogService

            await WorkLogService().append_log(
                project_id,
                agent_id,
                log_type="task_event",
                summary=summary
                or f"[{event}] task {task_id[:8]}",
                details={"task_id": task_id, "event": event},
            )
        except Exception as e:
            log.warning(
                "emit_task_event_worklog_failed",
                task_id=task_id,
                event=event,
                error=str(e),
            )

    async def update_progress(self, project_id: str, task_id: str,
                              progress: int) -> None:
        """Update progress (0-100). Never decreases below current value.

        Lifecycle floors (claim/start/submit/…) set a lower bound; LLM
        ``update_progress`` may only raise further.
        """
        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be 0-100, got {progress}")
        # P0-2: resolve short prefix / dashed id before the SELECT+UPDATE.
        # Without this, an 8-char prefix matches 0 rows → current=0 fallback →
        # UPDATE matches 0 rows silently → tool reports "progress set" but the
        # ledger never moved. require_task_id raises ValueError on unknown/
        # ambiguous refs (tool layer converts to an error receipt).
        task_id = await self.require_task_id(project_id, task_id)
        await _ensure_schema(project_id)
        rows = await _query(
            project_id, "SELECT progress FROM tasks WHERE id = ?", [task_id]
        )
        current = int(rows[0]["progress"] or 0) if rows else 0
        new_val = max(current, progress)
        if new_val == current:
            return
        now_ms = int(time.time() * 1000)
        await _execute(project_id,
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
            [new_val, now_ms, task_id])

