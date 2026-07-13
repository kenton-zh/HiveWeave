"""Per-project DB factory — manages per-project SQLite connections.

契约 11: Per-project DB
- 文件名: data.db（非 project.db — RECONCILE 修复）
- 位置: <workspace_path>/.hiveweave/data.db
- journal mode: DELETE（避免 Windows WAL 问题）
- busy_timeout: 5000
- 单连接（OpenCode Effect SqlClient 模型），asyncio 序列化
- 缓存：per-project DB 连接缓存，evict 时关闭
"""

import asyncio
from collections import OrderedDict

import aiosqlite
from pathlib import Path
from typing import Any

from hiveweave.db.schema import PROJECT_DB_TABLES, PROJECT_DB_INDEXES
from hiveweave.db import meta as meta_db

# ── Connection cache ────────────────────────────────────────
# key: workspace_path (normalized absolute path)
# value: aiosqlite.Connection
# R3: OrderedDict 实现 LRU — 访问时 move_to_end，超限时 evict 最旧的（popitem(last=False)）
MAX_CACHED_CONNECTIONS = 50
_cache: OrderedDict[str, aiosqlite.Connection] = OrderedDict()

# agent_id → workspace_path cache (avoids Meta DB lookup on every query)
_agent_cache: dict[str, str] = {}

# R2: 保护 ensure_project_db 的懒初始化，避免并发创建多个连接到同一 DB
_ensure_lock = asyncio.Lock()

# 已驱逐的工作区集合 — delete_project 调用 evict 后标记，
# 防止 cancel 路径的收尾 DB 操作通过 get_project_db_for_agent 重连锁住 data.db
_evicted_workspaces: set[str] = set()


def _db_path_for_workspace(workspace_path: str) -> str:
    """Get the per-project DB path for a workspace."""
    ws = Path(workspace_path).resolve()
    hw_dir = ws / ".hiveweave"
    hw_dir.mkdir(parents=True, exist_ok=True)
    return str(hw_dir / "data.db")


async def ensure_project_db(workspace_path: str) -> aiosqlite.Connection | None:
    """Get or create the per-project DB for a workspace.

    契约 11: ensureProjectDb(workspacePath) lazily creates a per-project DB.
    返回 None 表示 workspace 已被驱逐（项目删除中），调用方应处理此情况。

    R2: 使用 asyncio.Lock 保护，避免并发调用创建多个连接。
    R3: 缓存上限 MAX_CACHED_CONNECTIONS=50，超限时 evict 最久未用的连接（LRU）。
    """
    ws = str(Path(workspace_path).resolve())

    # 驱逐检查 — 项目删除后拒绝重连，防止 cancel 收尾操作锁住 data.db
    # 返回 None 而非 raise，与 get_project_db_for_agent 保持一致；
    # 调用方已有 None 检查，raise 会导致未 try/except 的新端点 500
    if ws in _evicted_workspaces:
        return None

    # 快速路径：无锁检查缓存（命中时只需 move_to_end，但需加锁保证 OrderedDict 一致）
    async with _ensure_lock:
        if ws in _cache:
            _cache.move_to_end(ws)  # LRU: 标记为最近使用
            return _cache[ws]

        db_path = _db_path_for_workspace(workspace_path)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row

        # Bug F fix: WAL 模式支持多进程并发读写，外部只读进程不会损坏数据
        # BUG-009/012/013 fix: explicitly set UTF-8 encoding to prevent CJK mojibake
        await conn.execute("PRAGMA encoding = 'UTF-8'")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")

        # Create tables + migrations (ALTER TABLE failures are non-fatal — column already exists)
        for sql in PROJECT_DB_TABLES:
            try:
                await conn.execute(sql)
            except Exception:
                # ALTER TABLE ADD COLUMN fails if column exists — safe to ignore
                if not sql.strip().upper().startswith("ALTER"):
                    raise

        # Create indexes
        for sql in PROJECT_DB_INDEXES:
            await conn.execute(sql)

        await conn.commit()

        _cache[ws] = conn

        # R3: LRU evict — 超过上限时关闭并移除最久未用的连接
        while len(_cache) > MAX_CACHED_CONNECTIONS:
            _, old_conn = _cache.popitem(last=False)
            try:
                await old_conn.close()
            except Exception:
                pass  # best-effort

        return conn


