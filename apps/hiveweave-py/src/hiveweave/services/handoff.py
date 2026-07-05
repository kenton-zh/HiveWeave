"""Handoff service — task lifecycle management.

契约 06: 交接
状态机: pending → accepted → completed → approved (终态)
                completed → accepted (reopen, 重置 context_delivered=0)

- create_handoff 去重: 同 from/to/summary 1 分钟内不重复
- mark_delivered 不可逆 (契约 06 RECONCILE: 崩溃时 handoff delivered 但 inbox 未读 → inbox 保留未读重试)
- complete_handoff 只完成 accepted (不 fallback 到 pending — 以 Elixir 为准)

schema.py 的 handoffs 表缺 module_id/expect_report/reported_up/updated_at/context_delivered
列，启动时 ALTER TABLE 补齐（幂等）。
"""

import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ensure_project_db

log = structlog.get_logger(__name__)

# Columns missing from schema.py handoffs table
_MISSING_COLUMNS = [
    ("module_id", "TEXT"),
    ("expect_report", "INTEGER DEFAULT 0"),
    ("reported_up", "INTEGER DEFAULT 0"),
    ("updated_at", "INTEGER"),
    ("context_delivered", "INTEGER DEFAULT 0"),
]
_migrated: set[str] = set()


async def _conn(project_id: str):
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ValueError(f"Workspace not found for project {project_id}")
    return await ensure_project_db(workspace)


async def _query(project_id: str, sql: str, params: list | None = None) -> list:
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id: str, sql: str, params: list | None = None) -> None:
    conn = await _conn(project_id)
    await conn.execute(sql, params or [])
    await conn.commit()


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to handoffs table (idempotent)."""
    if project_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE handoffs ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists
    _migrated.add(project_id)


class HandoffService:
    """Task handoff lifecycle — dispatch to approval with rework support."""

    async def create_handoff(self, project_id: str, from_agent_id: str,
                             to_agent_id: str, summary: str,
                             expect_report: bool = False) -> str:
        """Create a handoff with dedup (同 from/to/summary 1 分钟内不重复)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        expect = 1 if expect_report else 0
        # Dedup: active handoff with same from/to/summary within last 1 minute
        dedup_cutoff = now_ms - 60_000
        existing = await _query(project_id,
            "SELECT id FROM handoffs WHERE from_agent_id = ? AND to_agent_id = ? "
            "AND summary = ? AND status IN ('pending', 'accepted') "
            "AND created_at > ? LIMIT 1",
            [from_agent_id, to_agent_id, summary, dedup_cutoff])
        if existing:
            log.info("handoff_dedup", existing_id=existing[0]["id"],
                     summary=summary[:60])
            return existing[0]["id"]

        handoff_id = str(uuid.uuid4())
        await _execute(project_id,
            "INSERT INTO handoffs (id, from_agent_id, to_agent_id, module_id, summary, "
            "status, expect_report, reported_up, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, 'pending', ?, 0, ?, ?)",
            [handoff_id, from_agent_id, to_agent_id, summary, expect, now_ms, now_ms])
        log.info("handoff_created", from_agent_id=from_agent_id,
                 to_agent_id=to_agent_id, summary=summary[:60])
        return handoff_id

    async def accept_pending_handoffs(self, project_id: str, agent_id: str) -> int:
        """Accept all pending handoffs for an agent (pending → accepted). Returns count."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'accepted', updated_at = ? "
            "WHERE to_agent_id = ? AND status = 'pending'", [now_ms, agent_id])
        await conn.commit()
        count = max(cursor.rowcount, 0)
        await cursor.close()
        return count

    async def complete_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Complete a handoff (accepted → completed). Only accepted can be completed."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'completed', updated_at = ? "
            "WHERE id = ? AND status = 'accepted'", [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_complete", handoff_id=handoff_id, completed=ok)
        return ok

    async def approve_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Approve a handoff (completed → approved, terminal state)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'approved', updated_at = ? "
            "WHERE id = ? AND status = 'completed'", [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_approve", handoff_id=handoff_id, approved=ok)
        return ok

    async def reopen_handoff(self, project_id: str, handoff_id: str) -> bool:
        """Reopen a handoff (completed → accepted, resets context_delivered=0)."""
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        conn = await _conn(project_id)
        cursor = await conn.execute(
            "UPDATE handoffs SET status = 'accepted', context_delivered = 0, "
            "updated_at = ? WHERE id = ? AND status = 'completed'",
            [now_ms, handoff_id])
        await conn.commit()
        ok = cursor.rowcount > 0
        await cursor.close()
        log.info("handoff_reopen", handoff_id=handoff_id, reopened=ok)
        return ok

    async def get_pending_handoffs(self, project_id: str, agent_id: str) -> list[dict]:
        """Get pending handoffs (status=pending AND context_delivered=0)."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'pending' AND context_delivered = 0 "
            "ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    async def get_accepted_handoffs(self, project_id: str, agent_id: str) -> list[dict]:
        """Get accepted handoffs (status=accepted AND context_delivered=0)."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'accepted' AND context_delivered = 0 "
            "ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    async def mark_delivered(self, project_id: str, handoff_ids: list[str]) -> None:
        """Mark handoffs as context_delivered=1 (不可逆 — 契约 06 RECONCILE)."""
        if not handoff_ids:
            return
        await _ensure_schema(project_id)
        now_ms = int(time.time() * 1000)
        placeholders = ", ".join(["?"] * len(handoff_ids))
        await _execute(project_id,
            f"UPDATE handoffs SET context_delivered = 1, updated_at = ? "
            f"WHERE id IN ({placeholders})", [now_ms] + handoff_ids)

    async def get_unreported_accepted_handoffs(self, project_id: str,
                                               agent_id: str) -> list[dict]:
        """Find accepted handoffs with expect_report=1 AND reported_up=0."""
        await _ensure_schema(project_id)
        rows = await _query(project_id,
            "SELECT id, from_agent_id, to_agent_id, module_id, summary, status, "
            "expect_report, reported_up, created_at, updated_at FROM handoffs "
            "WHERE to_agent_id = ? AND status = 'accepted' AND expect_report = 1 "
            "AND reported_up = 0 ORDER BY created_at ASC", [agent_id])
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["expect_report"] = bool(d.get("expect_report"))
        d["reported_up"] = bool(d.get("reported_up"))
        return d
