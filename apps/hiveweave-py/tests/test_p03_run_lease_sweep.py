"""P0-3 回归测试 — 运行时 run 租约 sweep（lease_expires_at 只写不读的收口）。

病灶（qoder 双项目报告 P0-3）：``agent_runs.lease_expires_at`` 由
``create_run`` 写入后**运行时没有任何读取方** —— 进程内 run 死亡（收口
路径漏掉 / asyncio task 静默消失 / OOM kill）后 running 行悬挂到下次重启
才被 startup sweep 收口（实测 5 条悬挂 ≈9 小时）。

修复 = startup sweep 核心提取为公共核心 ``_sweep_running_rows_locked``，
新增 ``sweep_expired_run_leases``（判据：lease 已过期；不在注册表的行直接
收口），挂 game_time 30s tick（``GameTimeService._sweep_expired_run_leases``）。

⚠ 批28-2（2026-10-08）语义更新：注册表内的过期 run 不再无条件豁免 ——
改为 abort 先行 + 宽限 + 双保险（Robert 失控 run 超 lease 6 分钟仍 running
的病灶，见 test_run_lease_active_harvest.py）。本文件聚焦 P0-3 原语义在
**首个 tick** 内的不变量（单次 sweep 只发 abort、不收口活跃行）。

验收判据（可机检）：
  ① lease 已过期 + 不在注册表 ⇒ sweep 后 interrupted（与启动 sweep 同语义：
    孤儿 step 分流 + R11 零账占位补账）；
  ② lease 未过期不动（健康 run 在 tick 间隙零写）；
  ③ 在注册表中的 run 单次 sweep 不动（首 tick 只发 abort 进宽限 ——
    防「死行未收而活流先杀」的次序颠倒；宽限过后的收割归批28-2 测试）；
  ④ lease IS NULL 的老行不动（交还给启动 sweep）；
  ⑤ usage 补录与启动 sweep 同源（批 2 A7 占位行口径一致）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services import run_ledger as rl_mod
from hiveweave.services.run_ledger import (
    RUN_LEASE_SWEEP_CLOSE_REASON,
    sweep_expired_run_leases,
    sweep_stale_agent_runs,
)

PROJECT_ID = "p03-proj-0001"
AGENT_A = "p03-ceo-0001"
AGENT_B = "p03-exec-0001"


@pytest.fixture(autouse=True)
def _isolated_harvest_state():
    """批28-2 活跃流收割状态隔离（模块级字典，防跨文件串扰）。"""
    rl_mod.reset_lease_abort_state()
    yield
    rl_mod.reset_lease_abort_state()


class _Project:
    """seeded_project 的句柄：per-project DB 连接 + workspace 路径。"""

    def __init__(self, conn, ws: str) -> None:
        self.conn = conn
        self.ws = ws


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


@pytest.fixture
async def seeded_project(tmp_path):
    """Meta projects 行 + per-project DB（2 个 active agent 行）+ 路由注册。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "p03-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    for aid, short, role in ((AGENT_A, "P3A", "ceo"), (AGENT_B, "P3B", "executor")):
        cur = await conn.execute(
            "INSERT INTO agents (id, short_id, project_id, name, role, status, "
            "model_id, created_at) VALUES (?, ?, ?, ?, ?, 'active', 'gpt-x', ?)",
            [aid, short, PROJECT_ID, f"Agent-{short}", role, now],
        )
        await cur.close()
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
    yield _Project(conn, ws)
    agent_router.reset_for_tests()


