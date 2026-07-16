"""Tests for off-duty auto-reply when project is not started."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.off_duty import (
    OFF_DUTY_REPLY,
    is_agent_off_duty,
    is_project_off_duty,
    send_off_duty_auto_reply,
)


@pytest.mark.asyncio
async def test_is_project_off_duty_when_not_started():
    with patch(
        "hiveweave.services.off_duty.meta_db.query_one",
        new=AsyncMock(return_value={"is_started": 0}),
    ):
        assert await is_project_off_duty("proj-1") is True


@pytest.mark.asyncio
async def test_is_project_off_duty_when_started():
    with patch(
        "hiveweave.services.off_duty.meta_db.query_one",
        new=AsyncMock(return_value={"is_started": 1}),
    ):
        assert await is_project_off_duty("proj-1") is False


@pytest.mark.asyncio
async def test_is_project_off_duty_missing_project():
    with patch(
        "hiveweave.services.off_duty.meta_db.query_one",
        new=AsyncMock(return_value=None),
    ):
        assert await is_project_off_duty("missing") is True
    assert await is_project_off_duty(None) is True


@pytest.mark.asyncio
async def test_is_agent_off_duty_resolves_project():
    with (
        patch(
            "hiveweave.services.off_duty.meta_db.get_agent_project_id",
            new=AsyncMock(return_value="proj-1"),
        ),
        patch(
            "hiveweave.services.off_duty.meta_db.query_one",
            new=AsyncMock(return_value={"is_started": 0}),
        ),
    ):
        assert await is_agent_off_duty("agent-1") is True


@pytest.mark.asyncio
async def test_send_off_duty_auto_reply_saves_and_broadcasts():
    saved = {
        "id": "msg-asst",
        "role": "assistant",
        "content": OFF_DUTY_REPLY,
        "created_at": 1,
    }
    mock_bus = AsyncMock()
    mock_save = AsyncMock(return_value=saved)
    with (
        patch("hiveweave.services.off_duty.ChatMessageService") as MockChat,
        patch(
            "hiveweave.realtime.event_bus.status_event_bus",
            mock_bus,
        ),
    ):
        MockChat.return_value.save_message = mock_save
        result = await send_off_duty_auto_reply("agent-1")

    assert result == saved
    mock_save.assert_awaited_once()
    call_attrs = mock_save.await_args.args[0]
    assert call_attrs["role"] == "assistant"
    assert call_attrs["content"] == OFF_DUTY_REPLY
    assert call_attrs["agent_id"] == "agent-1"

    assert mock_bus.publish.await_count == 1
    assert mock_bus.publish.await_args.args[1]["type"] == "message_id"
    assert mock_bus.publish_stream_event.await_count == 2
    types = [
        c.args[1]["type"] for c in mock_bus.publish_stream_event.await_args_list
    ]
    assert types == ["text_delta", "done"]
    assert mock_bus.publish_stream_event.await_args_list[0].args[1]["content"] == (
        OFF_DUTY_REPLY
    )


def test_off_duty_reply_text():
    assert "下班" in OFF_DUTY_REPLY
    assert "😊" in OFF_DUTY_REPLY
