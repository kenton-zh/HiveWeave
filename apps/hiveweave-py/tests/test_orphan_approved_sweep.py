"""P0-4 (TEST_DSH_33): approved 任务收口的定时器（此前无定时器）。

现场：M-C 02:49 approved、merge 义务已 fulfilled，此后 75 分钟到会话结束
无人再 merge/hire/重启 —— migrate_orphan_approved 的三个搭便车调用点
一次都没触发，任务永久停在 approved。修复：挂进 game_time tick。

契约（services/game_time.py）：
- ORPHAN_APPROVED_SWEEP_TICKS = 24（5s tick × 24 = 120s 档，与报告建议一致）
- tick 计数到 24 的倍数时调用 _sweep_orphan_approved
- _sweep_orphan_approved 只补时钟：门槛全部留在 migrate_orphan_approved
  自己身上（VERIFY 走 _close_verify_and_parent、10min 宽限、pending merge
  义务跳过），此处不得放宽任何门
"""

from __future__ import annotations

import pytest

from hiveweave.services import game_time as gt


@pytest.fixture
def svc() -> gt.GameTimeService:
    return gt.GameTimeService()


def _mk_state(tick_count: int) -> dict:
    return {
        "project_id": "proj-oas",
        "current_game_seconds": 0,
        "real_started_at": 0,
        "alarms": [],
        "tick_count": tick_count,
        "task": None,
        "stall_cooldowns": {},
    }


def _stub_calls():
    calls: dict[str, int] = {}

    def _stub(name):
        async def _f(*a, **k):
            calls[name] = calls.get(name, 0) + 1

        return _f

    return calls, _stub


@pytest.fixture
def patched_tick_methods(monkeypatch: pytest.MonkeyPatch, svc):
    """隔离 tick() 的全部副作用方法（只留被测的 sweep 调用点）。"""
    calls, _stub = _stub_calls()
    for name in (
        "_persist_time",
        "_process_wait_contracts",
        "_sweep_orphan_streaming",
        "_reconcile_worktrees",
        "_check_stalled",
        "_nudge_stale_verify",
        "_sweep_orphan_approved",
        # STALL_CHECK_TICKS 同为 24：同档触发的看门狗族一并隔离
        "_nudge_stale_ledger",
        "_reconcile_blocked_tasks",
        "_check_dead_agents",
        "_check_silent_agents",
    ):
        monkeypatch.setattr(svc, name, _stub(name), raising=True)
    monkeypatch.setattr(gt, "_states", {"proj-oas": _mk_state(23)})
    monkeypatch.setattr(
        "hiveweave.services.task_event_relay.task_event_relay.process_pending",
        _stub("relay"),
    )
    # obligation scan 在同档触发（24%12==0），patch 其 DB 依赖
    monkeypatch.setattr(
        "hiveweave.services.obligation.ObligationLedger.scan_overdue",
        _stub("obligation_scan"),
    )
    monkeypatch.setattr(
        "hiveweave.services.obligation.ObligationLedger."
        "audit_missing_review_obligations",
        _stub("obligation_audit"),
    )
    return calls


def test_sweep_cadence_is_120s():
    """ORPHAN_APPROVED_SWEEP_TICKS × TICK_INTERVAL = 120s —— 与报告建议的 120s 档一致。"""
    assert gt.ORPHAN_APPROVED_SWEEP_TICKS == 24
    assert gt.ORPHAN_APPROVED_SWEEP_TICKS * gt.TICK_INTERVAL == 120


@pytest.mark.asyncio
async def test_tick_fires_sweep_on_24th_tick(patched_tick_methods, svc):
    """tick_count 23 → 24：sweep 必须被调用（挂载点不断链）。"""
    await svc.tick("proj-oas")
    assert patched_tick_methods["_sweep_orphan_approved"] == 1


@pytest.mark.asyncio
async def test_tick_skips_sweep_off_cadence(
    monkeypatch: pytest.MonkeyPatch, svc
):
    """tick_count 22 → 23：非 24 倍数不扫（省 DB 轮询）。"""
    calls, _stub = _stub_calls()
    for name in (
        "_persist_time",
        "_process_wait_contracts",
        "_sweep_orphan_streaming",
        "_reconcile_worktrees",
        "_sweep_orphan_approved",
    ):
        monkeypatch.setattr(svc, name, _stub(name), raising=True)
    monkeypatch.setattr(gt, "_states", {"proj-oas": _mk_state(22)})
    await svc.tick("proj-oas")
    assert "_sweep_orphan_approved" not in calls


@pytest.mark.asyncio
async def test_sweep_delegates_to_migrate_orphan_approved(
    monkeypatch: pytest.MonkeyPatch, svc
):
    """sweep 只补时钟：全部门槛留在 migrate_orphan_approved 自己身上。"""
    got: list[str] = []

    class FakeTaskService:
        async def migrate_orphan_approved(self, project_id):
            got.append(project_id)
            return {"verifying": 0, "closed": 1}

    monkeypatch.setattr(
        "hiveweave.services.task.TaskService", FakeTaskService
    )
    # 有收口动作时才打 info 日志——返回值透传即可观测
    await svc._sweep_orphan_approved("proj-oas")
    assert got == ["proj-oas"]


@pytest.mark.asyncio
async def test_tick_survives_sweep_failure(
    monkeypatch: pytest.MonkeyPatch, svc
):
    """sweep 抛错由 tick 侧 try/except 兜住 —— 一个项目的收口故障
    不得炸掉整条 tick 链（后面还有 stall 看门狗）。"""
    calls, _stub = _stub_calls()

    async def _boom(project_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr(svc, "_sweep_orphan_approved", _boom, raising=True)
    for name in (
        "_persist_time",
        "_process_wait_contracts",
        "_sweep_orphan_streaming",
        "_reconcile_worktrees",
        "_check_stalled",
        "_nudge_stale_verify",
        "_nudge_stale_ledger",
        "_reconcile_blocked_tasks",
        "_check_dead_agents",
        "_check_silent_agents",
    ):
        monkeypatch.setattr(svc, name, _stub(name), raising=True)
    monkeypatch.setattr(gt, "_states", {"proj-oas": _mk_state(23)})
    monkeypatch.setattr(
        "hiveweave.services.task_event_relay.task_event_relay.process_pending",
        _stub("relay"),
    )
    monkeypatch.setattr(
        "hiveweave.services.obligation.ObligationLedger.scan_overdue",
        _stub("obligation_scan"),
    )
    monkeypatch.setattr(
        "hiveweave.services.obligation.ObligationLedger."
        "audit_missing_review_obligations",
        _stub("obligation_audit"),
    )
    await svc.tick("proj-oas")  # 不 raise 即通过
    # 同 tick 后续看门狗族仍被调度（故障不拖累同档其它机制）
    assert calls.get("_nudge_stale_ledger") == 1