async def _seed_run(
    proj: _Project,
    run_id: str,
    agent_id: str,
    *,
    status: str = "running",
    lease_expires_at: int | None | str = "expired",
    actual_llm_calls: int = 0,
    started_at: int | None = None,
) -> None:
    """lease 缺省 = 1 分钟前（已过期）；传 None 写 NULL（老行形态）。

    ``lease_expires_at`` 三态：'expired'（缺省，已过期）/ int（指定时刻）/
    None（NULL，老行）。
    """
    now = int(time.time() * 1000)
    if lease_expires_at == "expired":
        lease_expires_at = now - 60_000
    if started_at is None:
        started_at = now - 120_000
    cur = await proj.conn.execute(
        "INSERT INTO agent_runs (id, agent_id, activation_id, status, "
        "lease_expires_at, budget_llm_calls, budget_tool_calls, "
        "budget_elapsed_ms, actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, NULL, ?, ?, 50, 100, 600000, ?, 0, ?)",
        [run_id, agent_id, status, lease_expires_at, actual_llm_calls, started_at],
    )
    await cur.close()
    await proj.conn.commit()


async def _seed_running_step(
    proj: _Project,
    step_id: str,
    run_id: str,
    started: int | None,
) -> None:
    cur = await proj.conn.execute(
        "INSERT INTO run_steps (id, run_id, step_index, step_type, status, "
        "started_at, started) VALUES (?, ?, 0, 'tool_call', 'running', ?, ?)",
        [step_id, run_id, int(time.time() * 1000), started],
    )
    await cur.close()
    await proj.conn.commit()


async def _run_row(proj: _Project, run_id: str) -> dict:
    cur = await proj.conn.execute(
        "SELECT id, agent_id, status, ended_at, error_reason, result_summary "
        "FROM agent_runs WHERE id = ?",
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
        "result_summary": row[5],
    }


