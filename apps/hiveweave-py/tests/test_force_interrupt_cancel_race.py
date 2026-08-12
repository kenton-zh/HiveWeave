"""R1: sweep 强制中断不得劫持用户 cancel 语义的回归测试.

被测: src/hiveweave/agents/recovery.py::force_interrupt_stuck_stream

竞态背景: sweep 通过 DB 守卫后无条件覆写 agent._cancel_reason="safety_timeout"
（不持 agent 锁）。若用户刚对卡死 agent 点了 cancel()（已写入 "cancelled" /
"off_duty"），sweep 覆写后 CancelledError handler 误走 handle_safety_timeout，
用户明确取消的回合被安全超时恢复账劫持。修复后: reason 为空 → 按
safety_timeout 处理; reason 已是 safety_timeout → 幂等继续; 其他值（在途
用户取消）→ 返回 False 不打扰。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hiveweave.agents import recovery
from hiveweave.agents.types import AgentState


async def _hang() -> None:
    await asyncio.sleep(3600)


async def _await_task_quietly(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_user_cancel_inflight_not_hijacked():
    """PROCESSING + 活 LLM task + _cancel_reason="cancelled"（用户 cancel 在途）
    → 返回 False、reason 保持 "cancelled"、task 不被再次 cancel."""
    task = asyncio.create_task(_hang())
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=task, _cancel_reason="cancelled")
    try:
        ok = await recovery.force_interrupt_stuck_stream(
            agent, reason_detail="streaming quiet 6min")
        assert ok is False
        assert agent._cancel_reason == "cancelled"
        assert not task.cancelled() and not task.cancelling()
    finally:
        await _await_task_quietly(task)


@pytest.mark.asyncio
async def test_safety_timeout_inflight_idempotent_continue():
    """PROCESSING + 活 LLM task + _cancel_reason="safety_timeout"（安全超时自己
    已在途）→ 返回 True、reason 不变、task.cancel() 幂等调用."""
    task = asyncio.create_task(_hang())
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=task, _cancel_reason="safety_timeout")
    try:
        ok = await recovery.force_interrupt_stuck_stream(agent, reason_detail="t")
        assert ok is True
        assert agent._cancel_reason == "safety_timeout"
        assert task.cancelling()
    finally:
        await _await_task_quietly(task)


@pytest.mark.asyncio
async def test_desync_user_cancel_not_hijacked(monkeypatch):
    """状态脱钩（无活 task）+ reason="cancelled" → 返回 False，不调
    handle_safety_timeout."""
    mock_recovery = AsyncMock()
    monkeypatch.setattr(recovery, "handle_safety_timeout", mock_recovery)
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=None, _cancel_reason="cancelled")
    ok = await recovery.force_interrupt_stuck_stream(agent, reason_detail="t")
    assert ok is False
    assert agent._cancel_reason == "cancelled"
    mock_recovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_desync_safety_timeout_inflight_idempotent_continue(monkeypatch):
    """状态脱钩 + reason="safety_timeout" → 幂等继续走 safety_timeout 恢复."""
    mock_recovery = AsyncMock()
    monkeypatch.setattr(recovery, "handle_safety_timeout", mock_recovery)
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=None, _cancel_reason="safety_timeout")
    ok = await recovery.force_interrupt_stuck_stream(agent, reason_detail="t")
    assert ok is True
    assert agent._cancel_reason == "safety_timeout"
    mock_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_reason_empty_live_task_still_safety_timeout():
    """既有语义不回归: reason 为空 + 活 task → True + reason=="safety_timeout"
    + task 被 cancel（对齐 test_streaming_zombie 既有断言）."""
    task = asyncio.create_task(_hang())
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=task, _cancel_reason=None)
    try:
        assert not task.cancelling()
        ok = await recovery.force_interrupt_stuck_stream(
            agent, reason_detail="streaming quiet 6min")
        assert ok is True
        assert agent._cancel_reason == "safety_timeout"
        assert task.cancelling()
    finally:
        await _await_task_quietly(task)


@pytest.mark.asyncio
async def test_idempotent_branch_does_not_second_cancel():
    """幂等分支（reason="safety_timeout" + cancel 已在途）不得二次 cancel。

    二次 cancel 会在 CancelledError handler 的 await 点再抛一次
    CancelledError，把 handle_safety_timeout 中途打断（恢复账静默丢失）。
    用带 await 点的 handler 探针协程验证: handler 必须完整跑完。
    force 被安排在 handler 挂起窗口内运行，复现 sweep tick 落在
    handler 执行窗的竞态。
    """
    events: list[str] = []

    async def _handler_probe() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("handler_entered")
            await asyncio.sleep(0.02)
            events.append("handler_finished")
            raise

    task = asyncio.create_task(_handler_probe())
    await asyncio.sleep(0)
    task.cancel()
    assert task.cancelling()
    await asyncio.sleep(0)
    assert events == ["handler_entered"]
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=task, _cancel_reason="safety_timeout")
    try:
        ok = await asyncio.create_task(recovery.force_interrupt_stuck_stream(
            agent, reason_detail="streaming quiet 6min"))
        assert ok is True
        assert agent._cancel_reason == "safety_timeout"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done() and task.cancelled()
        assert events == ["handler_entered", "handler_finished"]
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_not_processing_with_inflight_cancel_still_noop():
    """非 PROCESSING + 在途 cancel → 仍 fail-open False，reason 不被触碰."""
    agent = SimpleNamespace(
        id="a1", status=AgentState.IDLE,
        _llm_task=None, _cancel_reason="cancelled")
    ok = await recovery.force_interrupt_stuck_stream(agent, reason_detail="t")
    assert ok is False
    assert agent._cancel_reason == "cancelled"
