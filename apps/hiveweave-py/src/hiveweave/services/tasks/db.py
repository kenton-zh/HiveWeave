"""Task ledger DB helpers."""
from __future__ import annotations

import aiosqlite

from hiveweave.db import meta as meta_db
from hiveweave.db.project import ProjectDbError, ensure_project_db

from .constants import _MISSING_COLUMNS

_migrated: set[str] = set()

async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection."""
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
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


async def _execute_tx(
    project_id: str, statements: list[tuple[str, list]]
) -> None:
    """Execute multiple SQL statements in a single transaction.

    Used by the Transactional Outbox: the state transition and the event
    record are written atomically — either both commit or neither does.
    """
    conn = await _conn(project_id)
    try:
        for sql, params in statements:
            await conn.execute(sql, params)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to tasks table (idempotent)."""
    if project_id in _migrated:
        return
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists
    _migrated.add(project_id)

