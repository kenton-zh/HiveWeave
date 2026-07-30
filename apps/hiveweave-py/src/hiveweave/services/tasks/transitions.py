"""Task status transition helpers."""
from __future__ import annotations

import json
import time
import uuid

import structlog

from .db import _conn, _ensure_schema, _execute, _execute_tx, _query
from .constants import _TRANSITIONS

log = structlog.get_logger(__name__)


class TransitionsMixin:
    """_transition / _transition_multi."""

    async def _transition(self, project_id: str, task_id: str, target: str,
                          *, actor_id: str | None = None,
                          reason_code: str | None = None,
                          detail: str | None = None) -> None:
        """Validate and execute a status transition.

        Raises ValueError if the task is not found or the transition is illegal.
        Writes a task_events row in the same transaction (Transactional Outbox).

        TEST21 M9: system migrations pass ``reason_code`` + ``detail`` into
        task_events.payload.

        Leaving ``blocked`` clears wait metadata (blocked_reason / wait_kind /
        wake_at) in the same transaction — state-machine invariant: not blocked
        means not waiting. Fixes residual wait_kind on running tasks when
        ``start_task`` is used instead of ``unblock_task`` (TEST11 #5-L1).
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        if target not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"Illegal transition: {current} → {target}")
        now_ms = int(time.time() * 1000)
        event_id = str(uuid.uuid4())
        payload_obj: dict = {}
        if reason_code:
            payload_obj["reason_code"] = str(reason_code)[:80]
        if detail:
            payload_obj["detail"] = str(detail)[:500]
        payload = json.dumps(payload_obj) if payload_obj else "{}"
        if current == "blocked":
            # Defensive: any exit from blocked clears wait metadata
            try:
                await _execute_tx(project_id, [
                    ("UPDATE tasks SET status = ?, blocked_reason = NULL, "
                     "wait_kind = NULL, wake_at = NULL, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                     "from_status, to_status, actor_id, payload, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [event_id, project_id, task_id, f"task.{target}",
                      current, target, actor_id, payload, now_ms]),
                ])
            except Exception as e:
                # Prefer status transition over abort; then best-effort clear
                log.warning(
                    "blocked_exit_clear_metadata_failed",
                    task_id=task_id,
                    error=str(e),
                )
                await _execute_tx(project_id, [
                    ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                     [target, now_ms, task_id]),
                    ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                     "from_status, to_status, actor_id, payload, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [event_id, project_id, task_id, f"task.{target}",
                      current, target, actor_id, payload, now_ms]),
                ])
                try:
                    await _execute(
                        project_id,
                        "UPDATE tasks SET blocked_reason = NULL, wait_kind = NULL, "
                        "wake_at = NULL, updated_at = ? WHERE id = ?",
                        [now_ms, task_id],
                    )
                except Exception as e2:
                    log.warning(
                        "blocked_exit_metadata_retry_failed",
                        task_id=task_id,
                        error=str(e2),
                    )
        else:
            await _execute_tx(project_id, [
                ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                 [target, now_ms, task_id]),
                ("INSERT INTO task_events (id, project_id, task_id, event_type, "
                 "from_status, to_status, actor_id, payload, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [event_id, project_id, task_id, f"task.{target}",
                  current, target, actor_id, payload, now_ms]),
            ])
        log.info("task_transition", task_id=task_id,
                 from_status=current, to_status=target,
                 reason_code=reason_code)

        # TEST17 fix: clear agent_waits referencing this task and wake waiters.
        # wake_on=["task_transition"] was dead code — no production caller ever
        # matched it. Wire it up here: any transition clears matching waits.
        await self._clear_task_wait_contracts(project_id, task_id)

    async def _transition_multi(self, project_id: str, task_id: str,
                               *targets: str,
                               actor_id: str | None = None,
                               reason_code: str | None = None,
                               detail: str | None = None) -> None:
        """Validate and execute a multi-step transition atomically.

        Validates each step against _TRANSITIONS, then performs a single
        UPDATE to the final state — no intermediate state is ever visible
        to concurrent readers. Writes a task_events row in the same tx.

        Example: _transition_multi(pid, tid, "rework", "running")
        validates reviewing → rework → running, then UPDATEs directly
        to "running" in one statement.
        """
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT status FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise ValueError(f"Task not found: {task_id}")
        current = rows[0]["status"]
        # Validate each step
        state = current
        for target in targets:
            if target not in _TRANSITIONS.get(state, set()):
                raise ValueError(f"Illegal transition: {state} → {target}")
            state = target
        # Single UPDATE to final state — atomic, no intermediate visible
        now_ms = int(time.time() * 1000)
        final = targets[-1]
        event_id = str(uuid.uuid4())
        payload_obj: dict = {}
        if reason_code:
            payload_obj["reason_code"] = str(reason_code)[:80]
        if detail:
            payload_obj["detail"] = str(detail)[:500]
        payload = json.dumps(payload_obj) if payload_obj else "{}"
        await _execute_tx(project_id, [
            ("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
             [final, now_ms, task_id]),
            ("INSERT INTO task_events (id, project_id, task_id, event_type, "
             "from_status, to_status, actor_id, payload, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [event_id, project_id, task_id, f"task.{final}",
              current, final, actor_id, payload, now_ms]),
        ])
        log.info("task_transition_multi", task_id=task_id,
                 from_status=current, through=list(targets[:-1]),
                 to_status=final, reason_code=reason_code)

        # L2 fix: clear wait contracts on multi-step transitions too
        # (rework path uses _transition_multi, waiters need to be woken)
        await self._clear_task_wait_contracts(project_id, task_id)