async def _step_row(proj: _Project, step_id: str) -> dict:
    cur = await proj.conn.execute(
        "SELECT status, ended_at, outcome_unknown, not_started, timeout_kind, "
        "error FROM run_steps WHERE id = ?",
        [step_id],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"step {step_id} not found"
    return {
        "status": row[0],
        "ended_at": row[1],
        "outcome_unknown": row[2],
        "not_started": row[3],
        "timeout_kind": row[4],
        "error": row[5],
    }


async def _usage_rows(proj: _Project, run_id: str) -> list[dict]:
    cur = await proj.conn.execute(
        "SELECT run_id, request_type, provider, total_tokens, duration_ms "
        "FROM llm_usage WHERE run_id = ?",
        [run_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 验收 ①：lease 已过期 + 不在注册表 ⇒ interrupted（启动 sweep 同语义）──


@pytest.mark.asyncio
async def test_expired_lease_outside_registry_swept_to_interrupted(seeded_project):
    """悬挂行收口：状态/ended_at/error_reason 与启动 sweep 同语义，孤儿 step 分流。"""
    await _seed_run(seeded_project, "p03-run-dead", AGENT_A)
    # 执行中被掐的孤儿 step（started=1 ⇒ outcome_unknown，不给"可直接重试"）
    await _seed_running_step(seeded_project, "p03-step-1", "p03-run-dead", started=1)
    # 正常完成的 run 不得被波及
    await _seed_run(seeded_project, "p03-run-done", AGENT_B, status="completed")

    swept = await sweep_expired_run_leases(seeded_project.ws)

    assert swept == 1
    row = await _run_row(seeded_project, "p03-run-dead")
    assert row["status"] == "interrupted", row
    assert row["ended_at"] is not None, row
    assert row["error_reason"] == RUN_LEASE_SWEEP_CLOSE_REASON, row
    done = await _run_row(seeded_project, "p03-run-done")
    assert done["status"] == "completed", "完成态 run 不得被租约 sweep 改写"

    # 孤儿 step：与启动 sweep 同语义（outcome_unknown + timeout_kind='swept'，
    # 来路段换成 run_lease_sweep，正文重试判据不变）
    s = await _step_row(seeded_project, "p03-step-1")
    assert (s["status"], s["outcome_unknown"], s["not_started"], s["timeout_kind"]) == (
        "error", 1, 0, "swept",
    ), s
    assert s["error"].startswith("run_lease_sweep: orphan step from"), s
    assert s["error"].endswith("Do not retry blindly."), s


@pytest.mark.asyncio
async def test_lease_sweep_idempotent_second_call_zero(seeded_project):
    """重入：二次 sweep 返 0（tick 每 30s 跑一次，必须幂等零成本）。"""
    await _seed_run(seeded_project, "p03-run-dead", AGENT_A)

    assert await sweep_expired_run_leases(seeded_project.ws) == 1
    assert await sweep_expired_run_leases(seeded_project.ws) == 0
    row = await _run_row(seeded_project, "p03-run-dead")
    assert row["status"] == "interrupted"


# ── 验收 ②：lease 未过期不动 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unexpired_lease_untouched(seeded_project):
    """健康 run（lease 未到期）在 tick 间隙零写 —— 不被打扰。"""
    now = int(time.time() * 1000)
    await _seed_run(
        seeded_project, "p03-run-live", AGENT_A,
        lease_expires_at=now + 600_000,
    )

    swept = await sweep_expired_run_leases(seeded_project.ws)

    assert swept == 0
    row = await _run_row(seeded_project, "p03-run-live")
    assert row["status"] == "running", row
    assert row["ended_at"] is None, row


@pytest.mark.asyncio
async def test_null_lease_left_to_startup_sweep(seeded_project):
    """lease IS NULL 的老行运行时不收（判据排除），启动 sweep 照常全收。"""
    await _seed_run(seeded_project, "p03-run-old", AGENT_A, lease_expires_at=None)

    assert await sweep_expired_run_leases(seeded_project.ws) == 0
    row = await _run_row(seeded_project, "p03-run-old")
    assert row["status"] == "running", row

    # 对照：启动 sweep（无条件全收）会收走同一行 —— NULL 的归属在启动侧
    assert await sweep_stale_agent_runs(seeded_project.ws) == 1
    row = await _run_row(seeded_project, "p03-run-old")
    assert row["status"] == "interrupted", row


# ── 验收 ③：在注册表中的 run 首 tick 不动（批28-2：abort 先行进宽限）──


@pytest.mark.asyncio
async def test_registry_exempt_live_run_untouched(seeded_project):
    """活跃 run（lease 已过期但在注册表）首 tick 不收口 —— 批28-2 起处置
    改为 abort 先行 + 宽限 + 双保险（宽限过后的收割语义见
    test_run_lease_active_harvest.py）。本用例钉住首 tick 不变量：
    发 abort、不动 DB 行 —— 死行先收、活流后杀的次序不颠倒。"""
    await _seed_run(seeded_project, "p03-run-live", AGENT_A)  # 缺省已过期

    swept = await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={"p03-run-live"},
    )

    assert swept == 0
    row = await _run_row(seeded_project, "p03-run-live")
    assert row["status"] == "running", row
    assert row["ended_at"] is None, row
    # abort 已进宽限状态（收割周期开启但未收口）
    assert "p03-run-live" in rl_mod._LEASE_ABORT_STATE


@pytest.mark.asyncio
async def test_mixed_live_and_stale_only_stale_swept(seeded_project):
    """P0-3 主场景：同 agent 一条活 run（豁免）+ 一条死亡遗留行（悬挂）——
    只收悬挂行，活 run 原样。"""
    await _seed_run(seeded_project, "p03-run-live", AGENT_A)   # 已过期但在册
    await _seed_run(seeded_project, "p03-run-dead", AGENT_A)   # 已过期且不在册
    await _seed_running_step(seeded_project, "p03-step-d", "p03-run-dead", started=0)

    swept = await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={"p03-run-live"},
    )

    assert swept == 1
    live = await _run_row(seeded_project, "p03-run-live")
    assert live["status"] == "running", live
    dead = await _run_row(seeded_project, "p03-run-dead")
    assert dead["status"] == "interrupted", dead
    # 未派发孤儿 step（started=0）⇒ not_started 分流，同启动 sweep
    s = await _step_row(seeded_project, "p03-step-d")
    assert (s["status"], s["not_started"], s["outcome_unknown"]) == ("error", 1, 0), s


# ── 验收 ⑤：usage 补录与启动 sweep 同源（批 2 A7 占位行口径）────────────


@pytest.mark.asyncio
async def test_placeholder_usage_parity_with_startup_sweep(seeded_project):
    """零账死亡 run（declared>0、llm_usage 零行）⇒ 补机检占位行 +
    result_summary 前置标记 —— 口径与启动 sweep 完全一致。"""
    await _seed_run(
        seeded_project, "p03-run-dead", AGENT_A, actual_llm_calls=3,
    )

    assert await sweep_expired_run_leases(seeded_project.ws) == 1

    usage = await _usage_rows(seeded_project, "p03-run-dead")
    assert len(usage) == 1, usage
    u = usage[0]
    assert u["request_type"] is None and u["provider"] is None, u
    assert int(u["total_tokens"] or 0) == 0 and int(u["duration_ms"] or 0) == 0, u
    row = await _run_row(seeded_project, "p03-run-dead")
    assert str(row["result_summary"]).startswith("[llm_usage_placeholder]"), row


@pytest.mark.asyncio
async def test_run_with_existing_usage_gets_no_placeholder(seeded_project):
    """已有账的 run 不补占位（占位只补「零账」断口，与启动 sweep 同判）。"""
    await _seed_run(seeded_project, "p03-run-dead", AGENT_A, actual_llm_calls=2)
    cur = await seeded_project.conn.execute(
        "INSERT INTO llm_usage (id, agent_id, project_id, run_id, task_id, "
        "model_id, request_type, provider, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, total_tokens, duration_ms, "
        "cold_start, creation_unreported, created_at) "
        "VALUES ('p03-u1', ?, NULL, 'p03-run-dead', NULL, 'gpt-x', 'chat', "
        "'openai', 10, 5, 0, 0, 15, 100, 0, 0, ?)",
        [AGENT_A, int(time.time() * 1000)],
    )
    await cur.close()
    await seeded_project.conn.commit()

    assert await sweep_expired_run_leases(seeded_project.ws) == 1

    usage = await _usage_rows(seeded_project, "p03-run-dead")
    assert len(usage) == 1, "不得重复补占位"
    assert usage[0]["request_type"] == "chat", usage


# ── game_time 接线：注册表判据 = PROCESSING agent 的 _current_run_id ──────


@pytest.mark.asyncio
async def test_game_time_predicate_exempts_only_processing_current_run(
    seeded_project, monkeypatch: pytest.MonkeyPatch,
):
    """_sweep_expired_run_leases 的注册表判据：PROCESSING 中 agent 的
    _current_run_id 进活跃集（首 tick abort 进宽限、不收口）；IDLE agent 的
    遗留 _current_run_id 不进活跃集（悬挂行直接收口）。"""
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.agents.types import AgentState
    from hiveweave.services.game_time import GameTimeService

    await _seed_run(seeded_project, "p03-run-live", AGENT_A)   # 活 run（在册）
    await _seed_run(seeded_project, "p03-run-stale", AGENT_B)  # 死亡遗留行

    fake_agents = [
        SimpleNamespace(
            project_id=PROJECT_ID,
            status=AgentState.PROCESSING,
            _current_run_id="p03-run-live",
        ),
        # agent 已回 idle 但 _current_run_id 还指着死亡遗留行 —— 悬挂行须收口
        SimpleNamespace(
            project_id=PROJECT_ID,
            status=AgentState.IDLE,
            _current_run_id="p03-run-stale",
        ),
        # 别的项目的 PROCESSING agent —— 不得跨项目豁免
        SimpleNamespace(
            project_id="other-project",
            status=AgentState.PROCESSING,
            _current_run_id="p03-run-stale",
        ),
    ]
    monkeypatch.setattr(agent_manager, "list_all", lambda: fake_agents)

    await GameTimeService()._sweep_expired_run_leases(PROJECT_ID)

    live = await _run_row(seeded_project, "p03-run-live")
    assert live["status"] == "running", live
    stale = await _run_row(seeded_project, "p03-run-stale")
    assert stale["status"] == "interrupted", stale
