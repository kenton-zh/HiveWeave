"""Tests for off-duty auto-reply when project is not started."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.off_duty import (
    OFF_DUTY_NOTICE_PREFIX,
    OFF_DUTY_NOTICE_REPLY,
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
        "content": OFF_DUTY_NOTICE_REPLY,
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
    assert call_attrs["content"] == OFF_DUTY_NOTICE_REPLY
    assert call_attrs["agent_id"] == "agent-1"

    assert mock_bus.publish.await_count == 1
    assert mock_bus.publish.await_args.args[1]["type"] == "message_id"
    assert mock_bus.publish_stream_event.await_count == 2
    types = [
        c.args[1]["type"] for c in mock_bus.publish_stream_event.await_args_list
    ]
    assert types == ["text_delta", "done"]
    assert mock_bus.publish_stream_event.await_args_list[0].args[1]["content"] == (
        OFF_DUTY_NOTICE_REPLY
    )


def test_off_duty_reply_text():
    assert "下班" in OFF_DUTY_REPLY
    assert "😊" in OFF_DUTY_REPLY


@pytest.mark.asyncio
async def test_off_duty_reply_is_platform_notice_not_assistant_turn():
    """T13（78 实测）：下班自动应答落库不得带「已处理」语义。

    kickoff 同秒撞上下班态时，该应答曾以纯友好文案 role=assistant 落库，
    后续轮次把原始规格误读成「已关闭轮次」的事。契约：
    - content 带 ``[SYSTEM NOTICE …]`` 前缀，显式声明「平台自动应答、
      未进入处理、需重新发送」——任何后续读者不会当成 AI 处理结果；
    - metadata.platform_notice=True 机器可辨标记（role 仍为 assistant，
      UI 主栏谓词依赖 role IN ('user','assistant')，改 role 会让气泡消失）；
    - 原友好文案原样保留（前缀之后）。
    """
    saved = {
        "id": "msg-asst",
        "role": "assistant",
        "content": OFF_DUTY_NOTICE_REPLY,
        "created_at": 1,
    }
    mock_save = AsyncMock(return_value=saved)
    with (
        patch("hiveweave.services.off_duty.ChatMessageService") as MockChat,
        patch("hiveweave.realtime.event_bus.status_event_bus", AsyncMock()),
    ):
        MockChat.return_value.save_message = mock_save
        await send_off_duty_auto_reply("agent-1")

    attrs = mock_save.await_args.args[0]
    assert attrs["content"].startswith(OFF_DUTY_NOTICE_PREFIX)
    assert "未进入处理" in attrs["content"]
    assert "重新发送" in attrs["content"]
    assert attrs["content"].endswith(OFF_DUTY_REPLY)  # 原文案原样保留
    assert attrs["metadata"]["platform_notice"] is True
    assert attrs["metadata"]["kind"] == "off_duty_auto_reply"
    assert attrs["metadata"]["source"] == "system"
    assert attrs["role"] == "assistant"  # UI 主栏可见性契约不变


def test_off_duty_notice_prefix_declares_unprocessed():
    """前缀自证：必须含 SYSTEM NOTICE 标记 + 未处理声明 + 下一步处方。"""
    assert OFF_DUTY_NOTICE_PREFIX.startswith("[SYSTEM NOTICE")
    assert OFF_DUTY_NOTICE_PREFIX.endswith("]")
    assert "不是 AI" in OFF_DUTY_NOTICE_PREFIX
