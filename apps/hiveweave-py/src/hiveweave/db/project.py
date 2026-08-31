"""Per-project DB factory — manages per-project SQLite connections.

契约 11: Per-project DB
- 文件名: data.db（非 project.db — RECONCILE 修复）
- 位置: <workspace_path>/.hiveweave/data.db
- journal mode: DELETE（避免 Windows WAL 问题）
- busy_timeout: env HIVEWEAVE_SQLITE_BUSY_TIMEOUT_MS (default 5000)
- 单连接（OpenCode Effect SqlClient 模型），asyncio 序列化
- 缓存：per-project DB 连接缓存，evict 时关闭
"""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import sqlite3

import aiosqlite
from pathlib import Path
from typing import Any

from hiveweave.db.schema import (
    PROJECT_DB_TABLES,
    PROJECT_DB_INDEXES,
    PROJECT_DB_COLUMN_CHECKS,
)
from hiveweave.db import meta as meta_db


class ProjectDbError(RuntimeError):
    """Per-project DB 不可用。

    触发条件：
    - workspace 已被驱逐（项目删除中，防重连）
    - project_id / agent_id 在 Meta DB 中查不到 workspace_path

    继承 RuntimeError 而非 ValueError：
    - 语义是"运行时状态不可用"而非"参数错误"
    - 避免被 `except ValueError` 误捕获（调用方应显式捕获或让异常传播）
    """


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

# 写操作事务级互斥（TEST18 审计 S1）：单连接 + 多协程（同项目多 agent 的
# 写队列 worker 并发）下，BEGIN IMMEDIATE..COMMIT 必须整段持锁——
# aiosqlite 只保证语句级 FIFO，不保证事务内无他人语句插入（他人 COMMIT/
# rollback 会提前终止或回滚本事务）。key = workspace 绝对路径。
_write_locks: dict[str, asyncio.Lock] = {}

# 已驱逐的工作区集合 — delete_project 调用 evict 后标记，
# 防止 cancel 路径的收尾 DB 操作通过 get_project_db_for_agent 重连锁住 data.db
_evicted_workspaces: set[str] = set()


def _db_path_for_workspace(workspace_path: str) -> str:
    """Get the per-project DB path for a workspace."""
    ws = Path(workspace_path).resolve()
    hw_dir = ws / ".hiveweave"
    hw_dir.mkdir(parents=True, exist_ok=True)
    return str(hw_dir / "data.db")


async def ensure_project_db(workspace_path: str) -> aiosqlite.Connection:
    """Get or create the per-project DB for a workspace.

    契约 11: ensureProjectDb(workspacePath) lazily creates a per-project DB.

    失败时 raise ProjectDbError：
    - workspace 已被驱逐（项目删除中），拒绝重连

    R2: 使用 asyncio.Lock 保护，避免并发调用创建多个连接。
    R3: 缓存上限 MAX_CACHED_CONNECTIONS=50，超限时 evict 最久未用的连接（LRU）。
    """
    ws = str(Path(workspace_path).resolve())

    # 驱逐检查 — 项目删除后拒绝重连，防止 cancel 收尾操作锁住 data.db
    if ws in _evicted_workspaces:
        raise ProjectDbError(
            f"Workspace evicted (project deletion in progress): {ws}"
        )

    # 快速路径：无锁检查缓存（命中时只需 move_to_end，但需加锁保证 OrderedDict 一致）
    async with _ensure_lock:
        if ws in _cache:
            _cache.move_to_end(ws)  # LRU: 标记为最近使用
            return _cache[ws]

        db_path = _db_path_for_workspace(workspace_path)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row

        # 契约 11: DELETE journal mode（避免 Windows WAL 孤儿化/代际分叉损坏）
        #   WAL 依赖 -shm 文件做跨进程协调，Windows 下多连接打开同一库时
        #   主库与 WAL 易分叉成不同代际（salt 不匹配）→ B-tree 损坏。
        #   TEST18 事故根因（2026-08-05）：WAL 回归 + 双后端进程并发打开
        #   同一库，旧 WAL 成孤儿，后续写入绕过 WAL 直接落主库 →
        #   "invalid page number" 损坏。单后端架构靠 asyncio 锁串行化访问，
        #   DELETE 模式完全安全（busy_timeout 兜底并发）。
        # BUG-009/012/013 fix: explicitly set UTF-8 encoding to prevent CJK mojibake
        await conn.execute("PRAGMA encoding = 'UTF-8'")
        await conn.execute("PRAGMA journal_mode=DELETE")
        from hiveweave.config import sqlite_busy_timeout_sql

        await conn.execute(sqlite_busy_timeout_sql())
        await conn.execute("PRAGMA foreign_keys=ON")

        # Create tables + migrations (ALTER TABLE failures are non-fatal — column already exists)
        for sql in PROJECT_DB_TABLES:
            try:
                await conn.execute(sql)
            except Exception:
                # ALTER TABLE ADD COLUMN fails if column exists — safe to ignore
                if not sql.strip().upper().startswith("ALTER"):
                    raise

        # 建表自检（TEST_DSH_37 P0-① 防护）：关键列缺失 = 迁移断裂（如 ALTER
        # 排在 CREATE 前被上面的吞异常吃掉），后续 INSERT 会全部静默失败 ——
        # 记账/事实位丢失必须 fail-loud，启动即崩比静默断账好。
        for table, required_cols in PROJECT_DB_COLUMN_CHECKS.items():
            cur = await conn.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            cols = {row[1] for row in rows}
            missing = required_cols - cols
            if missing:
                try:
                    await conn.close()
                except Exception:
                    pass  # best-effort：连接关闭失败不掩盖根因
                raise ProjectDbError(
                    f"schema self-check failed: table '{table}' missing "
                    f"column(s) {sorted(missing)} — migration broken, "
                    f"INSERTs would fail silently (TEST_DSH_37 P0-1)"
                )

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


