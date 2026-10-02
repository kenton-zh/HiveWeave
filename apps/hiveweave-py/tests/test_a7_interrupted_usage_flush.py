"""批 2 A7 回归 — 中断 run 用量缺账（R11：interrupted run 无 usage）。

报告 §2-A7：中断路径 ``_flush_pending_usage`` 覆盖不全 ——
· 进程内：状态脱钩（PROCESSING 但无活 LLM task）时 cancel 的
  CancelledError handler 不跑，内存 sink 没人接 ⇒ interrupted 零账；
· startup_sweep / off_duty close / activate reclaim（``sweep_stale_agent_runs``
  三个调用方）：被收口 run 的 usage 在上个进程内存里已蒸发，token 本体
  不可补记 —— 实测 5 条 interrupted 里 3 条零账主要来自这类。

修复：
① ``_flush_pending_usage`` 防重入（同步摘 sink 快照，消灭双计窗口）+
  本 run 二次 flush 降 debug；
② ``Agent.cancel`` 在 await 被掐 task 之后补一次幂等 flush（脱钩兜底）；
③ ``sweep_stale_agent_runs`` 对 declared>0 且零账的收口 run 补 R11 占位
  usage 行（口径同 ``error_run``）；
④ ``close_running_runs`` 在 DB sweep 前对本进程内存 agent 预 flush。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

PROJECT_ID = "a7-proj-0001"
AGENT_ID = "a7-exec-0001"
WS = None  # 由 fixture 填


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（照 test_i8_run_lifecycle_closeout 模式）。"""
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


