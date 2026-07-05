"""Meta DB — global SQLite database for projects, agents, models, settings.

契约 11: Meta DB
- 全局单例，WAL 模式
- 表: projects, agents, agent_templates, llm_models, global_settings, agent_charters, charter_attachments, meta_index
- agents 表在此（RECONCILE 修复：agent 路由依赖 Meta DB 的 agents 表）
"""

import aiosqlite
from pathlib import Path
from typing import Any

from hiveweave.db.schema import META_DB_TABLES, META_DB_INDEXES
from hiveweave.config import settings as app_settings

_db: aiosqlite.Connection | None = None


async def init_meta_db() -> None:
    """Initialize Meta DB — create tables and indexes if not exist."""
    global _db
    db_path = app_settings.get_meta_db_path()

    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row

    # WAL mode for Meta DB (global, concurrent reads)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.execute("PRAGMA foreign_keys=ON")

    # Create tables
    for sql in META_DB_TABLES:
        await _db.execute(sql)

    # Create indexes
    for sql in META_DB_INDEXES:
        await _db.execute(sql)

    await _db.commit()


async def get_meta_db() -> aiosqlite.Connection:
    """Get the Meta DB connection. Initializes if needed."""
    global _db
    if _db is None:
        await init_meta_db()
    assert _db is not None
    return _db


async def close_meta_db() -> None:
    """Close the Meta DB connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def query(sql: str, params: list[Any] | None = None) -> list[aiosqlite.Row]:
    """Execute a SELECT query and return rows."""
    db = await get_meta_db()
    cursor = await db.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def query_one(sql: str, params: list[Any] | None = None) -> aiosqlite.Row | None:
    """Execute a SELECT query and return a single row."""
    db = await get_meta_db()
    cursor = await db.execute(sql, params or [])
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def execute(sql: str, params: list[Any] | None = None) -> None:
    """Execute an INSERT/UPDATE/DELETE query."""
    db = await get_meta_db()
    await db.execute(sql, params or [])
    await db.commit()


async def get_agent_project_id(agent_id: str) -> str | None:
    """Look up which project an agent belongs to.

    契约 11: agent 路由 — agent_id → Meta DB agents 表查 project_id → per-project DB
    """
    row = await query_one(
        "SELECT project_id FROM agents WHERE id = ? LIMIT 1", [agent_id]
    )
    if row is None:
        return None
    return row["project_id"]


async def get_project_workspace(project_id: str) -> str | None:
    """Look up a project's workspace path."""
    row = await query_one(
        "SELECT workspace_path FROM projects WHERE id = ? LIMIT 1", [project_id]
    )
    if row is None:
        return None
    return row["workspace_path"]


async def get_agent_by_id(agent_id: str) -> dict | None:
    """Get full agent record from Meta DB."""
    row = await query_one("SELECT * FROM agents WHERE id = ? LIMIT 1", [agent_id])
    if row is None:
        return None
    return dict(row)
