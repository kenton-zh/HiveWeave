"""Event audit — lightweight event audit log.

契约 16: 可观测性
- Writes events to agent_events table in per-project DB
- log() is fire-and-forget (asyncio.create_task, returns immediately)
- timeline() returns recent events (default 1 hour, LIMIT 100, DESC)
- payload decoded as dict in timeline results (friendlier than Elixir string)
- Event types: stream_start, stream_chunk, stream_done, stream_fail,
  chat_start, chat_done, crash, circuit_open, circuit_close
"""

import asyncio
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db

logger = structlog.get_logger()


class EventAudit:
    """Lightweight event audit log backed by per-project DB.

    The agent_events table is created by ProjectFactory (schema.py),
    not by this service.
    """

    async def log(
        self,
        agent_id: str,
        project_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Log an event asynchronously (fire-and-forget).

        Returns immediately; the DB write runs in a background task.
        project_id is accepted for API compatibility but routing uses agent_id.
        """
        asyncio.create_task(
            self._write(agent_id, str(event_type), payload or {})
        )

    async def timeline(
        self, agent_id: str, hours: int = 1, limit: int = 100
    ) -> list[dict]:
        """Get timeline of events for an agent.

        Default: last 1 hour, max 100 rows, ordered by created_at DESC.
        Returns empty list on error.
        """
        since = int(time.time() * 1000) - hours * 3_600_000
        try:
            rows = await project_db.query(
                agent_id,
                """SELECT id, agent_id, event_type, payload, created_at
                   FROM agent_events
                   WHERE agent_id = ? AND created_at > ?
                   ORDER BY created_at DESC LIMIT ?""",
                [agent_id, since, limit],
            )
            result = []
            for r in rows:
                d = dict(r)
                # Decode payload JSON for friendlier Python access
                if d.get("payload"):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(d)
            return result
        except Exception as e:
            logger.warning("event_audit.timeline_failed",
                           agent_id=agent_id, error=str(e))
            return []

    async def _write(
        self, agent_id: str, event_type: str, payload: dict
    ) -> None:
        """Internal: write event to per-project DB (async, error-safe)."""
        event_id = str(uuid.uuid4())
        created_at = int(time.time() * 1000)
        try:
            payload_json = json.dumps(payload)
        except (TypeError, ValueError):
            payload_json = json.dumps(
                {"error": "payload not serializable"}
            )
        try:
            await project_db.execute(
                agent_id,
                """INSERT INTO agent_events
                   (id, agent_id, event_type, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [event_id, agent_id, event_type, payload_json, created_at],
            )
        except Exception as e:
            logger.warning("event_audit.write_failed",
                           agent_id=agent_id, error=str(e))


event_audit = EventAudit()
