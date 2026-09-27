"""I8 (批7) 回归测试 — run 生命周期收尾（stop 收尾 + activate 先回收再开 + 无双 run）。

病灶（s3-clone_13 实测）：``agent.cancel``（off_duty 下班）不写 run ledger ——
8 个 run 停在 ``status='running'``、``ended_at`` 全 NULL；平台恢复后 activate
又给同一批 agent 起新 run ⇒ 同 agent 并发双 run。

修复 = 复用统一判定源 ``sweep_stale_agent_runs``（running→interrupted + 孤儿
run_step 按 started 事实位分流，**不新写清扫**）：
  ① 停止收尾：``stop_project_cleanly`` 在 agent 全部 cancel 之后收尾在跑 run；
  ② 恢复回收：activate 路径**先**回收孤儿 run **再** ``start_project_agents``；
  ③ 无双 run：回收后开新 run，同 agent running 行数恒为 1。

验收判据（fixplan §四 I8，可机检）：
  ① 项目停止后 ``agent_runs where status='running'`` 归零（直接调用验证）；
  ② 恢复后先回收孤儿再开新 run；
  ③ 无双 run（同 agent 并发）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.project_lifecycle import (
    close_running_runs,
    stop_project_cleanly,
)

PROJECT_ID = "i8-proj-0001"
AGENT_A = "i8-ceo-0001"
AGENT_B = "i8-exec-0001"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    """meta DB 钉到本用例临时路径（conftest 隔离之上的显式 init，照
    test_batch_d_fact_positions 的真实 per-project DB 模式）。"""
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture
async def seeded_project(tmp_path):
    """Meta projects 行 + per-project DB（2 个 active agent 行）+ 内存路由。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "i8-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    for aid, short, role in ((AGENT_A, "I8A", "ceo"), (AGENT_B, "I8B", "executor")):
        cur = await conn.execute(
            "INSERT INTO agents (id, short_id, project_id, name, role, status, "
            "model_id, created_at) VALUES (?, ?, ?, ?, ?, 'active', 'gpt-x', ?)",
            [aid, short, PROJECT_ID, f"Agent-{short}", role, now],
        )
        await cur.close()
        # RunLedger（create_run 等）按 agent_id 走 AgentRouter 内存注册表解析
        # workspace —— 仅落 DB 行不够，必须同时注册路由（照 batch_d 模式）。
        agent_router.register(
            AgentRoute(
                agent_id=aid,
                project_id=PROJECT_ID,
                workspace_path=ws,
                short_id=short,
                name=f"Agent-{short}",
                role=role,
                status="active",
            )
        )
    await conn.commit()
    yield conn
    agent_router.reset_for_tests()


async def _seed_run(
    conn,
    run_id: str,
    agent_id: str,
    status: str = "running",
) -> None:
    cur = await conn.execute(
        "INSERT INTO agent_runs (id, agent_id, activation_id, status, "
        "lease_expires_at, budget_llm_calls, budget_tool_calls, "
        "budget_elapsed_ms, actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, NULL, ?, 0, 50, 100, 600000, 0, 0, ?)",
        [run_id, agent_id, status, int(time.time() * 1000)],
    )
    await cur.close()
    await conn.commit()


async def _seed_running_step(
    conn,
    step_id: str,
    run_id: str,
    started: int | None,
) -> None:
    """started 事实位三态：1=执行中被掐 / 0=从未派发 / NULL=存量不可判。"""
    cur = await conn.execute(
        "INSERT INTO run_steps (id, run_id, step_index, step_type, status, "
        "started_at, started) VALUES (?, ?, 0, 'tool_call', 'running', ?, ?)",
        [step_id, run_id, int(time.time() * 1000), started],
    )
    await cur.close()
    await conn.commit()


