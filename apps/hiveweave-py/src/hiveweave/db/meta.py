"""Meta DB — global SQLite database for projects, agents, models, settings.

契约 11: Meta DB
- 全局单例，WAL 模式
- 表: projects, agents, agent_templates, llm_models, global_settings, agent_charters, charter_attachments, meta_index
- agents 表在此（RECONCILE 修复：agent 路由依赖 Meta DB 的 agents 表）
"""

import asyncio

import aiosqlite
import structlog
from pathlib import Path
from typing import Any

from hiveweave.db.schema import META_DB_TABLES, META_DB_INDEXES
from hiveweave.config import settings as app_settings

log = structlog.get_logger(__name__)

_db: aiosqlite.Connection | None = None

# R1: 保护 init_meta_db 的懒初始化，避免并发调用创建多个连接
_init_lock = asyncio.Lock()

# R7: per-connection 迁移标记。close_meta_db 时重置为 False，
# 这样 DB 被重建后重新 init 会重新执行迁移（而非误认为已迁移）。
_migrated: bool = False


# ── Schema migration ───────────────────────────────────────
# Meta DB 现位于 apps/hiveweave-py/data/hiveweave.db。
# 早期复用 TS 后端创建的 DB，可能缺少 Python 期望的某些列；
# 以下迁移在创建表之后执行，用 ALTER TABLE ADD COLUMN 补齐，try/except 忽略"列已存在"。
#
# Python schema: apps/hiveweave-py/src/hiveweave/db/schema.py (META_DB_TABLES)
#
# 缺失列清单（旧 TS 表已存在但缺列）：
#   projects:        language, updated_at
#   agents:          workspace_path, language  （TS Drizzle schema 无此两列）
#   agent_templates: updated_at                （TS schema 无此列）

_META_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, column_def)
    # projects — TS client.ts 创建时无 language / updated_at
    ("projects", "language", "TEXT DEFAULT 'en'"),
    ("projects", "updated_at", "INTEGER"),
    # agents — TS Drizzle schema 无 workspace_path / language
    ("agents", "workspace_path", "TEXT"),
    ("agents", "language", "TEXT DEFAULT 'en'"),
    # agent_templates — TS schema 无 updated_at
    ("agent_templates", "updated_at", "INTEGER"),
]


async def _migrate_meta_schema(conn: aiosqlite.Connection) -> None:
    """Run schema migrations for columns missing from TS-created tables.

    对每个可能缺失的列执行 ALTER TABLE ADD COLUMN，用 try/except 忽略
    "duplicate column name"（列已存在）和 "no such table"（表不存在）错误。

    R7: 使用 per-connection 的 _migrated 标记，避免每次调用都重复执行 ALTER。
    标记在 close_meta_db 中重置，保证 DB 重建后能重新迁移。
    """
    global _migrated
    if _migrated:
        return
    for table, column, col_def in _META_MIGRATIONS:
        try:
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
            )
            log.info("schema_migrated", table=table, column=column)
        except aiosqlite.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "no such table" in msg:
                # 列已存在（本就是 Python 创建的表）或表不存在（跳过）
                continue
            raise
    await conn.commit()
    _migrated = True


async def init_meta_db() -> None:
    """Initialize Meta DB — create tables and indexes if not exist.

    R1: 使用 asyncio.Lock 保护懒初始化，并发调用时只创建一个连接。
    """
    global _db
    db_path = app_settings.get_meta_db_path()

    async with _init_lock:
        # Double-check：持锁后再次确认，已初始化则直接返回
        if _db is not None:
            return

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

        # Migrate columns missing from TS-created tables
        await _migrate_meta_schema(_db)

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
    global _db, _migrated
    async with _init_lock:
        if _db is not None:
            await _db.close()
            _db = None
        # R7: 重置迁移标记，DB 重建后重新 init 会重新执行迁移
        _migrated = False


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
