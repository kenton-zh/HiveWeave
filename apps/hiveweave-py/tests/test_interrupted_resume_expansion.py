"""断流补偿闭环回归（TEST_DSH_24 视界事故）.

被测:
  - recovery.py llm_error 无未读 inbox 分支 → 按闭式判定挂补偿唤醒
  - completion.py stall_break 补偿范围扩 claimed
  - game_time.py 看门狗节奏 env 化

事故形态（2026-08-22 TEST_DSH_24）: M1 executor（claimed 任务 90% 完成）
续跑轮 LLM 断流（write timeout），无未读 inbox → recovery 三条分支全不进
→ 直接 idle 零补偿 → 纯等 10min watchdog。修复后: 闭式判定有名下活
→ ~45s 补偿唤醒，watchdog 退为兜底。

变异验证:
  - 删 recovery else 分支 → test 1/2 失败
  - completion 过滤换回 == "running" → test 3 失败
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── recovery.py: llm_error 无 inbox → 闭式补偿 ─────────────


def _err_agent(**kw):
    """recovery.handle_error 依赖的最小替身."""
    from types import SimpleNamespace

    ag = SimpleNamespace(
        id="ag-1",
        project_id="proj-1",
        pending_inbox_msg_ids=None,          # 无未读 inbox（事故形态）
        _consecutive_errors=1,               # 未超阈值
        _CONSECUTIVE_ERROR_MAX=3,
        _rate_limit_streak=0,
        _stream_timeout_streak=0,
        _current_run_id=None,                # 跳过 run ledger
        _streaming_msg_id=None,
        _chat_msg=SimpleNamespace(save_message=AsyncMock()),
        _work_log=SimpleNamespace(write_work_log=AsyncMock()),
        _broadcast_stream_event=lambda *a, **k: None,
        _broadcast_agent_health=lambda *a, **k: None,
        _arm_interrupted_resume=MagicMock(),  # 同步方法（agent.py def 非 async）
        _write_resume_checkpoint=AsyncMock(),
        _arm_resume_cooldown=AsyncMock(),
        _arm_resume_suppressed=AsyncMock(),
        _ack_inbox_on_give_up=AsyncMock(),
        _escalate_turn_interruption=AsyncMock(),
        _inject_ledger_review_wake=AsyncMock(),
        _cancel_safety_timer=AsyncMock(),
        _go_idle=AsyncMock(),
    )
    for k, v in kw.items():
        setattr(ag, k, v)
    return ag


async def test_llm_error_no_inbox_open_work_arms_resume():
    """断流 + 无未读 + 名下有 assignee 开放任务 → 挂补偿（~45s 恢复）.

    变异: 删 recovery else 分支 → _arm_interrupted_resume 未调用 → 失败."""
    from hiveweave.agents import recovery as recovery_mod

    ag = _err_agent()
    open_tasks = [
        {"id": "t-claimed-1", "assignee_id": "ag-1", "status": "claimed"},
        {"id": "t-running-1", "assignee_id": "ag-1", "status": "running"},
        # reviewer/creator 义务不在 assignee 补偿范围（唤醒文案只覆盖
        # assignee 推进语义；审/merge 债由 watchdog 兜底口径覆盖）
        {"id": "t-review-1", "assignee_id": None, "reviewer_id": "ag-1",
         "status": "submitted"},
    ]
    with patch(
        "hiveweave.services.task.TaskService.get_open_work_obligations",
        AsyncMock(return_value=open_tasks),
    ):
        await recovery_mod.handle_error(
            ag, RuntimeError("The write operation timed out")
        )
    ag._arm_interrupted_resume.assert_called_once()
    refs = ag._arm_interrupted_resume.call_args.args[0]
    # refs 按 [:8] 截断（与 completion.py stall 补偿同构）
    assert refs == ["t-claime", "t-runnin"], "只认 assignee 义务"


async def test_llm_error_no_inbox_no_open_work_stays_quiet():
    """断流 + 无未读 + 名下无活（真收工后断流）→ 不挂补偿保持安静."""
    from hiveweave.agents import recovery as recovery_mod

    ag = _err_agent()
    with patch(
        "hiveweave.services.task.TaskService.get_open_work_obligations",
        AsyncMock(return_value=[]),
    ):
        await recovery_mod.handle_error(ag, RuntimeError("HTTP 503"))
    ag._arm_interrupted_resume.assert_not_called()
    ag._go_idle.assert_awaited_once()


async def test_llm_error_with_inbox_uses_checkpoint_path():
    """有未读 inbox → 走既有 checkpoint resume（不进闭式补偿分支）."""
    from hiveweave.agents import recovery as recovery_mod

    ag = _err_agent(pending_inbox_msg_ids=["m1", "m2"])
    with patch(
        "hiveweave.services.task.TaskService.get_open_work_obligations",
        AsyncMock(),
    ) as mock_open:
        await recovery_mod.handle_error(ag, RuntimeError("HTTP 503"))
    ag._write_resume_checkpoint.assert_awaited_once()
    mock_open.assert_not_awaited()
    ag._arm_interrupted_resume.assert_not_called()


async def test_llm_error_over_threshold_no_inbox_skips_resume():
    """超连续错误阈值 + 无 inbox → 走升级分支（elif），不进闭式补偿（else）.

    钉死 elif/else 互斥：超阈值 = give-up 语义（suppressed + 升级），
    不再挂 45s 补偿。变异：elif 链顺序调错 → 本测试失败."""
    from hiveweave.agents import recovery as recovery_mod

    ag = _err_agent(_consecutive_errors=4)  # > _CONSECUTIVE_ERROR_MAX=3
    with patch(
        "hiveweave.services.task.TaskService.get_open_work_obligations",
        AsyncMock(),
    ) as mock_open:
        await recovery_mod.handle_error(ag, RuntimeError("HTTP 503"))
    mock_open.assert_not_awaited()
    ag._arm_interrupted_resume.assert_not_called()
    ag._arm_resume_suppressed.assert_called_once()
    ag._escalate_turn_interruption.assert_awaited_once()


async def test_interrupted_resume_fire_skips_parked_dispositions():
    """_fire 守卫扩全停泊 disposition：waiting_*/blocked/complete 不补偿唤醒.

    变异：守卫换回 == "blocked" → waiting_agent 用例失败（审计 B/C 项）。
    用真实事件循环 + 0 延迟驱动 timer（call_later 需 running loop）。"""
    import asyncio

    from hiveweave.agents.agent import Agent, AgentState

    for disp in ("waiting_agent", "waiting_human", "blocked", "complete"):
        ag = object.__new__(Agent)
        ag.id = f"ag-park-{disp}"
        ag.status = AgentState.IDLE
        ag.disposition = disp
        ag._interrupted_resume_timer = None
        ag._INTERRUPTED_RESUME_DELAY_S = 0.0
        fired: list = []

        async def _noop_chat(*a, **k):
            fired.append(disp)

        ag.chat = _noop_chat
        ag._arm_interrupted_resume(["t-1"])
        assert ag._interrupted_resume_timer is not None
        await asyncio.sleep(0.05)  # 让 0 延迟 timer 触发
        assert fired == [], f"停泊 disposition {disp} 不得被补偿唤醒"


# ── completion.py: stall_break 补偿扩 claimed ──────────────


def test_stall_break_resume_includes_claimed():
    """stall_break 补偿范围对齐 ADR-001 闭式口径（assignee 负空间）.

    变异: 换回 ``list_tasks + in ("running","claimed")`` 窄口径 → 本测试
    失败（负空间判定被 replace 后 claimed/负空间语义丢失，审石 E4 停摆案例）。"""
    from hiveweave.agents import completion as completion_mod

    src = inspect.getsource(completion_mod)
    # 8b 段必须走 ADR-001 闭式判定 + assignee 过滤（与断流补偿同源）
    assert "TaskService().get_open_work_obligations(" in src
    assert 't.get("assignee_id") == agent.id' in src
    # 旧窄口径（list_tasks + running/claimed）不得残留
    assert 'in ("running", "claimed")' not in src


# ── game_time.py: 看门狗节奏 env 化 ────────────────────────


def test_watchdog_thresholds_env_tunable(monkeypatch):
    """SILENCE/STALL 节奏常量必须读 env（变异：换回硬编码 → 失败）."""
    monkeypatch.setenv("HIVEWEAVE_SILENCE_THRESHOLD_MS", "120000")
    monkeypatch.setenv("HIVEWEAVE_STALL_COOLDOWN_MS", "300000")
    monkeypatch.setenv("HIVEWEAVE_SILENCE_NOTIFY_MS", "60000")
    import importlib

    from hiveweave.services import game_time as gt

    importlib.reload(gt)
    try:
        assert gt.SILENCE_THRESHOLD_MS == 120_000
        assert gt.STALL_COOLDOWN_MS == 300_000
        assert gt.SILENCE_NOTIFY_MS == 60_000
    finally:
        monkeypatch.delenv("HIVEWEAVE_SILENCE_THRESHOLD_MS")
        monkeypatch.delenv("HIVEWEAVE_STALL_COOLDOWN_MS")
        monkeypatch.delenv("HIVEWEAVE_SILENCE_NOTIFY_MS")
        importlib.reload(gt)