async def get_project_db_for_agent(agent_id: str) -> aiosqlite.Connection:
    """Get the per-project DB for an agent.

    契约 11: lookupAgentWorkspace() → getProjectDbForAgent()
    1. Check agent_id → workspace_path cache
    2. If miss, query Meta DB for agent's project_id
    3. Query Meta DB for project's workspace_path
    4. ensure_project_db(workspace_path)
    5. Cache the mapping

    失败时 raise ProjectDbError：
    - agent_id 在 Meta DB 中查不到 project_id
    - project_id 查不到 workspace_path
    - workspace 已被驱逐（透传 ensure_project_db 的 ProjectDbError）
    """
    # Check cache
    if agent_id in _agent_cache:
        ws = _agent_cache[agent_id]
        if ws in _evicted_workspaces:
            raise ProjectDbError(
                f"Workspace evicted (project deletion in progress): {ws}"
            )
        if ws in _cache:
            return _cache[ws]

    # Query Meta DB for project_id
    project_id = await meta_db.get_agent_project_id(agent_id)
    if project_id is None:
        raise ProjectDbError(
            f"No project found for agent_id={agent_id} (agent not registered in Meta DB)"
        )

    # Query Meta DB for workspace_path
    workspace_path = await meta_db.get_project_workspace(project_id)
    if workspace_path is None:
        raise ProjectDbError(
            f"No workspace_path for project_id={project_id} (project record incomplete)"
        )

    ws_resolved = str(Path(workspace_path).resolve())
    if ws_resolved in _evicted_workspaces:
        raise ProjectDbError(
            f"Workspace evicted (project deletion in progress): {ws_resolved}"
        )

    # Ensure DB exists — 可能 raise ProjectDbError，直接传播
    conn = await ensure_project_db(workspace_path)

    # Cache the mapping
    _agent_cache[agent_id] = ws_resolved

    return conn


async def get_project_db_by_project_id(project_id: str) -> aiosqlite.Connection:
    """Get the per-project DB connection by project_id.

    路由链: project_id → meta_db.projects → workspace_path → ensure_project_db
    Convenience helper for services that have project_id but not agent_id.

    失败时 raise ProjectDbError：
    - project_id 查不到 workspace_path
    - workspace 已被驱逐（透传 ensure_project_db 的 ProjectDbError）
    """
    workspace_path = await meta_db.get_project_workspace(project_id)
    if workspace_path is None:
        raise ProjectDbError(
            f"No workspace_path for project_id={project_id} (project not found in Meta DB)"
        )
    return await ensure_project_db(workspace_path)


