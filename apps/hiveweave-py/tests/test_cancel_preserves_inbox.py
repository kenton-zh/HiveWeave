"""cancel() must not ACK pending inbox (TEST6 agent-switch false cancel).

If cancel marks inbox read, a mistaken WS cancel (e.g. UI agent switch with
stale streamAbortRef) permanently drops the wake signal. Align with
recovery.handle_cancel: preserve unread. Revive watcher ONLY when a non-empty
pending claim was preserved (not on every cancel — avoids Stop 不停 / orphans).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.services.project_lifecycle import OFF_DUTY_CANCEL_REASON


PROJECT_ID = "cancel-inbox-project"
AGENT_ID = "cancel-inbox-agent"


def _make_agent(
    *,
    with_hanging_task: bool = False,
    pending: list[str] | None = None,
    status: AgentState = AgentState.PROCESSING,
) -> Agent:
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "CEO", "role": "ceo"}
    agent.status = status
    agent.empty_retry_count = 0
    agent.pending_inbox_msg_ids = pending
    agent.current_job = None
    agent._cancel_reason = None
    agent._message_queue = []
    agent._streaming_msg_id = "stream-1" if status == AgentState.PROCESSING else None
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
    agent._current_run_id = None
    agent._safety_timer = None
    agent._on_status_change = None
    agent._on_stream_event = None
    agent._stop_watcher = False
    agent._inbox_watcher_task = None
    agent._lock = asyncio.Lock()
    agent._conversation = AsyncMock()
    agent._inbox = AsyncMock()
    agent._org = AsyncMock()
    agent._chat_msg = AsyncMock()
    agent._work_log = AsyncMock()
    agent._run_ledger = AsyncMock()
    agent._finalize_streaming_turn = AsyncMock(return_value=True)
    agent._cancel_safety_timer = MagicMock()
    agent._ensure_watcher_alive = MagicMock()
    agent._broadcast_status = MagicMock()

    async def _fake_handle_cancel() -> None:
        agent.status = AgentState.IDLE
        agent._cancel_reason = None
        agent._llm_task = None

    agent._handle_cancel = AsyncMock(side_effect=_fake_handle_cancel)

    if with_hanging_task:

        async def _hang() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                reason = agent._cancel_reason or "unknown"
                assert reason == "cancelled", f"expected cancelled, got {reason!r}"
                await agent._handle_cancel()

        agent._llm_task = asyncio.create_task(_hang())
    else:
        agent._llm_task = None
    return agent


@pytest.mark.asyncio
async def test_cancel_does_not_ack_pending_inbox_with_running_task():
    agent = _make_agent(
        with_hanging_task=True, pending=["inbox-hr-reply"]
    )
    # Let the hanging task reach await sleep before cancel, otherwise the
    # CancelledError path never runs and _handle_cancel is skipped.
    await asyncio.sleep(0)
    await agent.cancel(reason="cancelled")

    agent._inbox.mark_read_by_ids.assert_not_awaited()
    # Non-empty pending preserved → revive once for re-claim.
    agent._ensure_watcher_alive.assert_called()
    assert agent.pending_inbox_msg_ids is None
    agent._handle_cancel.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_does_not_ack_when_no_llm_task():
    agent = _make_agent(
        with_hanging_task=False, pending=["inbox-hr-reply"]
    )
    await agent.cancel(reason="cancelled")

    agent._inbox.mark_read_by_ids.assert_not_awaited()
    agent._ensure_watcher_alive.assert_called()
    assert agent.status == AgentState.IDLE
    assert agent.pending_inbox_msg_ids is None


@pytest.mark.asyncio
async def test_cancel_without_pending_does_not_revive_watcher():
    """Intentional Stop with no inbox claim must not auto-retrigger."""
    agent = _make_agent(with_hanging_task=False, pending=None)
    await agent.cancel(reason="cancelled")

    agent._ensure_watcher_alive.assert_not_called()
    assert agent._stop_watcher is True


@pytest.mark.asyncio
async def test_cancel_empty_pending_list_does_not_revive():
    agent = _make_agent(with_hanging_task=False, pending=[])
    await agent.cancel(reason="cancelled")

    agent._ensure_watcher_alive.assert_not_called()
    assert agent.pending_inbox_msg_ids is None


@pytest.mark.asyncio
async def test_off_duty_cancel_preserves_unread_but_does_not_revive():
    agent = _make_agent(
        with_hanging_task=False, pending=["inbox-1"]
    )
    await agent.cancel(reason=OFF_DUTY_CANCEL_REASON)

    agent._inbox.mark_read_by_ids.assert_not_awaited()
    agent._ensure_watcher_alive.assert_not_called()
    assert agent._stop_watcher is True
    assert agent.pending_inbox_msg_ids is None


@pytest.mark.asyncio
async def test_stop_agent_leaves_watcher_dead():
    """supervisor.stop_agent → cancel without pending must not orphan-revive."""
    from hiveweave.agents.supervisor import AgentManager

    agent = _make_agent(with_hanging_task=False, pending=None)
    # Simulate a live watcher task that cancel() will kill.
    async def _noop_watch():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    agent._inbox_watcher_task = asyncio.create_task(_noop_watch())
    agent._stop_watcher = False
    await asyncio.sleep(0)

    supervisor = AgentManager()
    supervisor._agents[AGENT_ID] = agent
    await supervisor.stop_agent(AGENT_ID)

    assert AGENT_ID not in supervisor._agents
    assert agent._stop_watcher is True
    assert agent._inbox_watcher_task is None or agent._inbox_watcher_task.done()
    agent._ensure_watcher_alive.assert_not_called()
