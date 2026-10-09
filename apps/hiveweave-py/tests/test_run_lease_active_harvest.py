"""N3 批28-2 施工2 回归：lease 过期收割活跃流（接 run_ledger tick）。

病灶（第六轮实锤）：Robert 第三次失控 run 超 lease（21:40 过期）6 分钟仍
running —— P0-3 的注册表豁免把活跃流永久放过（只收「不在注册表」的死行）。

修法（abort 先行 + 宽限 + 双保险，配 renew_lease 续约）：
  ① 超 lease 活跃 run ⇒ abort 发出（注册表内可见置位）+ 宽限内自收口
    ⇒ 不打双保险（error_reason 保持流侧收口原值）；
  ② abort 后仍不死（宽限过后仍 running）⇒ 双保险 interrupted
    （RUN_LEASE_ACTIVE_CLOSE_REASON，与死行收口来路分流）；
  ③ lease 正常续约的 run 不被收割（renew_lease 推 lease ⇒ 不再选中、
    宽限中续约 ⇒ 双保险空打、状态清空 —— 「lease 刚续过但 sweep 用旧
    快照」竞态的锁内重查防线）；
  ④ 幂等：同 run 只 abort 一次（宽限内重复 sweep 不重发）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.llm.streamer.abort_source import (
    register_abort_source,
    reset_abort_registry,
    unregister_abort_source,
)
from hiveweave.services import run_ledger as rl
from hiveweave.services.run_ledger import (
    RUN_LEASE_ACTIVE_CLOSE_REASON,
    RUN_LEASE_SWEEP_CLOSE_REASON,
    run_ledger,
    sweep_expired_run_leases,
)

PROJECT_ID = "b282-proj-0001"
AGENT_A = "b282-agent-0001"

_GRACE = rl.RUN_LEASE_ABORT_GRACE_MS


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    reset_abort_registry()
    rl.reset_lease_abort_state()
    yield
    await meta_db.close_meta_db()
    reset_abort_registry()
    rl.reset_lease_abort_state()


@pytest.fixture
async def seeded_project(tmp_path):
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "b282-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    # agents 行 + AgentRouter 注册（renew_lease 经 project_db.execute 按
    # agent_id 路由 —— 未注册会拒写；照 test_p03_run_lease_sweep 模式）
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "model_id, created_at) VALUES (?, ?, ?, ?, ?, 'active', 'gpt-x', ?)",
        [AGENT_A, "B2A", PROJECT_ID, "Agent-B2A", "executor", now],
    )
    await cur.close()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_A,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="B2A",
            name="Agent-B2A",
            role="executor",
            status="active",
        )
    )
    await conn.commit()

    class _Proj:
        pass

    proj = _Proj()
    proj.conn = conn
    proj.ws = ws
    yield proj
    agent_router.reset_for_tests()


async def _seed_run(
    proj,
    run_id: str,
    *,
    status: str = "running",
    lease_expires_at: int | None | str = "expired",
    started_at: int | None = None,
    budget_elapsed_ms: int = 600_000,
) -> None:
    now = int(time.time() * 1000)
    if lease_expires_at == "expired":
        lease_expires_at = now - 60_000
    if started_at is None:
        started_at = now - 20 * 60_000
    cur = await proj.conn.execute(
        "INSERT INTO agent_runs (id, agent_id, activation_id, status, "
        "lease_expires_at, budget_llm_calls, budget_tool_calls, "
        "budget_elapsed_ms, actual_llm_calls, actual_tool_calls, started_at) "
        "VALUES (?, ?, NULL, ?, ?, 50, 100, ?, 0, 0, ?)",
        [run_id, AGENT_A, status, lease_expires_at, budget_elapsed_ms, started_at],
    )
    await cur.close()
    await proj.conn.commit()


async def _run_row(proj, run_id: str) -> dict:
    cur = await proj.conn.execute(
        "SELECT status, ended_at, error_reason, lease_expires_at "
        "FROM agent_runs WHERE id = ?",
        [run_id],
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    return dict(row)


def _backdate_grace(run_id: str, ms: int = _GRACE + 1_000) -> None:
    """把该 run 的 abort 签发时间拨回宽限之前（免真实 sleep）。"""
    agent_id, _issued = rl._LEASE_ABORT_STATE[run_id]
    rl._LEASE_ABORT_STATE[run_id] = (agent_id, int(time.time() * 1000) - ms)


# ── ① 超 lease 活跃 run：abort 发出 + 宽限内自收口 ⇒ 不打双保险 ──────


@pytest.mark.asyncio
async def test_active_expired_run_aborted_then_self_closed_no_double_insurance(
    seeded_project,
):
    run_id = "b282-run-live-1"
    await _seed_run(seeded_project, run_id)

    src = register_abort_source(AGENT_A, run_id=run_id)
    try:
        # 第一次 sweep：abort 发出（源置位、reason 正确），行不动（宽限中）
        swept = await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        )
        assert swept == 0
        assert src.event.is_set()
        assert src.reason == "lease_expired"
        row = await _run_row(seeded_project, run_id)
        assert row["status"] == "running" and row["ended_at"] is None
        assert run_id in rl._LEASE_ABORT_STATE

        # 宽限内流自收口（批28-1 管道 → handle_completion 落 interrupted）
        await seeded_project.conn.execute(
            "UPDATE agent_runs SET status='interrupted', ended_at=?, "
            "error_reason='stream_aborted' WHERE id=?",
            [int(time.time() * 1000), run_id],
        )
        await seeded_project.conn.commit()

        # 宽限过后再 sweep：run 已终态 ⇒ 双保险空打（error_reason 不被改写）
        _backdate_grace(run_id)
        swept2 = await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        )
        assert swept2 == 0
        row2 = await _run_row(seeded_project, run_id)
        assert row2["status"] == "interrupted"
        assert row2["error_reason"] == "stream_aborted"  # 流侧收口原值保留
        # 收割周期结束 ⇒ 状态清空
        assert run_id not in rl._LEASE_ABORT_STATE
    finally:
        unregister_abort_source(AGENT_A, src)


# ── ② abort 后仍不死 ⇒ 双保险 interrupted ──────────────────────────


@pytest.mark.asyncio
async def test_active_run_survives_abort_reaped_as_double_insurance(
    seeded_project,
):
    run_id = "b282-run-stuck-1"
    await _seed_run(seeded_project, run_id)

    src = register_abort_source(AGENT_A, run_id=run_id)
    try:
        assert await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        ) == 0
        assert src.event.is_set()  # abort 确已发出

        # 宽限过后 run 仍 running（流死了没收口）
        _backdate_grace(run_id)
        swept = await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        )
        assert swept == 1
        row = await _run_row(seeded_project, run_id)
        assert row["status"] == "interrupted"
        assert row["ended_at"] is not None
        assert row["error_reason"] == RUN_LEASE_ACTIVE_CLOSE_REASON
        assert run_id not in rl._LEASE_ABORT_STATE
        # 再 sweep 幂等零收
        assert await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        ) == 0
    finally:
        unregister_abort_source(AGENT_A, src)


# ── ③ lease 正常续约的 run 不被收割 ─────────────────────────────────


@pytest.mark.asyncio
async def test_renewed_lease_run_not_harvested(seeded_project):
    run_id = "b282-run-healthy-1"
    now = int(time.time() * 1000)
    await _seed_run(
        seeded_project, run_id, lease_expires_at=now - 60_000  # 起手已过期
    )

    # 进展续约（LLM 轮/工具步边界）：lease 推到 now + max(预算, 下限)
    await run_ledger.renew_lease(AGENT_A, run_id)
    row = await _run_row(seeded_project, run_id)
    assert row["lease_expires_at"] >= now + rl.RUN_LEASE_RENEW_FLOOR_MS - 1_000
    assert row["status"] == "running"

    # sweep：不在活跃集也能被续约救回（lease 未过期 ⇒ 判据不命中）
    assert await sweep_expired_run_leases(seeded_project.ws) == 0
    assert await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    ) == 0
    row2 = await _run_row(seeded_project, run_id)
    assert row2["status"] == "running" and row2["ended_at"] is None
    assert run_id not in rl._LEASE_ABORT_STATE  # 从未进入收割状态


@pytest.mark.asyncio
async def test_renew_during_grace_prevents_double_insurance(seeded_project):
    """「lease 刚续过但 sweep 用旧快照」竞态：宽限中 run 恢复进展并续约 ⇒
    双保险的锁内重查（status='running' 且 lease 仍过期）空打、状态清空。"""
    run_id = "b282-run-race-1"
    await _seed_run(seeded_project, run_id)

    assert await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    ) == 0  # abort 已发（宽限中）

    # 快照之后、双保险之前：run 恢复进展（renew_lease 推 lease）
    await run_ledger.renew_lease(AGENT_A, run_id)

    _backdate_grace(run_id)  # 宽限已过（模拟下一 tick）
    swept = await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    )
    assert swept == 0
    row = await _run_row(seeded_project, run_id)
    assert row["status"] == "running"  # 续约救回，未被误杀
    assert run_id not in rl._LEASE_ABORT_STATE
    # 且不再重发 abort（lease 未过期 ⇒ 不再选中）
    assert await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    ) == 0


@pytest.mark.asyncio
async def test_renew_lease_floor_covers_tool_step_cap(seeded_project, monkeypatch):
    """续约窗口 = max(run 预算, 下限)：小预算 run 也拿到 ≥15min 窗口
    （python_script 单步 10min 上限 + 余量），env 可调下限。"""
    run_id = "b282-run-budget-1"
    await _seed_run(
        seeded_project, run_id,
        lease_expires_at=int(time.time() * 1000) + 60_000,
        budget_elapsed_ms=120_000,  # run 自身预算只有 2min
    )
    await run_ledger.renew_lease(AGENT_A, run_id)
    row = await _run_row(seeded_project, run_id)
    assert (
        row["lease_expires_at"] >= int(time.time() * 1000) + 900_000 - 1_000
    )  # 下限 15min 生效（而非 2min）

    monkeypatch.setenv("HIVEWEAVE_RUN_LEASE_RENEW_FLOOR_MS", "3600000")
    await run_ledger.renew_lease(AGENT_A, run_id)
    row2 = await _run_row(seeded_project, run_id)
    assert row2["lease_expires_at"] >= int(time.time() * 1000) + 3_600_000 - 1_000


@pytest.mark.asyncio
async def test_renew_lease_skips_terminal_run(seeded_project):
    """status='running' 守卫：已收口 run 不复活 lease。"""
    run_id = "b282-run-dead-1"
    await _seed_run(seeded_project, run_id, status="interrupted")
    await run_ledger.renew_lease(AGENT_A, run_id)
    row = await _run_row(seeded_project, run_id)
    assert row["lease_expires_at"] < int(time.time() * 1000)  # 原值未动


# ── ④ 幂等：同 run 只 abort 一次 ────────────────────────────────────


@pytest.mark.asyncio
async def test_abort_issued_once_within_grace(seeded_project, monkeypatch):
    run_id = "b282-run-idem-1"
    await _seed_run(seeded_project, run_id)

    calls = {"n": 0}
    import hiveweave.llm.streamer.abort_source as abort_source_mod

    real_abort = abort_source_mod.abort_stream

    def counting_abort(agent_id, run_id=None, reason=""):
        calls["n"] += 1
        return real_abort(agent_id, run_id=run_id, reason=reason)

    monkeypatch.setattr(abort_source_mod, "abort_stream", counting_abort)

    # 宽限窗口内连扫三次：abort 只发一次
    for _ in range(3):
        await sweep_expired_run_leases(
            seeded_project.ws, exempt_run_ids={run_id}
        )
    assert calls["n"] == 1
    row = await _run_row(seeded_project, run_id)
    assert row["status"] == "running"  # 宽限内不收口

    # 宽限过后：双保险收口一次，之后幂等零收
    _backdate_grace(run_id)
    assert await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    ) == 1
    assert calls["n"] == 1  # 双保险不再发 abort
    assert await sweep_expired_run_leases(
        seeded_project.ws, exempt_run_ids={run_id}
    ) == 0
    assert run_id not in rl._LEASE_ABORT_STATE


# ── 既有 P0-3 语义不回归：死行照收、来路文案分流 ──────────────────────


@pytest.mark.asyncio
async def test_dead_row_still_reaped_with_original_reason(seeded_project):
    """不在注册表的过期死行：P0-3 原路径直收（RUN_LEASE_SWEEP_CLOSE_REASON
    —— 与活跃流双保险的来路文案分流）。"""
    await _seed_run(seeded_project, "b282-run-deadrow-1")

    assert await sweep_expired_run_leases(seeded_project.ws) == 1
    row = await _run_row(seeded_project, "b282-run-deadrow-1")
    assert row["status"] == "interrupted"
    assert row["error_reason"] == RUN_LEASE_SWEEP_CLOSE_REASON