# ── Read-only connection pool (Timeline v4 §4.4) ────────────
# 聚合只读查询（timeline 端点）与写路径物理隔离：独立只读池 + slot 锁。
# 注意：DELETE 模式下跨"读写"的互斥来自 SQLite 文件锁 + busy_timeout（5s），
# 而非 asyncio 锁——只读池 slot.lock 与写路径 workspace 写锁是两把独立的锁，
# 互不协调。因此长读事务可能阻塞写方 COMMIT 最多 5s 后抛 "database is locked"
# （timeline 查询 limit 上限小，通常毫秒级，实际触发概率低）。这是相对 WAL
# "读者不阻塞写者"的刻意取舍——换取规避 Windows WAL 孤儿化/代际分叉，
# 若后续出现写方锁超时，优先收紧 timeline 查询时长而非改回 WAL。
# - 每 workspace 独立池（2 条连接），每条连接绑一把 asyncio.Lock：
#   aiosqlite 只串行化单条 execute、不串行化事务块，两个并发请求共用
#   一条连接会 BEGIN 交错（"cannot start a transaction within a transaction"），
#   所以请求获取连接 = 持锁直到 COMMIT 释放。
# - 驱逐联动：evict_project_db 同关只读池；打开前检查 _evicted_workspaces；
#   打开失败降级回共享连接（只覆盖打开失败，陈旧靠驱逐联动兜底）。
_READONLY_POOL_SIZE = 2


class _ReadonlySlot:
    """一条只读连接 + 它的事务锁。"""

    __slots__ = ("conn", "lock")

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()


# workspace -> slots；round-robin 计数独立存放
_readonly_pools: dict[str, list[_ReadonlySlot]] = {}
_readonly_rr: dict[str, int] = {}
_readonly_pool_guard = asyncio.Lock()


def _sqlite_readonly_uri(db_path: str) -> str:
    """把绝对路径转成 sqlite URI（mode=ro）。

    Windows 绝对路径必须是 ``file:///C:/...``（三斜杠 + 正斜杠），
    POSIX 是 ``file:///home/...``——直接 f"file:{path}" 在 Windows 会
    被 URI 解析器当成不透明路径而打开失败。路径做百分号编码，
    兼容空格 / CJK 字符的 workspace。
    """
    from urllib.parse import quote

    posix = Path(db_path).resolve().as_posix()
    encoded = quote(posix, safe="/:")
    if encoded.startswith("/"):
        return f"file://{encoded}?mode=ro"   # POSIX: file:///home/...
    return f"file:///{encoded}?mode=ro"      # Windows: file:///C:/...


async def _acquire_readonly_slot(ws: str) -> _ReadonlySlot | None:
    """取（或惰性创建）workspace 的只读池 slot；打开失败返回 None。"""
    async with _readonly_pool_guard:
        if ws in _evicted_workspaces:
            return None
        pool = _readonly_pools.get(ws)
        if pool is None:
            db_path = _db_path_for_workspace(ws)
            uri = _sqlite_readonly_uri(db_path)
            building: list[_ReadonlySlot] = []
            try:
                for _ in range(_READONLY_POOL_SIZE):
                    conn = await aiosqlite.connect(uri, uri=True)
                    slot = _ReadonlySlot(conn)
                    building.append(slot)  # 先入列：PRAGMA 失败也能被清理
                    conn.row_factory = aiosqlite.Row
                    from hiveweave.config import sqlite_busy_timeout_sql

                    await conn.execute(sqlite_busy_timeout_sql())
            except Exception:
                # DB 文件尚不存在 / 权限问题 → 调用方降级共享连接
                for slot in building:
                    try:
                        await slot.conn.close()
                    except Exception:
                        pass
                return None
            _readonly_pools[ws] = building
            _readonly_rr[ws] = 0
            pool = building
        idx = _readonly_rr.get(ws, 0) % len(pool)
        _readonly_rr[ws] = (idx + 1) % len(pool)
        return pool[idx]


async def _close_readonly_pool(ws: str) -> None:
    """关闭并移除 workspace 的只读池（evict / shutdown 用）。"""
    async with _readonly_pool_guard:
        pool = _readonly_pools.pop(ws, None)
        _readonly_rr.pop(ws, None)
    if pool:
        for slot in pool:
            try:
                await slot.conn.close()
            except Exception:
                pass


