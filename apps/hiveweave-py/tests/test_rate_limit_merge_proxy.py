"""429 skip give-up + rate-limit resume cooldown."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.agent import (
    RATE_LIMIT_RESUME_COOLDOWN_S,
    is_rate_limit_error,
)
from hiveweave.llm.retry import RetryableError


def test_is_rate_limit_error_variants():
    assert is_rate_limit_error(RetryableError("quota", status=429))
    assert is_rate_limit_error(ValueError("AccountRateLimitExceeded"))
    assert is_rate_limit_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not is_rate_limit_error(RetryableError("busy", status=503))
    assert not is_rate_limit_error(ValueError("boom"))


@pytest.mark.asyncio
async def test_handle_error_rate_limit_no_consecutive():
    from hiveweave.agents.agent import Agent

    agent = Agent.__new__(Agent)
    agent.id = "a1"
    agent.project_id = "p1"
    agent.config = {}
    agent._consecutive_errors = 0
    agent._stream_timeout_streak = 0
    agent._CONSECUTIVE_ERROR_MAX = 3
    agent.pending_inbox_msg_ids = ["m1"]
    agent._streaming_msg_id = None
    agent._work_log = MagicMock()
    agent._work_log.write_work_log = AsyncMock()
    agent._chat_msg = MagicMock()
    agent._chat_msg.save_message = AsyncMock()
    agent._finalize_streaming_turn = AsyncMock()
    agent._write_resume_checkpoint = AsyncMock()
    agent._arm_resume_cooldown = MagicMock()
    agent._arm_resume_suppressed = MagicMock()
    agent._cancel_safety_timer = MagicMock()
    agent._go_idle = AsyncMock()
    agent._broadcast_stream_event = MagicMock()
    agent._broadcast_agent_health = MagicMock()
    agent._ack_inbox_on_give_up = AsyncMock()
    agent._escalate_turn_interruption = AsyncMock()
    agent._inject_ledger_review_wake = AsyncMock()

    with patch("hiveweave.services.event_audit.event_audit") as ea:
        ea.log = AsyncMock()
        for _ in range(5):
            agent.pending_inbox_msg_ids = ["m1"]
            await agent._handle_error(
                RetryableError("AccountRateLimitExceeded", status=429)
            )

    assert agent._consecutive_errors == 0
    agent._arm_resume_suppressed.assert_not_called()
    agent._ack_inbox_on_give_up.assert_not_called()
    assert agent._arm_resume_cooldown.call_count == 5
    agent._arm_resume_cooldown.assert_called_with(RATE_LIMIT_RESUME_COOLDOWN_S)


@pytest.mark.asyncio
async def test_handle_error_normal_still_counts():
    from hiveweave.agents.agent import Agent

    agent = Agent.__new__(Agent)
    agent.id = "a2"
    agent.project_id = "p1"
    agent.config = {}
    agent._consecutive_errors = 0
    agent._stream_timeout_streak = 0
    agent._CONSECUTIVE_ERROR_MAX = 3
    agent.pending_inbox_msg_ids = ["m1"]
    agent._streaming_msg_id = None
    agent._work_log = MagicMock()
    agent._work_log.write_work_log = AsyncMock()
    agent._chat_msg = MagicMock()
    agent._chat_msg.save_message = AsyncMock()
    agent._finalize_streaming_turn = AsyncMock()
    agent._write_resume_checkpoint = AsyncMock()
    agent._arm_resume_cooldown = MagicMock()
    agent._arm_resume_suppressed = MagicMock()
    agent._cancel_safety_timer = MagicMock()
    agent._go_idle = AsyncMock()
    agent._broadcast_stream_event = MagicMock()
    agent._broadcast_agent_health = MagicMock()
    agent._ack_inbox_on_give_up = AsyncMock()
    agent._escalate_turn_interruption = AsyncMock()
    agent._inject_ledger_review_wake = AsyncMock()

    with patch("hiveweave.services.event_audit.event_audit") as ea:
        ea.log = AsyncMock()
        for _ in range(4):
            agent.pending_inbox_msg_ids = ["m1"]
            await agent._handle_error(ValueError("boom"))

    assert agent._consecutive_errors == 4
    agent._arm_resume_suppressed.assert_called()
    agent._ack_inbox_on_give_up.assert_called()
