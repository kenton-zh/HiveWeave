"""批23（5-6）：failover 切换后 usage 的 model_id 记当轮实际模型。

实锤：``llm_failover_switched``（longcat→space-bunny）后该 run 的
llm_usage 行 model_id 仍记 longcat —— 批量记账的 model_id 来自 agent 的
主 model_config，而切换发生在 streamer 的 ``_stream_single_round`` 内部。

修法（最小切片）：streamer 在 usage sink 出口按 round 盖当轮实际模型
（``ToolLoopMixin._stamp_round_model``，只在已切换时盖）；
``token_meter.record_rounds`` 按行优先取轮上模型（缺省回退批量值 ⇒
未切换 run 落库逐字节不变）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.llm.failover import RunFailover
from hiveweave.llm.streamer.tool_loop import ToolLoopMixin
from hiveweave.services.token_meter import token_meter

PROJECT_ID = "failover-model"
AGENT = "agent-failover-model"


@pytest.fixture
async def env():
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


async def _model_ids(conn, run_id: str) -> list[str | None]:
    cur = await conn.execute(
        "SELECT model_id FROM llm_usage WHERE run_id = ? ORDER BY rowid",
        (run_id,),
    )
    return [r[0] for r in await cur.fetchall()]


def _round(input_: int, **extra) -> dict:
    base = {
        "input": input_,
        "output": 10,
        "cache_read": 0,
        "cache_creation": 0,
        "total": input_ + 10,
        "ts": 1_700_000_000_000,
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_switched_round_records_new_model(env):
    """① 切换后新 round 的 usage 记新模型；同批未带键轮记批量主模型。"""
    conn = await project_db.ensure_project_db(env["ws"])
    await token_meter.record_rounds(
        agent_id=AGENT,
        project_id=PROJECT_ID,
        run_id="run-switched",
        rounds=[
            # 切换前轮（streamer 未盖键）→ 批量主模型
            _round(100),
            # 切换后轮（streamer 盖了当轮实际模型）
            _round(200, model_id="space-bunny"),
        ],
        model_id="longcat",
        provider="openai-compatible",
        request_type="main",
    )
    ids = await _model_ids(conn, "run-switched")
    assert ids == ["longcat", "space-bunny"], (
        "failover 切换后该轮 usage 必须记当轮实际模型（round 级事实），"
        "切换前的轮保持主模型"
    )


@pytest.mark.asyncio
async def test_unswitched_run_rows_unchanged(env):
    """② 未切换 run：轮上无 model_id 键 ⇒ 全部记批量主模型（逐字节不变）。"""
    conn = await project_db.ensure_project_db(env["ws"])
    await token_meter.record_rounds(
        agent_id=AGENT,
        project_id=PROJECT_ID,
        run_id="run-plain",
        rounds=[_round(10), _round(20)],
        model_id="longcat",
        provider="openai-compatible",
        request_type="main",
    )
    ids = await _model_ids(conn, "run-plain")
    assert ids == ["longcat", "longcat"]


# ── streamer 侧盖戳（_stamp_round_model）──────────────────────────


def test_stamp_round_model_when_failover_active():
    """已切换（active_config 非空）⇒ 轮 dict 盖上切换后模型。"""
    mixin = ToolLoopMixin()
    mixin._failover = RunFailover()
    mixin._failover.active_config = {"model_id": "space-bunny"}
    usage: dict = {"input": 1, "output": 1}
    mixin._stamp_round_model(usage)
    assert usage["model_id"] == "space-bunny"


def test_stamp_round_model_noop_when_not_switched():
    """未切换（active_config 空）⇒ 不盖键（落库逐字节不变的根基）。"""
    mixin = ToolLoopMixin()
    mixin._failover = RunFailover()
    usage: dict = {"input": 1, "output": 1}
    mixin._stamp_round_model(usage)
    assert "model_id" not in usage


def test_stamp_round_model_noop_without_failover_state():
    """状态机缺失（裸 mixin / 旧实例）⇒ 不盖、不炸。"""
    mixin = ToolLoopMixin()
    usage: dict = {"input": 1, "output": 1}
    mixin._stamp_round_model(usage)
    assert "model_id" not in usage


def test_stamp_round_model_noop_on_empty_model_id():
    """切换配置缺 model_id ⇒ 不盖（宁可缺省不写空串）。"""
    mixin = ToolLoopMixin()
    mixin._failover = RunFailover()
    mixin._failover.active_config = {"model_id": ""}
    usage: dict = {"input": 1, "output": 1}
    mixin._stamp_round_model(usage)
    assert "model_id" not in usage
