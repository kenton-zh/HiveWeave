"""回归测试 — 共享连接 DML 失败后的隐式事务泄漏.

根因（已审计）：per-project DB 共享连接为 aiosqlite legacy 隔离模式
（isolation_level=""），DML 隐式 BEGIN。db/project.execute() 与
services/tasks/db._execute() 在语句抛错时无 rollback → 隐式事务残留在
共享连接上 → 下一个显式 BEGIN IMMEDIATE（execute_transaction/_execute_tx）
报 "cannot start a transaction within a transaction"。实测 slack-clone_03
发生 2 次。

修复后两个入口均在异常路径 rollback（rollback 自身异常吞掉）后 re-raise，
与 execute_transaction/_execute_tx 既有的 except→rollback→raise 模式一致。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.tasks import db as tasks_db

PROJECT_ID = "tx-leak-regression"
AGENT_ID = "tx-leak-agent"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())
        yield {"workspace": ws}
        # 先关缓存连接再让 TemporaryDirectory 清理 — 否则 Windows 上
        # data.db 仍被 aiosqlite 占用，rmtree 抛 PermissionError（WinError 32）
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_execute_failure_does_not_leak_tx_on_shared_conn(env):
    """execute() 违反约束抛错后，同一连接上 execute_transaction 仍可 BEGIN IMMEDIATE."""
    ws = env["workspace"]
    conn = await project_db.ensure_project_db(ws)
    assert conn.isolation_level == ""  # legacy 隐式 BEGIN 前提（修复所依赖的事实）

    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _txleak (k TEXT PRIMARY KEY, v TEXT)"
    )
    await conn.commit()

    # agent 映射到该 workspace（真实运行 get_project_db_for_agent 会填充）
    project_db._agent_cache[AGENT_ID] = ws

    await project_db.execute(
        AGENT_ID, "INSERT INTO _txleak (k, v) VALUES ('a', '1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await project_db.execute(
            AGENT_ID, "INSERT INTO _txleak (k, v) VALUES ('a', '2')"
        )

    # 修复前：泄漏的隐式事务使 BEGIN IMMEDIATE 抛
    # "cannot start a transaction within a transaction"
    await project_db.execute_transaction(
        AGENT_ID,
        [
            ("INSERT INTO _txleak (k, v) VALUES ('b', '2')", None),
            ("INSERT INTO _txleak (k, v) VALUES ('c', '3')", None),
        ],
    )

    cursor = await conn.execute("SELECT COUNT(*) FROM _txleak")
    assert (await cursor.fetchone())[0] == 3
    await cursor.close()


@pytest.mark.asyncio
async def test_tasks_execute_failure_does_not_leak_tx_on_shared_conn(env):
    """tasks._execute() 违反约束抛错后，同一连接上 _execute_tx 仍可 BEGIN IMMEDIATE."""
    ws = env["workspace"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _txleak_tasks (k TEXT PRIMARY KEY, v TEXT)"
    )
    await conn.commit()

    async def fake_ws(pid: str):
        return ws if pid == PROJECT_ID else None

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        await tasks_db._execute(
            PROJECT_ID, "INSERT INTO _txleak_tasks (k, v) VALUES ('a', '1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await tasks_db._execute(
                PROJECT_ID, "INSERT INTO _txleak_tasks (k, v) VALUES ('a', '2')"
            )

        await tasks_db._execute_tx(
            PROJECT_ID,
            [
                ("INSERT INTO _txleak_tasks (k, v) VALUES ('b', '2')", []),
                ("INSERT INTO _txleak_tasks (k, v) VALUES ('c', '3')", []),
            ],
        )

    cursor = await conn.execute("SELECT COUNT(*) FROM _txleak_tasks")
    assert (await cursor.fetchone())[0] == 3
    await cursor.close()
