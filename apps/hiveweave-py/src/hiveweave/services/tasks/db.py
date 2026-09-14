"""Task ledger DB helpers."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    _execute_write_with_retry,
    ensure_project_db,
    get_workspace_write_lock,
)

from .constants import _MISSING_COLUMNS

log = structlog.get_logger(__name__)

_migrated: set[tuple[str, int]] = set()

async def _resolve_workspace(project_id: str) -> str:
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise ProjectDbError(f"Workspace not found for project {project_id}")
    return workspace


async def _conn(project_id: str) -> aiosqlite.Connection:
    """Resolve project_id to per-project DB connection."""
    return await ensure_project_db(await _resolve_workspace(project_id))


async def _query(project_id: str, sql: str, params: list | None = None) -> list:
    conn = await _conn(project_id)
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _execute(project_id: str, sql: str, params: list | None = None) -> None:
    # 与 execute/execute_transaction/_execute_tx 共用同一条 per-workspace
    # 连接：不持锁会击穿他人 BEGIN IMMEDIATE 事务（提前 COMMIT/回滚）。
    workspace = await _resolve_workspace(project_id)
    lock = await get_workspace_write_lock(workspace)
    async with lock:
        conn = await ensure_project_db(workspace)
        await _execute_write_with_retry(conn, sql, params)


async def _execute_tx(
    project_id: str, statements: list[tuple[str, list]]
) -> None:
    """Execute multiple SQL statements in a single transaction.

    Used by the Transactional Outbox: the state transition and the event
    record are written atomically — either both commit or neither does.

    纪律（TEST18 审计 S1，与 db/project.execute_transaction 同款）：
    BEGIN IMMEDIATE..COMMIT 整段持 per-workspace 写锁，否则同一共享连接上
    其他协程的 COMMIT/rollback 会提前终止或回滚本事务。异常回滚并上抛。
    """
    workspace = await _resolve_workspace(project_id)
    lock = await get_workspace_write_lock(workspace)
    async with lock:
        conn = await ensure_project_db(workspace)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                await conn.execute(sql, params)
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


async def _ensure_schema(project_id: str) -> None:
    """Add missing columns to tasks table (idempotent).

    标记键 = ``(workspace, 连接世代)``（机制见
    :func:`db.project.schema_marker_key_for_project`）—— tasks 是最核心的表，
    按 project_id 记忆的旧标记在库整代重建后会继续命中，补列被静默跳过、
    随后所有任务操作一起炸 ``no such column``（与 inbox 的 TEST_DSH_52_A 同形）。

    顺带修掉原来的「``except: pass`` + 无条件标记」：一次锁/IO 失败就会让补列
    在进程内永久短路。
    """
    from hiveweave.db import project as project_db

    key = await project_db.schema_marker_key_for_project(project_id)
    if key in _migrated:
        return
    pending = False
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await _execute(project_id,
                           f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" in str(exc).lower():
                continue  # 列已存在 —— 正常幂等路径
            pending = True
            log.warning("tasks_column_migration_failed",
                        column=col_name, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — 非 OperationalError 同样重试
            pending = True
            log.warning("tasks_column_migration_failed",
                        column=col_name, error=str(exc))
    if not pending:
        # #11 阶段 B 前半：**存量 VERIFY 的 kind 回填**。
        #
        # 为什么必须在这里做：阶段 B 的翻转让运行时判定改读 `kind` ⇒ 不回填
        # 会让存量 VERIFY 全部被当成普通任务，**门静默敞开且不报错**。
        #
        # ⚠ 一次性语义由 `migrate_verify_kind.CUTOVER_MS`（时间锚）保证，
        # **不是**由本函数外层的 `_migrated` 保证 —— 那个标记是 `(workspace,
        # 连接世代)` 键，每次新世代都会重跑，而回填**不能重跑**（重跑会把
        # cutover 之后新建的、标题恰好像 VERIFY 的普通任务静默升格）。
        # 回填自身幂等 ⇒ 放在这个位置是安全的。
        #
        # 失败时 `pending=True`（不落标记）⇒ 下次重试，而不是静默跳过。
        try:
            from .migrate_verify_kind import backfill_verify_kind

            _stats = await backfill_verify_kind(project_id)
            if _stats.get("backfilled"):
                log.info("verify_kind_backfilled", **_stats)
        except Exception as exc:  # noqa: BLE001 — 失败要重试，不能静默
            pending = True
            log.warning("verify_kind_backfill_failed", error=str(exc)[:200])
    if not pending:
        _migrated.add(key)


# ── Shared task_events write helper (Timeline v4 §4.1) ─────
# Single funnel for ALL task_events writes: _transition/_transition_multi,
# create_task, reassign_task, archive_task, verify_rehang, dismiss, obligation.
# Every insert is paired with a lobby WS publish so the frontend timeline
# gets an invalidation signal (WS is signal-only; REST remains the source).

_TASK_EVENT_INSERT_SQL = (
    "INSERT INTO task_events (id, project_id, task_id, event_type, "
    "from_status, to_status, actor_id, payload, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def build_task_event_insert(
    project_id: str,
    task_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    actor_id: str | None = None,
    payload: dict | str | None = None,
    now_ms: int | None = None,
) -> tuple[tuple[str, list], int, str]:
    """Build (sql, params) for a task_events INSERT + timestamp + event_id.

    Returns ``((sql, params), ts, event_id)``。event_id 是该行主键，
    供需要幂等键的调用方（如归档指引 relay）复用，避免另铸 uuid。

    Use inside a caller-managed transaction (org.py dismiss), or via
    insert_task_event() for a standalone write.
    """
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    if payload is None:
        payload_json = "{}"
    elif isinstance(payload, str):
        payload_json = payload
    else:
        payload_json = json.dumps(payload, ensure_ascii=False)
    event_id = str(uuid.uuid4())
    return (
        _TASK_EVENT_INSERT_SQL,
        [
            event_id,
            project_id,
            task_id,
            event_type,
            from_status,
            to_status,
            actor_id,
            payload_json,
            ts,
        ],
    ), ts, event_id


async def publish_task_event(
    project_id: str,
    task_id: str,
    event_type: str,
    to_status: str | None,
    ts: int,
) -> None:
    """Publish a task_event invalidation signal to the lobby channel.

    Best-effort: a failed publish must never break the write path —
    the frontend falls back to 30s polling (WS is signal-only).
    """
    try:
        from hiveweave.realtime.event_bus import status_event_bus

        await status_event_bus.publish(
            "lobby",
            {
                "type": "task_event",
                "kind": "task_event",
                "project_id": project_id,
                "task_id": task_id,
                "event_type": event_type,
                "to_status": to_status,
                "ts": ts,
            },
        )
    except Exception as e:
        log.warning(
            "task_event_publish_failed",
            task_id=task_id[:12],
            event_type=event_type,
            error=str(e),
        )


async def insert_task_event(
    project_id: str,
    task_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    actor_id: str | None = None,
    payload: dict | str | None = None,
    now_ms: int | None = None,
) -> None:
    """Standalone task_events write: INSERT + lobby publish."""
    (sql, params), ts, _event_id = build_task_event_insert(
        project_id, task_id, event_type, from_status, to_status,
        actor_id=actor_id, payload=payload, now_ms=now_ms,
    )
    await _execute(project_id, sql, params)
    await publish_task_event(project_id, task_id, event_type, to_status, ts)

