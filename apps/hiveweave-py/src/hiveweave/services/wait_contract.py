"""Wait Contract — persisted waiting_on from commit_turn (P1).

Active waits gate wake policy: waiting_human only wakes on matching events.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db
from hiveweave.services.turn_result import WaitingOnItem

log = structlog.get_logger(__name__)

_migrated: set[str] = set()

# Default wake_on events by waiting kind
DEFAULT_WAKE_ON: dict[str, list[str]] = {
    "user": ["user_message", "task_transition", "timeout"],
    "agent": ["ask_reply", "message_from_ref", "timeout"],
    "task": ["task_transition", "timeout"],
    "timer": ["alarm", "timeout"],
    "external": ["external", "timeout"],
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_waits (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    wake_on TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER,
    obligation_version TEXT,
    phase TEXT,
    note TEXT,
    created_at INTEGER NOT NULL,
    cleared_at INTEGER
)
"""


async def _conn(project_id: str):
    return await project_db.get_project_db_by_project_id(project_id)


async def _ensure_schema(project_id: str) -> None:
    if project_id in _migrated:
        return
    conn = await _conn(project_id)
    if conn is None:
        return
    await conn.execute(CREATE_SQL)
    try:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_waits_agent "
            "ON agent_waits(agent_id, cleared_at)"
        )
    except Exception:
        pass
    await conn.commit()
    _migrated.add(project_id)


def obligation_version(obligations: list[dict]) -> str:
    parts = sorted(
        f"{t.get('id')}:{t.get('status')}" for t in (obligations or [])
    )
    raw = "|".join(parts) or "empty"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row) if not isinstance(row, dict) else row
    wake_raw = d.get("wake_on") or "[]"
    try:
        wake_on = json.loads(wake_raw) if isinstance(wake_raw, str) else list(wake_raw)
    except Exception:
        wake_on = []
    return {
        "id": d["id"],
        "agentId": d["agent_id"],
        "projectId": d["project_id"],
        "kind": d["kind"],
        "ref": d["ref"],
        "wakeOn": wake_on,
        "expiresAt": d.get("expires_at"),
        "obligationVersion": d.get("obligation_version"),
        "phase": d.get("phase"),
        "note": d.get("note"),
        "createdAt": d.get("created_at"),
        "clearedAt": d.get("cleared_at"),
    }


class WaitContractService:
    """CRUD for active agent wait contracts."""

    async def replace_waits(
        self,
        project_id: str,
        agent_id: str,
        waiting_on: list[WaitingOnItem] | list[dict],
        *,
        phase: str,
        obligations: list[dict] | None = None,
        expires_at: int | None = None,
    ) -> list[dict]:
        """Clear previous active waits and insert new ones from waiting_on."""
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []

        now = int(time.time() * 1000)
        await conn.execute(
            "UPDATE agent_waits SET cleared_at = ? "
            "WHERE agent_id = ? AND cleared_at IS NULL",
            [now, agent_id],
        )

        ver = obligation_version(obligations or [])
        created: list[dict] = []
        for item in waiting_on or []:
            if isinstance(item, WaitingOnItem):
                kind = item.kind
                ref = item.ref
                note = item.note
            else:
                kind = str(item.get("kind") or "external")
                ref = str(item.get("ref") or "")
                note = item.get("note")
            if not ref:
                continue
            wake_on = list(DEFAULT_WAKE_ON.get(kind, ["timeout"]))
            # Optional per-item override
            if isinstance(item, dict) and item.get("wake_on"):
                wake_on = list(item["wake_on"])
            wid = str(uuid.uuid4())
            exp = expires_at
            if isinstance(item, dict) and item.get("expires_at") is not None:
                exp = int(item["expires_at"])
            await conn.execute(
                "INSERT INTO agent_waits "
                "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
                "obligation_version, phase, note, created_at, cleared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                [
                    wid,
                    agent_id,
                    project_id,
                    kind,
                    ref,
                    json.dumps(wake_on),
                    exp,
                    ver,
                    phase,
                    note,
                    now,
                ],
            )
            created.append(
                {
                    "id": wid,
                    "agentId": agent_id,
                    "projectId": project_id,
                    "kind": kind,
                    "ref": ref,
                    "wakeOn": wake_on,
                    "expiresAt": exp,
                    "obligationVersion": ver,
                    "phase": phase,
                    "note": note,
                    "createdAt": now,
                    "clearedAt": None,
                }
            )
        await conn.commit()
        log.info(
            "wait_contracts_replaced",
            agent_id=agent_id,
            count=len(created),
            phase=phase,
            obligation_version=ver,
        )
        return created

    async def clear_waits(self, project_id: str, agent_id: str) -> int:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return 0
        now = int(time.time() * 1000)
        cur = await conn.execute(
            "UPDATE agent_waits SET cleared_at = ? "
            "WHERE agent_id = ? AND cleared_at IS NULL",
            [now, agent_id],
        )
        await conn.commit()
        return cur.rowcount or 0

    async def list_active(self, project_id: str, agent_id: str) -> list[dict]:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return []
        cur = await conn.execute(
            "SELECT * FROM agent_waits "
            "WHERE agent_id = ? AND cleared_at IS NULL "
            "ORDER BY created_at DESC",
            [agent_id],
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_dict(r) for r in rows]

    async def clear_expired(self, project_id: str, agent_id: str | None = None) -> int:
        await _ensure_schema(project_id)
        conn = await _conn(project_id)
        if conn is None:
            return 0
        now = int(time.time() * 1000)
        if agent_id:
            cur = await conn.execute(
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE agent_id = ? AND cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [now, agent_id, now],
            )
        else:
            cur = await conn.execute(
                "UPDATE agent_waits SET cleared_at = ? "
                "WHERE cleared_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                [now, now],
            )
        await conn.commit()
        return cur.rowcount or 0


