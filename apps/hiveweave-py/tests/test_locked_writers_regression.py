"""回归测试 — project_id 键控公共写 helper（db/project.py 2026-08-13 收编）。

覆盖：
1. execute_by_project 写入成功；
2. SQL 异常后连接可用（随后 execute_transaction_by_project 的
   BEGIN IMMEDIATE 正常，隐式事务已回滚释放）且异常语句数据未落库；
3. execute_transaction_by_project 中途失败整体回滚（全有或全无）。

fixture 参考 test_tx_leak_regression.py：临时 workspace + patch
meta_db.get_project_workspace 路由。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db

PROJECT_ID = "locked-writers-regression"


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


def _fake_ws_for(ws: str):
    async def fake_ws(pid: str):
        return ws if pid == PROJECT_ID else None

    return fake_ws


@pytest.mark.asyncio
async def test_execute_by_project_writes(env):
    """execute_by_project 单语句写成功落库。"""
    ws = env["workspace"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _lw (k TEXT PRIMARY KEY, v TEXT)"
    )
    await conn.commit()

    with patch("hiveweave.db.meta.get_project_workspace", _fake_ws_for(ws)):
        await project_db.execute_by_project(
            PROJECT_ID, "INSERT INTO _lw (k, v) VALUES ('a', '1')"
        )

    cursor = await conn.execute("SELECT v FROM _lw WHERE k = 'a'")
    assert (await cursor.fetchone())[0] == "1"
    await cursor.close()


@pytest.mark.asyncio
async def test_execute_by_project_failure_keeps_conn_usable(env):
    """SQL 异常后：连接可用（后续 BEGIN IMMEDIATE 正常）+ 异常数据未落库。"""
    ws = env["workspace"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _lw (k TEXT PRIMARY KEY, v TEXT)"
    )
    await conn.commit()

    with patch("hiveweave.db.meta.get_project_workspace", _fake_ws_for(ws)):
        await project_db.execute_by_project(
            PROJECT_ID, "INSERT INTO _lw (k, v) VALUES ('ok', '1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await project_db.execute_by_project(
                PROJECT_ID, "INSERT INTO _lw (k, v) VALUES ('ok', '2')"
            )

        # 修复前：失败语句的隐式事务泄漏在共享连接 → BEGIN IMMEDIATE 报
        # "cannot start a transaction within a transaction"
        await project_db.execute_transaction_by_project(
            PROJECT_ID,
            [
                ("INSERT INTO _lw (k, v) VALUES ('b', '2')", None),
                ("INSERT INTO _lw (k, v) VALUES ('c', '3')", None),
            ],
        )

    cursor = await conn.execute("SELECT v FROM _lw WHERE k = 'ok'")
    row = await cursor.fetchone()
    await cursor.close()
    # 异常的那条（'2'）未落库；此前成功的那条保留
    assert row is not None and row[0] == "1"


@pytest.mark.asyncio
async def test_execute_transaction_by_project_rolls_back(env):
    """事务中途失败 → 整体回滚，事务内语句全部不落库。"""
    ws = env["workspace"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS _lw (k TEXT PRIMARY KEY, v TEXT)"
    )
    await conn.commit()

    with patch("hiveweave.db.meta.get_project_workspace", _fake_ws_for(ws)):
        # 第二条 INSERT 携带不可绑定参数 → 事务中途失败
        with pytest.raises(sqlite3.Error):
            await project_db.execute_transaction_by_project(
                PROJECT_ID,
                [
                    ("INSERT INTO _lw (k, v) VALUES ('t1', '1')", None),
                    ("INSERT INTO _lw (k, v) VALUES ('t2', ?)", [object()]),
                ],
            )

    cursor = await conn.execute("SELECT COUNT(*) FROM _lw")
    assert (await cursor.fetchone())[0] == 0
    await cursor.close()