async def get_project_db_for_agent(agent_id: str) -> aiosqlite.Connection | None:
    """Get the per-project DB for an agent.

    契约 11: lookupAgentWorkspace() → getProjectDbForAgent()
    1. Check agent_id → workspace_path cache
    2. If miss, query Meta DB for agent's project_id
    3. Query Meta DB for project's workspace_path
    4. ensure_project_db(workspace_path)
    5. Cache the mapping
    """
    # Check cache
    if agent_id in _agent_cache:
        ws = _agent_cache[agent_id]
        if ws in _evicted_workspaces:
            return None  # 项目已删除，拒绝重连
        if ws in _cache:
            return _cache[ws]

    # Query Meta DB for project_id
    project_id = await meta_db.get_agent_project_id(agent_id)
    if project_id is None:
        return None

    # Query Meta DB for workspace_path
    workspace_path = await meta_db.get_project_workspace(project_id)
    if workspace_path is None:
        return None

    ws_resolved = str(Path(workspace_path).resolve())
    if ws_resolved in _evicted_workspaces:
        return None  # 项目已删除，拒绝重连

    # Ensure DB exists
    conn = await ensure_project_db(workspace_path)

    # Cache the mapping
    _agent_cache[agent_id] = ws_resolved

    return conn


async def get_project_db_by_project_id(project_id: str) -> aiosqlite.Connection | None:
    """Get the per-project DB connection by project_id.

    路由链: project_id → meta_db.projects → workspace_path → ensure_project_db
    Convenience helper for services that have project_id but not agent_id.
    """
    workspace_path = await meta_db.get_project_workspace(project_id)
    if workspace_path is None:
        return None
    return await ensure_project_db(workspace_path)


async def evict_project_db(workspace_path: str) -> None:
    """Close and remove a per-project DB from cache.

    契约 11: evictProjectDb() — best-effort close, caller catches errors.
    R2: 加锁保证与 ensure_project_db 的缓存操作互斥。
    标记 workspace 为已驱逐，后续 get_project_db_for_agent / ensure_project_db
    拒绝重连，防止 delete_project 收尾阶段 cancel 路径重连锁住 data.db。
    """
    ws = str(Path(workspace_path).resolve())
    _evicted_workspaces.add(ws)  # 标记 — 拒绝后续重连
    async with _ensure_lock:
        conn = _cache.pop(ws, None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            pass  # best-effort

    # Clean agent cache for this workspace
    to_remove = [aid for aid, w in _agent_cache.items() if w == ws]
    for aid in to_remove:
        del _agent_cache[aid]


async def evict_project_db_for_agent(agent_id: str) -> None:
    """Close the per-project DB connection associated with an agent.

    Used during project deletion to ensure all agent-related DB connections
    are released before attempting to delete the .hiveweave directory.
    """
    ws = _agent_cache.get(agent_id)
    if ws is None:
        return
    async with _ensure_lock:
        conn = _cache.pop(ws, None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            pass
    _agent_cache.pop(agent_id, None)


async def close_all() -> None:
    """Close all per-project DB connections (shutdown)."""
    global _cache, _agent_cache
    for conn in _cache.values():
        try:
            await conn.close()
        except Exception:
            pass
    _cache.clear()
    _agent_cache.clear()
    _evicted_workspaces.clear()


def clear_evicted_workspace(workspace_path: str) -> None:
    """清除工作区的驱逐标记 — 用于同路径重建项目时恢复 DB 访问。"""
    ws = str(Path(workspace_path).resolve())
    _evicted_workspaces.discard(ws)


# ── Query helpers ───────────────────────────────────────────


async def query(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> list[aiosqlite.Row]:
    """Execute a SELECT query on the per-project DB for an agent."""
    conn = await get_project_db_for_agent(agent_id)
    if conn is None:
        raise ValueError(f"No project DB found for agent {agent_id}")
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def query_one(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> aiosqlite.Row | None:
    """Execute a SELECT query and return a single row."""
    conn = await get_project_db_for_agent(agent_id)
    if conn is None:
        raise ValueError(f"No project DB found for agent {agent_id}")
    cursor = await conn.execute(sql, params or [])
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def execute(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> None:
    """Execute an INSERT/UPDATE/DELETE on the per-project DB for an agent."""
    conn = await get_project_db_for_agent(agent_id)
    if conn is None:
        raise ValueError(f"No project DB found for agent {agent_id}")
    await conn.execute(sql, params or [])
    await conn.commit()
