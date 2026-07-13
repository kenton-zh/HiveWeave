"""Meta DB — global SQLite database for projects + global config.

契约 11: Meta DB
- 全局单例，WAL 模式
- 表: projects (id, name, workspace_path, created_at), agent_templates, llm_models, global_settings, meta_index
- 不再存储 agent_index 或任何 per-project 业务数据
- agent_id → project_id 路由由 AgentRouter 内存映射完成
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
# 旧 Meta DB 可能有已删除的表（agent_index, agent_charters, chat_messages 等）。
# 迁移时执行 DROP TABLE IF EXISTS 清理这些遗留表。
# 同时用 ALTER TABLE ADD COLUMN 补齐缺失列。

_META_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, column_def)
    # agent_templates — TS schema 无 updated_at
    ("agent_templates", "updated_at", "INTEGER"),
    # agent_templates — add discipline_suite
    ("agent_templates", "discipline_suite", "TEXT DEFAULT ''"),
    # llm_models — add provider_type for multi-format LLM support
    ("llm_models", "provider_type", "TEXT DEFAULT ''"),
    # Bug J fix: add fallback column for circuit breaker fallback
    ("llm_models", "fallback", "TEXT"),
    # Bug K fix: per-project is_started flag (上班/下班)
    ("projects", "is_started", "INTEGER DEFAULT 0"),
    # 注: 不再添加 projects.language — 该列的真相源在 per-project DB
    # 的 project_meta 表（见 db/schema.py PROJECT_DB_TABLES），见
    # api/projects.py:_fetch_project_meta 注释。旧 DB 残留的 language 列
    # 是孤儿列, 无害, 不读即可。
]

# 旧 Meta DB 中需要 DROP 的遗留表（已迁移到 per-project DB 或已废弃）
_LEGACY_TABLES_TO_DROP = [
    "agent_index",
    "agent_charters",
    "charter_attachments",
    "chat_messages",
    "conversation_turns",
    "handoffs",
    "inbox",
    "memories",
    "work_logs",
    "personnel_records",
    "permission_requests",
    "permission_rules",
    "modules",
    "merges",
    "__new_agents",
]

# 旧 Meta DB 中需要 DROP 的遗留列（真相源已迁到 per-project DB project_meta）
# 见 db/schema.py PROJECT_DB_TABLES / api/projects.py:_fetch_project_meta 注释
_LEGACY_COLUMNS_TO_DROP = [
    ("projects", "language"),
]


async def _migrate_meta_schema(conn: aiosqlite.Connection) -> None:
    """Run schema migrations — drop legacy tables + add missing columns.

    R7: 使用 per-connection 的 _migrated 标记，避免每次调用都重复执行。
    标记在 close_meta_db 中重置，保证 DB 重建后能重新迁移。
    """
    global _migrated
    if _migrated:
        return

    # 1. Drop legacy per-project tables that no longer belong in Meta DB
    for table_name in _LEGACY_TABLES_TO_DROP:
        try:
            await conn.execute(f"DROP TABLE IF EXISTS [{table_name}]")
        except Exception:
            pass  # Table doesn't exist — fine

    # 2. Drop legacy indexes that reference dropped tables
    legacy_indexes = [
        "idx_agent_index_project_id",
        "idx_agent_index_short_id",
        "idx_agent_charters_project_id",
    ]
    for idx_name in legacy_indexes:
        try:
            await conn.execute(f"DROP INDEX IF EXISTS [{idx_name}]")
        except Exception:
            pass

    # 3. Add missing columns to remaining tables
    for table, column, col_def in _META_MIGRATIONS:
        try:
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
            )
            log.info("schema_migrated", table=table, column=column)
        except aiosqlite.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "no such table" in msg:
                continue
            raise

    # 4. Drop legacy columns that no longer belong in Meta DB
    #    真相源已迁到 per-project DB (见 db/schema.py PROJECT_DB_TABLES / project_meta).
    for table, column in _LEGACY_COLUMNS_TO_DROP:
        try:
            await conn.execute(
                f"ALTER TABLE {table} DROP COLUMN {column}"
            )
            log.info("schema_column_dropped", table=table, column=column)
        except aiosqlite.OperationalError as e:
            msg = str(e).lower()
            if "no such column" in msg or "no such table" in msg:
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
        if _db is not None:
            return

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        _db = await aiosqlite.connect(db_path)
        _db.row_factory = aiosqlite.Row

        # WAL mode for Meta DB (global, concurrent reads)
        await _db.execute("PRAGMA encoding = 'UTF-8'")
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout=5000")
        await _db.execute("PRAGMA foreign_keys=ON")

        # Create tables
        for sql in META_DB_TABLES:
            await _db.execute(sql)

        # Create indexes
        for sql in META_DB_INDEXES:
            await _db.execute(sql)

        # Migrate: drop legacy tables + add missing columns
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


# ── Project routing (Meta DB projects table) ──────────────

async def get_project_workspace(project_id: str) -> str | None:
    """Look up a project's workspace path."""
    row = await query_one(
        "SELECT workspace_path FROM projects WHERE id = ? LIMIT 1", [project_id]
    )
    if row is None:
        return None
    return row["workspace_path"]


async def get_agent_by_id(agent_id: str) -> dict | None:
    """Get full agent record from per-project DB.

    路由链: agent_id → AgentRouter(内存) → project_id → projects(Meta DB) → workspace_path
            → per-project DB → agents 表
    """
    from hiveweave.services.agent_router import agent_router

    # Step 1: agent_id → project_id (in-memory route)
    project_id = agent_router.get_project_id(agent_id)
    if project_id is None:
        return None

    # Step 2: project_id → workspace_path
    workspace_path = await get_project_workspace(project_id)
    if workspace_path is None:
        return None

    # Step 3: Open per-project DB and query agents table
    from hiveweave.db.project import ensure_project_db
    conn = await ensure_project_db(workspace_path)
    if conn is None:
        return None

    cursor = await conn.execute(
        "SELECT * FROM agents WHERE id = ? LIMIT 1", [agent_id]
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return dict(row)


async def get_agent_project_id(agent_id: str) -> str | None:
    """Look up which project an agent belongs to.

    路由链: agent_id → AgentRouter(内存) → project_id
    """
    from hiveweave.services.agent_router import agent_router
    return agent_router.get_project_id(agent_id)
