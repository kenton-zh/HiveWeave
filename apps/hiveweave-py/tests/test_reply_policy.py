"""Tests for reply_policy hard rules + soft expect_report detection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.services.reply_policy import (
    message_requests_reply,
    resolve_expect_report,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("工具可用性验证。请依次执行检查并回复结果。", True),
        ("回复格式：工具名 + 结果。有任何失败立即报告。", True),
        ("Please reply with the test results.", True),
        ("report back when done", True),
        ("招聘完成，7人全部到位。", False),
        ("模块划分批准，你直接向天线发送招聘请求。", False),
        ("", False),
        (None, False),
    ],
)
def test_message_requests_reply(text, expected):
    assert message_requests_reply(text) is expected


def test_resolve_expect_report_explicit_and_heuristic():
    assert resolve_expect_report(True, "随便看看") is True
    assert resolve_expect_report(False, "请回复结果") is True
    assert resolve_expect_report(None, "请回复结果") is True
    assert resolve_expect_report(False, "FYI 已完成招聘") is False
    assert resolve_expect_report(None, "FYI 已完成招聘") is False


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
async def test_unreplied_detects_soft_expect_without_flag():
    """CEO forgot expect_report=1 but text asks for 回复 → still unreplied."""
    ag = _make_agent()
    ag._inbox.get_pending_messages = AsyncMock(
        return_value=[
            {
                "id": "msg-1",
                "from_agent_id": "ceo-id",
                "message": "工具可用性验证。请依次执行并回复结果。",
                "expect_report": 0,
            }
        ]
    )

    with patch(
        "hiveweave.agents.agent.meta_db.get_agent_by_id",
        new=AsyncMock(return_value={"name": "归零"}),
    ):
        unreplied = await ag._check_unreplied_expect_report(tool_calls=[])

    assert len(unreplied) == 1
    assert unreplied[0]["from_name"] == "归零"


@pytest.mark.asyncio
async def test_unreplied_cleared_when_send_message_to_sender():
    ag = _make_agent()
    ag._inbox.get_pending_messages = AsyncMock(
        return_value=[
            {
                "id": "msg-1",
                "from_agent_id": "ceo-id",
                "message": "请回复结果",
                "expect_report": 1,
            }
        ]
    )
    tool_calls = [
        {
            "function": {
                "name": "send_message",
                "arguments": json.dumps(
                    {"recipients": ["归零"], "message": "验证通过"}
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
