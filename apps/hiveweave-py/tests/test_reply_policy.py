"""Tests for reply_policy — structured expect_report only (language-agnostic)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.services.reply_policy import (
    message_requests_reply,
    resolve_expect_report,
)


def test_message_requests_reply_never_scans_text():
    """Free-text must never imply reply-need (any language)."""
    assert message_requests_reply("请依次执行检查并回复结果。") is False
    assert message_requests_reply("Please reply with the test results.") is False
    assert message_requests_reply("report back when done") is False
    assert message_requests_reply("招聘完成") is False
    assert message_requests_reply("") is False
    assert message_requests_reply(None) is False


def test_resolve_expect_report_explicit_only():
    assert resolve_expect_report(True, "anything") is True
    assert resolve_expect_report(False, "请回复结果") is False
    assert resolve_expect_report(None, "Please reply") is False
    assert resolve_expect_report(False, "FYI") is False


def _make_agent() -> Agent:
    with patch.object(Agent, "__init__", lambda self, *a, **k: None):
        ag = Agent.__new__(Agent)
    ag.id = "agent-exec"
    ag.project_id = "proj"
    ag.config = {"role": "developer", "name": "拾柒"}
    ag.status = AgentState.IDLE
    ag.pending_inbox_msg_ids = ["msg-1"]
    ag._reply_reminder_count = 0
    ag._REPLY_REMINDER_MAX = 2
    ag._inbox = MagicMock()
    return ag


@pytest.mark.asyncio
async def test_unreplied_ignores_soft_text_without_flag():
    """Without expect_report / ask type, free-text is not an obligation."""
    ag = _make_agent()
    ag._inbox.get_pending_messages = AsyncMock(
        return_value=[
            {
                "id": "msg-1",
                "from_agent_id": "ceo-id",
                "message": "工具可用性验证。请依次执行并回复结果。",
                "expect_report": 0,
                "message_type": "normal",
            }
        ]
    )

    with patch(
        "hiveweave.agents.agent.meta_db.get_agent_by_id",
        new=AsyncMock(return_value={"name": "归零"}),
    ):
        unreplied = await ag._check_unreplied_expect_report(tool_calls=[])

    assert unreplied == []


@pytest.mark.asyncio
async def test_unreplied_cleared_when_send_message_to_sender():
    ag = _make_agent()
    ag._inbox.get_pending_messages = AsyncMock(
        return_value=[
            {
                "id": "msg-1",
                "from_agent_id": "ceo-id",
                "message": "any language body",
                "expect_report": 1,
                "message_type": "ask",
            }
        ]
    )
    tool_calls = [
        {
            "function": {
                "name": "send_message",
                "arguments": json.dumps(
                    {"recipients": ["归零"], "message": "ok"}
                ),
            }
        }
    ]

    with patch(
        "hiveweave.agents.agent.meta_db.get_agent_by_id",
        new=AsyncMock(return_value={"name": "归零"}),
    ):
        unreplied = await ag._check_unreplied_expect_report(tool_calls)

    assert unreplied == []