async def _seed_project(tmp_path):
    """Meta projects 行 + per-project DB + agent 路由注册（照 i8 模式）。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "a7-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        [AGENT_ID, "A7X", PROJECT_ID, "Agent-A7X", "executor", "model-x", now],
    )
    await cur.close()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="A7X",
            name="Agent-A7X",
            role="executor",
            status="active",
        )
    )
    await conn.commit()
    return conn, ws


async def _seed_run(
    conn,
    run_id: str,
    *,
    status: str = "running",
    actual_llm_calls: int = 0,
) -> None:
    cur = await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, activation_id, status, "
        "lease_expires_at, budget_llm_calls, budget_tool_calls, "
        "budget_elapsed_ms, actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, NULL, ?, 0, 50, 100, 600000, ?, 0, ?)",
        [run_id, AGENT_ID, status, actual_llm_calls, int(time.time() * 1000)],
    )
    await cur.close()
    await conn.commit()


async def _usage_rows(conn, run_id: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, agent_id, run_id, request_type, provider, total_tokens, "
        "duration_ms FROM llm_usage WHERE run_id = ?",
        [run_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _run_summary(conn, run_id: str) -> tuple[str, str]:
    cur = await conn.execute(
        "SELECT status, result_summary FROM agent_runs WHERE id = ?",
        [run_id],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"run {run_id} not found"
    return row["status"], row["result_summary"] or ""


# ── ③ sweep 收口 run 的 R11 占位补账 ────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_inserts_placeholder_for_declared_zero_account_run(
    tmp_path,
):
    """进程死亡收口的 run：declared>0 且零账 ⇒ interrupted + 占位 usage 行。

    占位行机检位：request_type/provider NULL + 全 0 token + duration 0；
    result_summary 前置 ``[llm_usage_placeholder]``（R11 判据同 error_run）。
    """
    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-ghost", actual_llm_calls=3)

    from hiveweave.services.run_ledger import sweep_stale_agent_runs

    assert await sweep_stale_agent_runs(ws) == 1

    status, summary = await _run_summary(conn, "a7-run-ghost")
    assert status == "interrupted"
    assert summary.startswith("[llm_usage_placeholder]"), summary
    assert "startup_sweep" in summary, "close_reason 仍要在 summary 里可读"

    rows = await _usage_rows(conn, "a7-run-ghost")
    assert len(rows) == 1, "declared>0 零账 run 必须补一条占位"
    row = rows[0]
    assert row["request_type"] is None and row["provider"] is None
    assert int(row["total_tokens"] or 0) == 0
    assert int(row["duration_ms"] or 0) == 0
    assert row["agent_id"] == AGENT_ID


@pytest.mark.asyncio
async def test_sweep_skips_placeholder_when_usage_already_recorded(tmp_path):
    """已有账的 run 不补（占位只堵「零账」断口），summary 不加标记。"""
    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-paid", actual_llm_calls=2)
    cur = await conn.execute(
        "INSERT INTO llm_usage (id, agent_id, project_id, run_id, task_id, "
        "model_id, request_type, provider, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, total_tokens, "
        "duration_ms, cold_start, creation_unreported, created_at) "
        "VALUES (?, ?, ?, ?, NULL, 'model-x', 'main', 'openai', 10, 5, 0, 0, "
        "15, 100, 0, 0, ?)",
        ["u-paid", AGENT_ID, PROJECT_ID, "a7-run-paid", int(time.time() * 1000)],
    )
    await cur.close()
    await conn.commit()

    from hiveweave.services.run_ledger import sweep_stale_agent_runs

    assert await sweep_stale_agent_runs(ws) == 1

    rows = await _usage_rows(conn, "a7-run-paid")
    assert len(rows) == 1, "已有账不得被占位双计"
    assert rows[0]["request_type"] == "main"
    _status, summary = await _run_summary(conn, "a7-run-paid")
    assert "[llm_usage_placeholder]" not in summary


@pytest.mark.asyncio
async def test_sweep_no_placeholder_for_zero_declared_run(tmp_path):
    """declared=0（一轮 LLM 都没跑完）⇒ 无账可补，不造占位（E16 老语义不变）。"""
    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-empty", actual_llm_calls=0)

    from hiveweave.services.run_ledger import sweep_stale_agent_runs

    assert await sweep_stale_agent_runs(ws) == 1
    assert await _usage_rows(conn, "a7-run-empty") == []
    _status, summary = await _run_summary(conn, "a7-run-empty")
    assert "[llm_usage_placeholder]" not in summary


@pytest.mark.asyncio
async def test_sweep_placeholder_idempotent_second_call(tmp_path):
    """二次 sweep（幂等验收）：无 running 行 ⇒ 0，占位行不重复。"""
    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-once", actual_llm_calls=1)

    from hiveweave.services.run_ledger import sweep_stale_agent_runs

    assert await sweep_stale_agent_runs(ws) == 1
    assert await sweep_stale_agent_runs(ws) == 0
    assert len(await _usage_rows(conn, "a7-run-once")) == 1


# ── ④ close_running_runs 的内存预 flush ─────────────────────────────────


def _fake_live_agent(run_id: str, rounds: list[dict], *, llm_task=None):
    """内存 sink 还挂着账的 agent 替身（recovery._flush_pending_usage 的
    全部触点：id / project_id / _pending_usage / _current_run_id /
    _run_saw_stream_chunk / _run_ledger / _get_model_config）。"""

    class _FakeAgent:
        id = AGENT_ID
        project_id = PROJECT_ID
        _pending_usage = rounds
        _current_run_id = run_id
        _current_task_id = None
        _run_saw_stream_chunk = True
        _run_ledger = AsyncMock()
        _usage_flushed_run_id = None
        _llm_task = llm_task

        async def _get_model_config(self):
            return {"model_id": "model-x", "provider_type": "openai"}

    return _FakeAgent()


@pytest.mark.asyncio
async def test_close_running_runs_flushes_inmemory_pending_usage(
    tmp_path, monkeypatch
):
    """sweep 前把内存 sink 的账落库：rounds 进 token_meter，sink 排空。"""
    from hiveweave.services import token_meter as tm
    from hiveweave.services.project_lifecycle import close_running_runs

    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-live", actual_llm_calls=1)

    rounds = [{"input": 100, "output": 50, "total": 150, "ts": 1}]
    agent = _fake_live_agent("a7-run-live", rounds)
    from hiveweave.agents.supervisor import agent_manager

    agent_manager._agents[AGENT_ID] = agent

    captured: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    try:
        assert await close_running_runs(
            PROJECT_ID,
            close_reason="a7_test: close with pending usage",
        ) == 1
    finally:
        agent_manager._agents.pop(AGENT_ID, None)

    assert len(captured) == 1, "pending rounds 必须在 sweep 前落库"
    call = captured[0]
    assert call["rounds"] == rounds
    assert call["run_id"] == "a7-run-live"
    assert call["request_type"] == "main"
    assert agent._pending_usage == [], "flush 后 sink 必须排空"
    status, _summary = await _run_summary(conn, "a7-run-live")
    assert status == "interrupted"


@pytest.mark.asyncio
async def test_close_running_runs_no_agents_is_pure_db_sweep(tmp_path):
    """启动期形态（内存无 agent）：预 flush 静默跳过，DB sweep 照常。"""
    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-orphan", actual_llm_calls=0)

    from hiveweave.services.project_lifecycle import close_running_runs

    assert await close_running_runs(PROJECT_ID) == 1
    status, _summary = await _run_summary(conn, "a7-run-orphan")
    assert status == "interrupted"


@pytest.mark.asyncio
async def test_close_running_runs_skips_live_stream_sink(tmp_path, monkeypatch):
    """活流不抢 sink：有活 LLM task 的 agent 交由其自身恢复路径 flush，
    预 flush 若在流中途摘走 sink 会孤儿化 streamer 手里的 usage_sink。"""
    from hiveweave.services import token_meter as tm
    from hiveweave.services.project_lifecycle import close_running_runs

    conn, ws = await _seed_project(tmp_path)
    await _seed_run(conn, "a7-run-live2", actual_llm_calls=0)

    async def _hang() -> None:
        await asyncio.sleep(3600)

    rounds = [{"input": 1, "output": 1, "total": 2, "ts": 1}]
    agent = _fake_live_agent(
        "a7-run-live2", rounds, llm_task=asyncio.ensure_future(_hang())
    )
    from hiveweave.agents.supervisor import agent_manager

    agent_manager._agents[AGENT_ID] = agent

    calls: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    try:
        assert await close_running_runs(PROJECT_ID) == 1
    finally:
        agent._llm_task.cancel()
        agent_manager._agents.pop(AGENT_ID, None)

    assert calls == [], "活流的 sink 不得被预 flush 摘走"
    assert agent._pending_usage == rounds


# ── ① _flush_pending_usage 幂等 / 防重入 ────────────────────────────────


@pytest.mark.asyncio
async def test_flush_pending_usage_double_call_no_double_write(monkeypatch):
    """同一批 rounds 第二次 flush 不得双写；二次调用降级为 no-op。"""
    from hiveweave.agents import recovery
    from hiveweave.services import token_meter as tm

    rounds = [{"input": 10, "output": 5, "total": 15, "ts": 1}]
    agent = _fake_live_agent("r-a7", rounds)

    calls: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    await recovery._flush_pending_usage(agent, reason="cancelled_by_user")
    await recovery._flush_pending_usage(agent, reason="cancelled_by_user")

    assert len(calls) == 1, "同一批 rounds 只许落库一次"
    assert calls[0]["rounds"] == rounds
    assert agent._pending_usage == []
    # 二次 flush 走 dedup 早退：不得标 empty_stream（chunk 明明到过）
    agent._run_ledger.set_run_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_pending_usage_marks_empty_stream_once(monkeypatch):
    """真·零 chunk run：首次空 flush 标 empty_stream=1，二次 flush 不重复标。"""
    from hiveweave.agents import recovery
    from hiveweave.services import token_meter as tm

    agent = _fake_live_agent("r-a7-empty", [])
    agent._run_saw_stream_chunk = False

    async def _fail_record_rounds(**kwargs):
        raise AssertionError("空 sink 不应触达 record_rounds")

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fail_record_rounds)

    await recovery._flush_pending_usage(agent, reason="safety_timeout")
    await recovery._flush_pending_usage(agent, reason="safety_timeout")

    agent._run_ledger.set_run_fact.assert_awaited_once_with(
        AGENT_ID, "r-a7-empty", empty_stream=1
    )


# ── ② Agent.cancel 的状态脱钩补账兜底 ───────────────────────────────────


def _make_desynced_agent(pending_rounds: list[dict]) -> object:
    """PROCESSING 但无活 LLM task（CancelledError handler 不会跑）的 agent。

    照 test_cancel_preserves_inbox._make_agent 的 __new__ 模式裁剪。
    """
    from hiveweave.agents.agent import Agent, AgentState

    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "A7", "role": "executor"}
    agent.status = AgentState.PROCESSING
    agent.empty_retry_count = 0
    agent.pending_inbox_msg_ids = None
    agent.current_job = None
    agent._cancel_reason = None
    agent._message_queue = []
    agent._streaming_msg_id = None
    agent._resume_cooldown_until = 0.0
    agent._consecutive_errors = 0
    agent._CONSECUTIVE_ERROR_MAX = 3
    agent._resume_suppressed = False
    agent._pending_resume_hint = None
    agent.disposition = "waiting_agent"
    agent.visibility = "background"
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._slice_budget = 0
    agent._SLICE_BUDGET_MAX = 2
    agent._progress_fingerprint = None
    agent._no_progress_streak = 0
    agent._empty_done_slice_streak = 0
    agent._MERGE_WINDOW_MS = 300
    agent._workspace_path = None
    agent._current_run_id = "r-desync"
    agent._current_task_id = None
    agent._pending_usage = pending_rounds
    agent._run_saw_stream_chunk = True
    agent._usage_flushed_run_id = None
    agent._safety_timer = None
    agent._on_status_change = None
    agent._on_stream_event = None
    agent._stop_watcher = False
    agent._inbox_watcher_task = None
    agent._llm_task = None  # 脱钩点：无活 task ⇒ handler 不跑
    agent._lock = asyncio.Lock()
    agent._conversation = AsyncMock()
    agent._inbox = AsyncMock()
    agent._org = AsyncMock()
    agent._chat_msg = AsyncMock()
    agent._work_log = AsyncMock()
    agent._run_ledger = AsyncMock()
    agent._finalize_streaming_turn = AsyncMock(return_value=True)
    agent._cancel_safety_timer = MagicMock()
    agent._cancel_productive_continue = MagicMock()
    agent._cancel_interrupted_resume = MagicMock()
    agent._ensure_watcher_alive = MagicMock()
    agent._broadcast_status = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_cancel_flushes_pending_usage_when_no_live_task(monkeypatch):
    """脱钩 cancel：handler 不跑 ⇒ cancel 自己兜底 flush（A7 补的洞）。"""
    from hiveweave.agents.agent import Agent, AgentState
    from hiveweave.services import token_meter as tm

    rounds = [{"input": 7, "output": 3, "total": 10, "ts": 1}]
    agent = _make_desynced_agent(rounds)

    calls: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    await agent.cancel(reason="cancelled")

    assert len(calls) == 1, "脱钩 cancel 必须在实例被丢弃前交接 pending 账"
    assert calls[0]["rounds"] == rounds
    assert calls[0]["run_id"] == "r-desync"
    assert agent._pending_usage == []
    assert agent.status == AgentState.IDLE


@pytest.mark.asyncio
async def test_cancel_with_live_task_does_not_double_flush(monkeypatch):
    """有活 task 时 handler 已 flush ⇒ cancel 的兜底 flush 是 no-op（不双写）。"""
    from hiveweave.services import token_meter as tm

    agent = _make_desynced_agent([])  # handler 侧已把 sink 排空的形态
    agent._llm_task = None
    agent._usage_flushed_run_id = "r-desync"  # handler 已 flush 过本 run

    calls: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    await agent.cancel(reason="cancelled")

    assert calls == [], "handler 已 flush 过 ⇒ 兜底不得再写"


@pytest.mark.asyncio
async def test_cancel_skips_flush_when_new_stream_took_over(monkeypatch):
    """等锁窗口新流接管：兜底 flush 不得抢摘新流的 sink（防孤儿化丢账）。

    形态：有活 task（正常取消路径）→ handler 处理期间另一个 chat() 已把
    ``_llm_task`` 换成新活任务 ⇒ cancel await 完旧 task 后，兜底 flush 必须
    让位（新流的账归新流自己的恢复路径）。
    """
    from hiveweave.agents.agent import AgentState
    from hiveweave.services import token_meter as tm

    rounds = [{"input": 9, "output": 1, "total": 10, "ts": 1}]
    agent = _make_desynced_agent(rounds)

    async def _hang() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await agent._handle_cancel()

    async def _handler_swaps_in_new_stream() -> None:
        # 模拟等锁窗口里新 chat() 起了新流：_llm_task 被换成活任务。
        agent.status = AgentState.IDLE
        agent._cancel_reason = None
        agent._llm_task = new_task

    old_task = asyncio.ensure_future(_hang())
    agent._llm_task = old_task
    agent._handle_cancel = AsyncMock(side_effect=_handler_swaps_in_new_stream)
    new_task = asyncio.ensure_future(_hang())

    calls: list[dict] = []

    async def _fake_record_rounds(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tm.token_meter, "record_rounds", _fake_record_rounds)

    await asyncio.sleep(0)  # 让挂起任务先到达 await 点
    try:
        await agent.cancel(reason="cancelled")
    finally:
        new_task.cancel()

    agent._handle_cancel.assert_awaited()
    assert calls == [], "新流接管后兜底 flush 必须让位"
    assert agent._pending_usage == rounds, "新流的 sink 不得被摘走"