async def _running_count(conn, agent_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM agent_runs WHERE status = 'running'"
    params: list = []
    if agent_id is not None:
        sql += " AND agent_id = ?"
        params.append(agent_id)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return int(row[0])


async def _run_row(conn, run_id: str) -> dict:
    cur = await conn.execute(
        "SELECT id, agent_id, status, ended_at, error_reason FROM agent_runs "
        "WHERE id = ?",
        [run_id],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"run {run_id} not found"
    return {
        "id": row[0],
        "agent_id": row[1],
        "status": row[2],
        "ended_at": row[3],
        "error_reason": row[4],
    }


async def _step_row(conn, step_id: str) -> dict:
    cur = await conn.execute(
        "SELECT id, status, ended_at, outcome_unknown, not_started FROM run_steps "
        "WHERE id = ?",
        [step_id],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"step {step_id} not found"
    return {
        "id": row[0],
        "status": row[1],
        "ended_at": row[2],
        "outcome_unknown": row[3],
        "not_started": row[4],
    }


# ── 验收 ①：项目停止后 running 归零（直接调用 stop_project_cleanly）──────


@pytest.mark.asyncio
async def test_stop_project_cleanly_reaps_running_runs(seeded_project):
    """下班收尾：4 个在跑 run 全部 interrupted + ended_at 落库 + 孤儿 step 分流。"""
    for i, (rid, aid) in enumerate(
        [
            ("i8-run-a1", AGENT_A),
            ("i8-run-a2", AGENT_A),
            ("i8-run-b1", AGENT_B),
            ("i8-run-b2", AGENT_B),
        ]
    ):
        await _seed_run(seeded_project, rid, aid)
        # 每 run 挂一个在跑 step：奇数 started=1（执行中），偶数 started=0（未派发）
        await _seed_running_step(seeded_project, f"i8-step-{i}", rid, started=i % 2)
    # 正常完成的 run 不得被波及
    await _seed_run(seeded_project, "i8-run-done", AGENT_A, status="completed")

    result = await stop_project_cleanly(PROJECT_ID)

    assert result["runs_interrupted"] == 4, result
    assert await _running_count(seeded_project) == 0
    for rid in ("i8-run-a1", "i8-run-a2", "i8-run-b1", "i8-run-b2"):
        row = await _run_row(seeded_project, rid)
        assert row["status"] == "interrupted", (rid, row)
        assert row["ended_at"] is not None, (rid, row)
        assert row["error_reason"].startswith("off_duty_close"), (rid, row)
    done = await _run_row(seeded_project, "i8-run-done")
    assert done["status"] == "completed", "完成态 run 不得被 sweep 改写"
    # 孤儿 step 分流语义与启动 sweep 同源（复用不新写）
    s0 = await _step_row(seeded_project, "i8-step-0")
    assert (s0["status"], s0["not_started"], s0["outcome_unknown"]) == ("error", 1, 0)
    s1 = await _step_row(seeded_project, "i8-step-1")
    assert (s1["status"], s1["not_started"], s1["outcome_unknown"]) == ("error", 0, 1)


@pytest.mark.asyncio
async def test_stop_project_cleanly_idempotent_second_stop_zero(seeded_project):
    """重入：二次 stop 的收尾必须为 0 行（幂等），终态仍归零。"""
    await _seed_run(seeded_project, "i8-run-a1", AGENT_A)
    await _seed_run(seeded_project, "i8-run-b1", AGENT_B)

    first = await stop_project_cleanly(PROJECT_ID)
    second = await stop_project_cleanly(PROJECT_ID)

    assert first["runs_interrupted"] == 2
    assert second["runs_interrupted"] == 0
    assert await _running_count(seeded_project) == 0


@pytest.mark.asyncio
async def test_close_running_runs_unknown_project_returns_zero():
    """未知项目（meta 无行）best-effort 返 0，不 raise —— 下班流程不能被收尾炸掉。"""
    n = await close_running_runs("i8-no-such-project")
    assert n == 0


@pytest.mark.asyncio
async def test_close_running_runs_no_running_rows_returns_zero(seeded_project):
    """空收尾：无 running 行时返 0（activate 每次都跑，不能有副作用）。"""
    assert await close_running_runs(PROJECT_ID) == 0
    assert await _running_count(seeded_project) == 0


# ── 并发 / 重入用例（fixplan 纪律：并发语义改动，用例先写）───────────────


@pytest.mark.asyncio
async def test_concurrent_stop_project_cleanly_end_state_zero_running(
    seeded_project,
):
    """并发重入：双 stop 同时跑 —— 不抛、终态 running=0、全部 interrupted。

    两条 sweep 在 await 点交错可能各自计到同一批行（计数可重复），但 UPDATE
    幂等 ⇒ **终态**是唯一可断言的不变量；计数断言只给先完成的那条路径留。
    """
    for rid, aid in (("i8-run-a1", AGENT_A), ("i8-run-b1", AGENT_B)):
        await _seed_run(seeded_project, rid, aid)

    results = await asyncio.gather(
        stop_project_cleanly(PROJECT_ID),
        stop_project_cleanly(PROJECT_ID),
    )

    assert sum(r["runs_interrupted"] for r in results) >= 2
    assert await _running_count(seeded_project) == 0
    for rid in ("i8-run-a1", "i8-run-b1"):
        row = await _run_row(seeded_project, rid)
        assert row["status"] == "interrupted"


@pytest.mark.asyncio
async def test_concurrent_close_running_runs_end_state_deterministic(seeded_project):
    """并发重入（细粒度入口）：双 close_running_returns 终态确定、不抛。"""
    for i in range(3):
        await _seed_run(seeded_project, f"i8-run-a{i}", AGENT_A)

    counts = await asyncio.gather(
        close_running_runs(PROJECT_ID),
        close_running_runs(PROJECT_ID),
    )

    assert all(isinstance(n, int) and n >= 0 for n in counts)
    assert sum(counts) >= 3, "至少一条路径计到全部行"
    assert await _running_count(seeded_project) == 0
    for i in range(3):
        row = await _run_row(seeded_project, f"i8-run-a{i}")
        assert row["status"] == "interrupted"


# ── 验收 ②：恢复后先回收孤儿再开新 run ─────────────────────────────────


@pytest.mark.asyncio
async def test_activate_reclaims_orphans_before_starting_agents(
    seeded_project, monkeypatch: pytest.MonkeyPatch
):
    """activate 顺序：close_running_runs **先**于 start_project_agents，
    且真实回收发生（DB running 归零）+ 出参带 runsReclaimed。"""
    from hiveweave.api import projects as api
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services import project_lifecycle as lifecycle

    await _seed_run(seeded_project, "i8-run-a1", AGENT_A)
    await _seed_run(seeded_project, "i8-run-b1", AGENT_B)

    order: list[str] = []
    real_close = lifecycle.close_running_runs

    async def spy_close(project_id: str, **kwargs) -> int:
        # 批 7 审计 P2-2 后真实签名带 close_reason kwarg —— spy 必须透传，
        # 否则 TypeError 会被 activate 的 best-effort except 静默吞掉。
        n = await real_close(project_id, **kwargs)
        order.append("reclaim")
        return n

    async def fake_start_agents(project_id: str) -> None:
        order.append("start_agents")

    class FakeGT:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            return None

    monkeypatch.setattr(lifecycle, "close_running_runs", spy_close)
    monkeypatch.setattr(agent_manager, "start_project_agents", fake_start_agents)
    monkeypatch.setattr(api, "GameTimeService", FakeGT)

    result = await api.activate_project(PROJECT_ID)

    assert order == ["reclaim", "start_agents"], order
    assert result["ok"] is True
    assert result["runsReclaimed"] == 2
    assert await _running_count(seeded_project) == 0


@pytest.mark.asyncio
async def test_repeated_activate_skips_reclaim(seeded_project, monkeypatch: pytest.MonkeyPatch):
    """批 7 审计 P1-1：项目已在班（is_started=1）时重复 activate **不得**回收。

    sweep 无时间窗（无条件收割所有 running）——对存活 agent 的活 run 收割
    会造出「账面 interrupted、实际还活着」的 run，下次唤醒即真双 run。
    已在班 ⇒ 活 run 不是孤儿 ⇒ 跳过回收（start_agents 照常幂等）。
    阳性对照：删掉 activate 里的 was_started 短路 ⇒ 本用例转红（reclaim
    被调用、活 run 被打成 interrupted）。
    """
    from hiveweave.api import projects as api
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.services import project_lifecycle as lifecycle

    # 活 run（模拟在班 agent 正在跑）+ 项目已上班
    await _seed_run(seeded_project, "i8-run-live", AGENT_A)
    await meta_db.execute(
        "UPDATE projects SET is_started = 1 WHERE id = ?", [PROJECT_ID]
    )

    calls: list[str] = []

    async def spy_close(project_id: str, **kwargs) -> int:
        calls.append("reclaim")
        return await lifecycle.close_running_runs(project_id, **kwargs)

    async def fake_start_agents(project_id: str) -> None:
        calls.append("start_agents")

    class FakeGT:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            return None

    monkeypatch.setattr(lifecycle, "close_running_runs", spy_close)
    monkeypatch.setattr(agent_manager, "start_project_agents", fake_start_agents)
    monkeypatch.setattr(api, "GameTimeService", FakeGT)

    result = await api.activate_project(PROJECT_ID)

    assert "reclaim" not in calls, calls
    assert result["runsReclaimed"] == 0
    # 活 run 不被打扰
    row = await _run_row(seeded_project, "i8-run-live")
    assert row["status"] == "running", row


@pytest.mark.asyncio
async def test_activate_without_orphans_still_reports_zero(
    seeded_project, monkeypatch: pytest.MonkeyPatch
):
    """无孤儿时 activate 照常上报 0 —— 常态路径零副作用。"""
    from hiveweave.api import projects as api
    from hiveweave.agents.supervisor import agent_manager

    class FakeGT:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args, **kwargs):
            return None

    async def fake_start_agents(project_id: str) -> None:
        return None

    monkeypatch.setattr(agent_manager, "start_project_agents", fake_start_agents)
    monkeypatch.setattr(api, "GameTimeService", FakeGT)

    result = await api.activate_project(PROJECT_ID)

    assert result["ok"] is True
    assert result["runsReclaimed"] == 0
    assert await _running_count(seeded_project) == 0


# ── 验收 ③：无双 run（同 agent 并发）────────────────────────────────────


@pytest.mark.asyncio
async def test_no_double_run_after_reclaim_then_new_run(seeded_project):
    """回收后开新 run：同 agent 的 running 行数恒为 1（旧 run 已终态）。"""
    from hiveweave.services.run_ledger import RunLedger

    await _seed_run(seeded_project, "i8-run-old", AGENT_A)

    assert await close_running_runs(PROJECT_ID) == 1
    assert await _running_count(seeded_project, AGENT_A) == 0

    new_run_id = await RunLedger().create_run(AGENT_A, "i8-act-1")

    assert await _running_count(seeded_project, AGENT_A) == 1
    old = await _run_row(seeded_project, "i8-run-old")
    assert old["status"] == "interrupted"
    new_row = await _run_row(seeded_project, new_run_id)
    assert new_row["status"] == "running"


@pytest.mark.asyncio
async def test_reclaim_without_stop_leaves_no_running_for_any_agent(seeded_project):
    """ Crash 后直接复工（没走下班）的场景：activate 侧回收覆盖全部 agent。 """
    for rid, aid in (
        ("i8-run-a1", AGENT_A),
        ("i8-run-a2", AGENT_A),
        ("i8-run-b1", AGENT_B),
    ):
        await _seed_run(seeded_project, rid, aid)

    n = await close_running_runs(PROJECT_ID)

    assert n == 3
    assert await _running_count(seeded_project, AGENT_A) == 0
    assert await _running_count(seeded_project, AGENT_B) == 0
