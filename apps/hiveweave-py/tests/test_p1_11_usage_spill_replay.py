"""P1-11 回归：llm_usage 记账写失败不得永久丢（spill sidecar + 回放补录）。

实测事故：usage_recorder_failed（disk I/O error / closed database）各 1 次，
R11 run 级断账 3+4 条 —— 重试层（``_execute_write_with_retry``）只覆盖
lock/busy，非 lock 类写失败即永久丢。

本文件锁死五个单元面：
1. 写库抛 OSError（disk I/O）⇒ 待写行落 ``<ws>/.hiveweave/llm_usage_spill.jsonl``；
2. DB 恢复后下一次 ``record_rounds`` 入口先回放 ⇒ 行数闭合（回放行 + 新行），
   回放成功后 sidecar 删除；
3. sidecar 损坏行（坏 JSON）跳过不炸，好行照常补录；
4. lock 类错误（database is locked）**不** spill（重试层已兜底，避免锁风暴刷盘）；
5. sidecar 行数上限防膨胀：超限丢最旧。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db

PROJECT_ID = "p111"
AGENT = "agent-p111"


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
            yield {"ws": ws, "sidecar": Path(ws) / ".hiveweave" / "llm_usage_spill.jsonl"}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT, None)
        project_db._write_locks.pop(ws, None)


def _spill_lines(sidecar: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in sidecar.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


async def _spill_via_broken_write(run_id: str) -> None:
    """制造一次非 lock 写失败（disk I/O），让 record_rounds 自行落 sidecar。"""
    from hiveweave.services.token_meter import token_meter

    async def broken_tx(agent_id, statements):
        raise OSError("disk I/O error")

    with patch.object(project_db, "execute_transaction", side_effect=broken_tx):
        await token_meter.record_rounds(
            agent_id=AGENT, project_id=PROJECT_ID, run_id=run_id,
            rounds=[{"input": 10, "output": 5, "total": 15}],
            request_type="main",
        )


@pytest.mark.asyncio
async def test_write_failure_spills_rows_to_sidecar(env):
    """① mock 写库抛 OSError ⇒ 行落 sidecar（usage 本体 0 行）。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])
    await _spill_via_broken_write("run-spill-1")

    sidecar = env["sidecar"]
    assert sidecar.exists()
    recs = _spill_lines(sidecar)
    assert len(recs) == 1
    assert recs[0]["kind"] == "rounds"
    # params 忠实保真：18 列，agent/project/run 都在
    params = recs[0]["params"]
    assert len(params) == 18
    assert params[1] == AGENT and params[2] == PROJECT_ID
    assert params[3] == "run-spill-1"

    cur = await conn.execute("SELECT COUNT(*) FROM llm_usage")
    assert (await cur.fetchone())[0] == 0  # 写失败 = 本体没进去


@pytest.mark.asyncio
async def test_replay_closes_ledger_after_recovery(env):
    """② 恢复后下一次 record 入口先回放 ⇒ 行数闭合 + sidecar 删除。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])
    await _spill_via_broken_write("run-broken")
    assert env["sidecar"].exists()

    # 不再 patch —— DB「恢复」，下一次 record 应先回放再写新行
    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id="run-after",
        rounds=[{"input": 1, "output": 1, "total": 2}],
        request_type="main",
    )

    cur = await conn.execute("SELECT run_id FROM llm_usage ORDER BY created_at")
    run_ids = [r[0] for r in await cur.fetchall()]
    assert run_ids == ["run-broken", "run-after"]  # 回放行在前 + 新行 = 闭合
    assert not env["sidecar"].exists()  # 回放成功后删除


@pytest.mark.asyncio
async def test_corrupt_lines_skipped_not_crash(env):
    """③ sidecar 坏 JSON 行跳过不炸，好行照常补录。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])
    await _spill_via_broken_write("run-good")

    # 追加一行坏 JSON + 一行 kind 不认识的记录 —— 都不可补录
    with open(env["sidecar"], "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write(json.dumps({"v": 1, "kind": "unknown_kind", "params": []}) + "\n")

    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id="run-after-corrupt",
        rounds=[{"input": 2, "output": 2, "total": 4}],
        request_type="main",
    )

    cur = await conn.execute("SELECT run_id FROM llm_usage ORDER BY created_at")
    run_ids = [r[0] for r in await cur.fetchall()]
    # 好行回放 + 新行；坏行被跳过（不炸、不阻塞）
    assert run_ids == ["run-good", "run-after-corrupt"]
    assert not env["sidecar"].exists()  # 坏行随成功回放一并清除


@pytest.mark.asyncio
async def test_lock_error_does_not_spill(env):
    """lock 类错误不 spill（重试层/busy_timeout 兜底，避免锁风暴刷盘）。"""
    from hiveweave.services.token_meter import token_meter

    await project_db.ensure_project_db(env["ws"])

    async def locked_tx(agent_id, statements):
        raise sqlite3.OperationalError("database is locked")

    with patch.object(project_db, "execute_transaction", side_effect=locked_tx):
        await token_meter.record_rounds(
            agent_id=AGENT, project_id=PROJECT_ID, run_id="run-locked",
            rounds=[{"input": 3, "output": 3, "total": 6}],
            request_type="main",
        )

    assert not env["sidecar"].exists()


@pytest.mark.asyncio
async def test_compaction_failure_spills_and_replays(env):
    """压缩记账写失败同样落 sidecar；record_rounds 入口回放补录。"""
    from hiveweave.services.token_meter import token_meter

    conn = await project_db.ensure_project_db(env["ws"])

    async def broken_execute(agent_id, sql, params=None):
        raise OSError("closed database")

    with patch.object(project_db, "execute", side_effect=broken_execute):
        await token_meter.record_compaction(
            agent_id=AGENT, model_id="m", input_tokens=100, output_tokens=20,
            kind="conversation",
        )

    recs = _spill_lines(env["sidecar"])
    assert len(recs) == 1 and recs[0]["kind"] == "compaction"
    assert len(recs[0]["params"]) == 14

    # 恢复后经 record_rounds 入口回放
    await token_meter.record_rounds(
        agent_id=AGENT, project_id=PROJECT_ID, run_id=None,
        rounds=[{"input": 1, "output": 1, "total": 2}],
        request_type="main",
    )
    cur = await conn.execute(
        "SELECT request_type, input_tokens FROM llm_usage "
        "ORDER BY created_at"
    )
    rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("compaction_conversation", 100),  # 回放的压缩行
        ("main", 1),                       # 新行
    ]
    assert not env["sidecar"].exists()


@pytest.mark.asyncio
async def test_spill_cap_drops_oldest(env, monkeypatch):
    """sidecar 行数上限防膨胀：超限丢最旧（FIFO），保留最新。"""
    from hiveweave.services import token_meter as tm

    monkeypatch.setattr(tm, "USAGE_SPILL_MAX_LINES", 5)
    rows = [[f"id-{i}", AGENT, PROJECT_ID] for i in range(4)]
    tm._spill_usage_rows(env["ws"], "rounds", rows)          # 4 行
    tm._spill_usage_rows(env["ws"], "rounds", [["id-x", AGENT, PROJECT_ID]])  # +1 = 5
    tm._spill_usage_rows(env["ws"], "rounds", [["id-y", AGENT, PROJECT_ID]])  # +1 = 6 → trim

    lines = _spill_lines(env["sidecar"])
    assert len(lines) == 5  # 封顶
    first_ids = [ln["params"][0] for ln in lines]
    assert first_ids[0] == "id-1"  # 最旧一行（id-0）被丢
    assert first_ids[-1] == "id-y"  # 最新保留
