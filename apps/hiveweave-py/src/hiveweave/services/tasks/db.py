"""Task ledger DB helpers."""
from __future__ import annotations

import json
import time
import uuid

import aiosqlite
import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db.project import (
    ProjectDbError,
    ensure_project_db,
    get_workspace_write_lock,
)

from .constants import _MISSING_COLUMNS

log = structlog.get_logger(__name__)

_migrated: set[str] = set()

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
        try:
            await conn.execute(sql, params or [])
            await conn.commit()
        except Exception:
            # 语句失败时隐式事务（legacy isolation_level="" 的 DML 隐式 BEGIN）
            # 残留在共享连接 → 后续 BEGIN IMMEDIATE 报
            # "cannot start a transaction within a transaction"（slack-clone_03 事故）。
            # 回滚释放（rollback 自身异常吞掉）后 re-raise，同 _execute_tx。
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


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