@asynccontextmanager
async def readonly_project_conn(project_id: str):
    """只读连接 context manager（timeline 聚合查询专用，Timeline v4 §4.4）。

    用法::

        async with readonly_project_conn(project_id) as conn:
            await conn.execute("BEGIN")
            ... SELECTs ...
            await conn.execute("COMMIT")

    请求应持连接完成整个读事务——slot 锁在 context 退出时释放。
    打开只读池失败时降级为共享连接（此时持 workspace 写锁防事务交错）。
    失败时 raise ProjectDbError（项目不存在 / 已驱逐）。
    """
    workspace_path = await meta_db.get_project_workspace(project_id)
    if workspace_path is None:
        raise ProjectDbError(
            f"No workspace_path for project_id={project_id} (project not found in Meta DB)"
        )
    ws = str(Path(workspace_path).resolve())
    if ws in _evicted_workspaces:
        raise ProjectDbError(
            f"Workspace evicted (project deletion in progress): {ws}"
        )

    slot = await _acquire_readonly_slot(ws)
    if slot is not None:
        async with slot.lock:
            yield slot.conn
        return

    # 降级：共享连接 + workspace 写锁（只覆盖打开失败场景）
    lock = await get_workspace_write_lock(ws)
    async with lock:
        yield await ensure_project_db(workspace_path)


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
    _write_locks.pop(ws, None)
    # 回退键（f"agent:{aid}"，见 _get_write_lock）一并清理，防键泄漏
    for aid in [a for a, w in _agent_cache.items() if w == ws]:
        _write_locks.pop(f"agent:{aid}", None)
    await _close_readonly_pool(ws)  # Timeline v4 §4.4: 驱逐同关只读池
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
    await _close_readonly_pool(ws)  # 与共享连接同进退（可惰性重开）
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
    _write_locks.clear()
    # Timeline v4 §4.4: 关停只读池
    for ws in list(_readonly_pools.keys()):
        await _close_readonly_pool(ws)


def clear_evicted_workspace(workspace_path: str) -> None:
    """清除工作区的驱逐标记 — 用于同路径重建项目时恢复 DB 访问。"""
    ws = str(Path(workspace_path).resolve())
    _evicted_workspaces.discard(ws)


# ── Query helpers ───────────────────────────────────────────