def _ref_matches_sender(
    ref: str,
    *,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    """Match wait ref (花名 / short_id / uuid) against the sender identity."""
    r = (ref or "").strip().lower()
    if not r:
        return False
    candidates = [
        (from_agent_id or "").strip().lower(),
        (from_agent_name or "").strip().lower(),
        (from_short_id or "").strip().lower(),
    ]
    for c in candidates:
        if not c:
            continue
        if c == r:
            return True
        # uuid / short_id prefix
        if len(r) >= 4 and (c.startswith(r) or r.startswith(c)):
            return True
        # flower name contained (rare) / exact short_id like a002
        if r == c.replace(" ", ""):
            return True
    return False


def event_matches_waits(
    waits: list[dict],
    *,
    event: str,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
) -> bool:
    """True if any active wait accepts this wake event.

    ``message_from_ref`` matches wait.ref against sender id **or** 花名/short_id
    (commit_turn usually stores the flower name, inbox uses UUID).
    """
    if not waits:
        return True  # no contract → fall back to disposition policy
    now = int(time.time() * 1000)
    for w in waits:
        exp = w.get("expiresAt") or w.get("expires_at")
        if exp is not None and int(exp) <= now:
            continue
        wake_on = w.get("wakeOn") or w.get("wake_on") or []
        if isinstance(wake_on, str):
            try:
                wake_on = json.loads(wake_on)
            except Exception:
                wake_on = []
        kind = (w.get("kind") or "").lower()
        ref = w.get("ref") or ""

        # kind=agent: any non-timeout event from that agent should wake
        if kind == "agent" and event in (
            "message_from_ref",
            "ask_reply",
            "command",
        ):
            if _ref_matches_sender(
                ref,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            ):
                return True

        if event not in wake_on:
            continue

        if event == "message_from_ref":
            if _ref_matches_sender(
                ref,
                from_agent_id=from_agent_id,
                from_agent_name=from_agent_name,
                from_short_id=from_short_id,
            ):
                return True
            continue
        return True
    return False


def category_to_wake_event(
    category: str,
    *,
    from_agent_id: str | None = None,
) -> str:
    from hiveweave.services.wake_policy import is_user_sender

    if is_user_sender(from_agent_id):
        return "user_message"
    if category == "task_transition":
        return "task_transition"
    if category == "ask":
        return "ask_reply"
    if category == "approval":
        return "task_transition"
    return "message_from_ref"


wait_contract_service = WaitContractService()
