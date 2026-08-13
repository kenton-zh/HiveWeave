"""Locked writers Part 2 — 无锁写点改走 per-workspace 写锁纪律。

覆盖：
- wait_contract.replace_waits 事务性（中途失败 → 旧 wait 不被部分替换）
- roster.update_roster DELETE+INSERT 两语句原子性
- obligation / dispatch / work_log 的 execute_by_project 委托基本可用

fixture 参考 test_idle_architecture_p0.task_env / test_roster_seed_on_hire.org_env：
patch meta_db.get_project_workspace → 真实 tmp per-project SQLite（schema 由
ensure_project_db 自动建表）。
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import dispatch as dispatch_mod
from hiveweave.services.obligation import ObligationLedger
from hiveweave.services.roster import RosterService
from hiveweave.services.wait_contract import WaitContractService
from hiveweave.services.work_log import WorkLogService

PROJECT_ID = "test-locked-writers-p2"


@pytest.fixture
async def task_env(tmp_path):
    ws = str(tmp_path.resolve())

    async def fake_ws(pid: str):
        return ws if pid == PROJECT_ID else None

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        yield {"project_id": PROJECT_ID, "workspace": ws}

    async with project_db._ensure_lock:
        conn = project_db._cache.pop(ws, None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            pass


# ── wait_contract.replace_waits 事务性 ───────────────────────


@pytest.mark.asyncio
async def test_replace_waits_rolls_back_on_failure(task_env):
    """中途失败 → 旧 wait 不被部分替换（UPDATE 与 INSERT 全有或全无）。

    第二条 INSERT 携带不可绑定参数（object()），使事务在
    "旧 wait 已 UPDATE cleared_at + 第一条 INSERT 已执行"之后失败；
    回滚后旧 wait 必须仍处于活跃状态。
    """
    pid = task_env["project_id"]
    svc = WaitContractService()

    # 先成功写入一条旧 wait（happy path 顺带覆盖）
    created = await svc.replace_waits(
        pid, "agent-a", [{"kind": "agent", "ref": "old-ref"}], phase="waiting"
    )
    assert len(created) == 1
    active = await svc.list_active(pid, "agent-a")
    assert len(active) == 1
    assert active[0]["ref"] == "old-ref"

    # 第二条 INSERT 的 note 参数不可绑定 → 事务中途失败
    with pytest.raises(sqlite3.Error):
        await svc.replace_waits(
            pid,
            "agent-a",
            [
                {"kind": "agent", "ref": "new-ref-ok"},
                {"kind": "agent", "ref": "bad-ref", "note": object()},
            ],
            phase="waiting",
        )

    # 全有或全无：旧 wait 仍活跃，且没有任何半插入的新行
    active_after = await svc.list_active(pid, "agent-a")
    assert len(active_after) == 1
    assert active_after[0]["ref"] == "old-ref"


@pytest.mark.asyncio
async def test_replace_waits_success_clears_and_inserts(task_env):
    """正常路径：旧 wait 全部清除 + 新 wait 全部插入（单事务提交）。"""
    pid = task_env["project_id"]
    svc = WaitContractService()

    await svc.replace_waits(
        pid, "agent-a", [{"kind": "agent", "ref": "r1"}], phase="waiting"
    )
    created = await svc.replace_waits(
        pid,
        "agent-a",
        [{"kind": "agent", "ref": "r2"}, {"kind": "task", "ref": "t1"}],
        phase="working",
    )

    assert len(created) == 2
    active = await svc.list_active(pid, "agent-a")
    assert {w["ref"] for w in active} == {"r2", "t1"}
    assert all(w["phase"] == "working" for w in active)


# ── roster.update_roster 两语句原子性 ────────────────────────


@pytest.mark.asyncio
async def test_update_roster_atomic_on_insert_failure(task_env):
    """INSERT 失败时 DELETE 必须回滚 — 原人事记录不能被清掉。"""
    pid = task_env["project_id"]
    rs = RosterService()

    await rs.update_roster(pid, "agent-a", {"position": "Old"})
    rec = await rs.get(pid, "agent-a")
    assert rec is not None and rec["position"] == "Old"

    # position 为 dict → INSERT 绑定失败 → DELETE+INSERT 整体回滚
    with pytest.raises(sqlite3.Error):
        await rs.update_roster(pid, "agent-a", {"position": {"bad": "bind"}})

    rec_after = await rs.get(pid, "agent-a")
    assert rec_after is not None, "DELETE 不应已提交 — 原记录必须仍在"
    assert rec_after["position"] == "Old"


@pytest.mark.asyncio
async def test_update_roster_upsert_still_works(task_env):
    """正常 upsert：重复 update_roster 覆盖同一行（DELETE+INSERT 语义不变）。"""
    pid = task_env["project_id"]
    rs = RosterService()

    await rs.update_roster(pid, "agent-a", {"position": "First"})
    await rs.update_roster(pid, "agent-a", {"position": "Second"})

    rec = await rs.get(pid, "agent-a")
    assert rec is not None
    assert rec["position"] == "Second"


# ── execute_by_project 委托基本可用 ──────────────────────────


@pytest.mark.asyncio
async def test_obligation_create_via_locked_write(task_env):
    pid = task_env["project_id"]
    ledger = ObligationLedger()

    ob_id = await ledger.create(pid, "owner-a", "merge", task_id=None)
    assert ob_id

    conn = await project_db.get_project_db_by_project_id(pid)
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM obligations WHERE id = ?", [ob_id]
    )
    row = await cur.fetchone()
    await cur.close()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_dispatch_execute_via_locked_write(task_env):
    pid = task_env["project_id"]
    await dispatch_mod._execute(
        pid,
        "INSERT INTO work_logs (id, agent_id, project_id, session_id, type, "
        "summary, details, created_at) VALUES (?, ?, ?, NULL, 'discussion', ?, '{}', ?)",
        ["wlog-dispatch-1", "agent-d", pid, "hello via dispatch", 1700000000000],
    )

    conn = await project_db.get_project_db_by_project_id(pid)
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM work_logs WHERE id = 'wlog-dispatch-1'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_work_log_append_via_locked_write(task_env):
    pid = task_env["project_id"]
    svc = WorkLogService()

    log_id = await svc.append_log(pid, "agent-w", "decision", "hello world")
    logs = await svc.get_recent(pid, "agent-w", limit=5)

    assert any(l["id"] == log_id for l in logs)
    assert any(l["type"] == "decision" and l["summary"] == "hello world"
               for l in logs)