async def query(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> list[aiosqlite.Row]:
    """Execute a SELECT query on the per-project DB for an agent.

    底层 get_project_db_for_agent 失败时 raise ProjectDbError，由调用方处理。
    """
    conn = await get_project_db_for_agent(agent_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def query_one(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> aiosqlite.Row | None:
    """Execute a SELECT query and return a single row.

    底层 get_project_db_for_agent 失败时 raise ProjectDbError，由调用方处理。
    """
    conn = await get_project_db_for_agent(agent_id)
    cursor = await conn.execute(sql, params or [])
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def query_by_project(
    project_id: str, sql: str, params: list[Any] | None = None
) -> list[aiosqlite.Row]:
    """Execute a SELECT query on the per-project DB for a project_id.

    供聚合只读查询（token metering / timeline）使用，通过 project_id 路由。
    底层 get_project_db_by_project_id 失败时 raise ProjectDbError，由调用方处理。
    """
    conn = await get_project_db_by_project_id(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def get_workspace_write_lock(workspace_path: str) -> asyncio.Lock:
    """Get (or create) the per-workspace write lock（供按 workspace 解析连接的调用方）。"""
    ws = str(Path(workspace_path).resolve())
    lock = _write_locks.get(ws)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[ws] = lock
    return lock


async def _get_write_lock(agent_id: str) -> asyncio.Lock:
    """Get (or create) the per-workspace write lock for an agent.

    所有写操作（execute / execute_transaction）共用同一把锁串行化，
    保证单连接上事务不会被其他协程击穿。
    """
    conn = await get_project_db_for_agent(agent_id)  # 填充 _agent_cache 映射
    ws = _agent_cache.get(agent_id)
    if ws is None:
        # 仅测试/异常路径（真实运行 get_project_db_for_agent 必填充映射）：
        # 回退 agent 级锁（不串行化同 ws 多 agent，但无锁安全）
        ws = f"agent:{agent_id}"
    return await get_workspace_write_lock(ws)


def is_sqlite_lock_error(exc: BaseException) -> bool:
    """True for lock/busy/timeout OperationalError (clone_03 write timeouts)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg or "timed out" in msg


async def _execute_write_with_retry(
    conn: aiosqlite.Connection, sql: str, params: list[Any] | None = None
) -> None:
    """Single-statement write with 2 extra retries on SQLite lock/busy."""
    last: BaseException | None = None
    for attempt in range(3):
        try:
            await conn.execute(sql, params or [])
            await conn.commit()
            return
        except sqlite3.OperationalError as e:
            last = e
            try:
                await conn.rollback()
            except Exception:
                pass
            if not is_sqlite_lock_error(e) or attempt >= 2:
                raise
            await asyncio.sleep(0.05 * (2 ** attempt))
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
    if last is not None:
        raise last


async def execute(
    agent_id: str, sql: str, params: list[Any] | None = None
) -> None:
    """Execute an INSERT/UPDATE/DELETE on the per-project DB for an agent.

    底层 get_project_db_for_agent 失败时 raise ProjectDbError，由调用方处理。
    写操作经 per-workspace 锁串行化（见 _get_write_lock）。
    """
    lock = await _get_write_lock(agent_id)
    async with lock:
        conn = await get_project_db_for_agent(agent_id)
        await _execute_write_with_retry(conn, sql, params)


async def execute_transaction(
    agent_id: str, statements: list[tuple[str, list[Any] | None]]
) -> None:
    """Execute multiple statements in ONE transaction (BEGIN IMMEDIATE).

    多语句写必须单事务（契约 11 纪律）——例如压缩持久化的
    "DELETE 旧 turn + INSERT 新 turn"：分两条 execute 会在中间暴露
    空历史窗口，且 INSERT 失败时 DELETE 已提交 = 历史永久丢失
    （TEST18 巡检 P1）。异常时回滚并上抛（调用方负责告警/计数）。
    整段事务持 per-workspace 写锁（TEST18 审计 S1）：否则同一连接上
    其他协程的 COMMIT/rollback 会提前终止或回滚本事务。
    """
    lock = await _get_write_lock(agent_id)
    async with lock:
        conn = await get_project_db_for_agent(agent_id)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                await conn.execute(sql, params or [])
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


# ── project_id 键控的写 helpers（2026-08-13 并发审计收编）─────
# 共享连接纪律：同一 workspace 连接上的**每个写者**，execute→commit
# 整段必须持 per-workspace 写锁——无锁写者与锁内事务会互相击穿
# （他人 commit 提前提交半成品 / 他人 rollback 静默丢写）。以下两个
# 函数把该纪律封装为 project_id 键控的公共入口，供无 agent_id 上下文
# 的服务（game_time/wait_contract/chat_message/inbox/handoff/charter/
# roster/obligation/dispatch/org/attestation/work_log/staffing 等）复用。


async def execute_by_project(
    project_id: str, sql: str, params: list[Any] | None = None
) -> None:
    """单语句写：per-workspace 写锁 + try/rollback/re-raise（同 execute()）。

    project_id → workspace 经 meta_db.get_project_workspace 解析
    （同 services/tasks/db._execute）。workspace 缺失 raise ProjectDbError。
    """
    workspace_path = await meta_db.get_project_workspace(project_id)
    if not workspace_path:
        raise ProjectDbError(f"No workspace_path for project_id={project_id}")
    ws = str(Path(workspace_path).resolve())
    lock = await get_workspace_write_lock(ws)
    async with lock:
        conn = await ensure_project_db(ws)
        await _execute_write_with_retry(conn, sql, params)


async def execute_transaction_by_project(
    project_id: str, statements: list[tuple[str, list[Any] | None]]
) -> None:
    """多语句单事务写：写锁 + BEGIN IMMEDIATE + rollback/re-raise
    （同 execute_transaction，按 project_id 解析 workspace）。"""
    workspace_path = await meta_db.get_project_workspace(project_id)
    if not workspace_path:
        raise ProjectDbError(f"No workspace_path for project_id={project_id}")
    ws = str(Path(workspace_path).resolve())
    lock = await get_workspace_write_lock(ws)
    async with lock:
        conn = await ensure_project_db(ws)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                await conn.execute(sql, params or [])
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
