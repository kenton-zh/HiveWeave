"""F11 迁移顺序缺陷回归（TEST_DSH_37 六轮审计 P0-①）。

背景：2026-08-30 44d84cd 落地的 F11 修复把
``ALTER TABLE llm_usage ADD COLUMN cold_start`` 排在了
``CREATE TABLE llm_usage`` **之前** —— PROJECT_DB_TABLES 按序执行、
建表循环对 ALTER 异常静默吞（"no such table" 被当成 "column already
exists"），新建库的 llm_usage 永远缺 cold_start 列；record_rounds 的
INSERT 全部静默失败，274 次 LLM 调用零记账（Token 页 / 命中率 / token
口径税率全盲）。

本文件锁死三件事：
1. 新库 llm_usage 原生带 cold_start，record_rounds 正常落账（含冷启动标记）。
2. 旧坏库（表存在但缺列）被 ensure_project_db 的 ALTER 自愈。
3. 自检断言 fail-loud + 记账失败落 usage_recorder_failed 告警事件
   （DSH defensive-patterns:7-9「正交结果独立上报」——best-effort
   不等于静默蒸发）。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ProjectDbError

PROJECT_ID = "f11-regress"
AGENT = "agent-f11"


@pytest.fixture
async def env():
    """临时 workspace + Meta 路由 patch（agent→project→workspace）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())

        async def fake_pid(agent_id: str):
            return PROJECT_ID if agent_id == AGENT else None

        async def fake_ws(pid: str):
            return ws if pid == PROJECT_ID else None

        with patch(
            "hiveweave.db.meta.get_agent_project_id", side_effect=fake_pid
        ), patch(
            "hiveweave.db.meta.get_project_workspace", side_effect=fake_ws
        ):
            yield {"ws": ws}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT, None)
        project_db._write_locks.pop(ws, None)


async def _table_cols(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_fresh_db_records_usage_with_cold_start(env):
    """新库：列原生存在 + record_rounds 全链路落账。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])
    assert "cold_start" in await _table_cols(conn, "llm_usage")

    rounds = [
        {"input": 100, "output": 20, "cache_read": 0,
         "cache_creation": 0, "total": 120, "duration_ms": 1500},
        {"input": 50, "output": 10, "cache_read": 80,
         "cache_creation": 0, "total": 60, "duration_ms": 900},
    ]
    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id="run-f11-1",
        rounds=rounds, model_id="m", provider="openai-responses",
        request_type="main",
    )
    cur = await conn.execute(
        "SELECT input_tokens, cold_start FROM llm_usage "
        "WHERE run_id='run-f11-1' ORDER BY created_at"
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    # 首轮 cache_read=0 且 cache_creation=0 且 input>0 → cold_start=1
    assert rows[0][0] == 100 and rows[0][1] == 1
    assert rows[1][0] == 50 and rows[1][1] == 0


@pytest.mark.asyncio
async def test_legacy_broken_db_self_heals(env):
    """旧坏库（F11 缺陷现场：表存在但缺列）→ ALTER 补列自愈。"""
    legacy = """
    CREATE TABLE llm_usage (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        project_id TEXT,
        run_id TEXT,
        task_id TEXT,
        model_id TEXT,
        request_type TEXT DEFAULT 'main',
        provider TEXT,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """
    import aiosqlite
    import os

    db_dir = Path(env["ws"]) / ".hiveweave"
    os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(str(db_dir / "data.db")) as pre:
        await pre.execute(legacy)
        await pre.commit()

    conn = await project_db.ensure_project_db(env["ws"])
    assert "cold_start" in await _table_cols(conn, "llm_usage")

    # 自愈后 record_rounds 能写进去
    from hiveweave.services.token_meter import token_meter

    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id="run-heal",
        rounds=[{"input": 7, "output": 3, "total": 10}],
        request_type="main",
    )
    cur = await conn.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE run_id='run-heal'"
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_self_check_fails_loud(env):
    """自检断言：关键列缺失必须 raise（fail-loud 而非静默断账）。"""
    with patch.object(
        project_db,
        "PROJECT_DB_COLUMN_CHECKS",
        {"llm_usage": {"cold_start", "column_that_never_exists"}},
    ):
        with pytest.raises(ProjectDbError, match="column_that_never_exists"):
            await project_db.ensure_project_db(env["ws"])


@pytest.mark.asyncio
async def test_record_rounds_failure_emits_warning_event(env):
    """记账失败 → usage_recorder_failed 告警事件落 agent_events（正交上报）。"""
    await project_db.ensure_project_db(env["ws"])
    conn = await project_db.get_project_db_for_agent(AGENT)

    async def broken_tx(agent_id, statements):
        raise RuntimeError("table llm_usage has no column named cold_start")

    from hiveweave.services.token_meter import token_meter

    with patch.object(
        project_db, "execute_transaction", side_effect=broken_tx
    ):
        await token_meter.record_rounds(
            agent_id=AGENT, project_id=PROJECT_ID, run_id="run-broken",
            rounds=[{"input": 10, "output": 5, "total": 15}],
            request_type="main",
        )

    # usage 本体没写进去（失败路径），但告警事件在
    cur = await conn.execute("SELECT COUNT(*) FROM llm_usage")
    assert (await cur.fetchone())[0] == 0
    cur = await conn.execute(
        "SELECT payload FROM agent_events "
        "WHERE event_type='usage_recorder_failed'"
    )
    events = await cur.fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload["n_rounds"] == 1
    assert payload["request_type"] == "main"
    assert "cold_start" in payload["error"]


@pytest.mark.asyncio
async def test_per_round_ts_preferred_over_bulk_stamp(env):
    """八轮 P2：record_rounds 应优先使用 streamer 逐次记录的 ts 字段，
    落库 created_at 反映真实调用时刻而非 run 结束的批量盖章时刻；
    缺 ts 的轮次回退 now（旧行为兼容）。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])
    t0, t1 = 1788150000000, 1788150060000
    rounds = [
        {"input": 100, "output": 20, "cache_read": 0,
         "cache_creation": 0, "total": 120, "duration_ms": 1500,
         "ts": t0},
        {"input": 50, "output": 10, "cache_read": 80,
         "cache_creation": 0, "total": 60, "duration_ms": 900,
         "ts": t1},
        {"input": 30, "output": 5, "cache_read": 40,
         "cache_creation": 0, "total": 35},  # 无 ts → 回退 now
    ]
    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id="run-ts-1",
        rounds=rounds, model_id="m", provider="openai-responses",
        request_type="main",
    )
    cur = await conn.execute(
        "SELECT input_tokens, created_at FROM llm_usage "
        "WHERE run_id='run-ts-1' ORDER BY created_at"
    )
    rows = await cur.fetchall()
    assert len(rows) == 3
    assert (rows[0]["input_tokens"], rows[0]["created_at"]) == (100, t0)
    assert (rows[1]["input_tokens"], rows[1]["created_at"]) == (50, t1)
    # 无 ts 的轮次回退到当下（远大于测试里的历史时刻即视为回退生效）
    assert rows[2]["input_tokens"] == 30 and rows[2]["created_at"] > t1
