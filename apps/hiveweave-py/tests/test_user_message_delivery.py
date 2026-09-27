"""deliver_user_message 与 phoenix 通道映射测试（spec §4.1 对话桥协议）。

单元级（打桩 ChatMessageService / InboxService / off_duty），覆盖：
- idle：先落库（metadata.source 原样写入）→ on_user_saved ack →
  agent.chat(BUG-036 信封)；图片经 parse_user_images 喂模型
- busy：落库 + InboxService 排队（from=用户，trusted_platform=True），
  不调 chat
- agent 未运行 + 下班：落库 + 下班自动回复 → outcome=off_duty
- agent 未运行 + 未下班：outcome=not_found
- chat() 返回 paused / project_not_started / busy 的结果映射
- phoenix ``_handle_chat_push``：各 outcome → wire 事件映射 +
  insert 模式仍走 steer（不经共用入口）
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from hiveweave.agents.supervisor import agent_manager
from hiveweave.services.chat_message import ChatMessageService
from hiveweave.services.inbox import InboxService

import pytest

from hiveweave.services import user_message as um


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _Agent:
    def __init__(self, status: str = "idle", chat_result: dict | None = None):
        self.id = "agent-1"
        self.project_id = "proj-1"
        self.status = _Status(status)
        self.chat = AsyncMock(return_value=chat_result or {"ok": True})
        self.steer = AsyncMock(return_value={"ok": True, "steer": True})
        self._on_stream_event = object()  # 已挂回调，避免走补丁分支
        self._on_status_change = object()


@pytest.fixture
def saved_calls():
    calls: list[dict] = []

    async def _save(self, attrs):
        calls.append(attrs)
        return {
            "id": f"msg-{len(calls)}",
            "role": attrs.get("role"),
            "content": attrs.get("content"),
            "created_at": 1,
        }

    with patch.object(ChatMessageService, "save_message", _save):
        yield calls


async def test_idle_saves_with_source_then_acks_then_chats(saved_calls):
    agent = _Agent("idle")
    order: list[str] = []

    async def _ack(saved):
        order.append(f"ack:{saved['id']}")

    async def _chat(msg, opts=None):
        order.append("chat")
        return {"ok": True}

    agent.chat = AsyncMock(side_effect=_chat)
    with patch.object(agent_manager, "get_agent", return_value=agent):
        result = await um.deliver_user_message(
            "agent-1", "hello", "ball", on_user_saved=_ack
        )

    assert result["ok"] is True
    assert result["outcome"] == "started"
    assert result["user_message_id"] == "msg-1"
    # 落库 metadata.source 原样写入（三端来源可辨）
    assert saved_calls[0]["metadata"] == {"source": "ball"}
    assert saved_calls[0]["role"] == "user"
    assert saved_calls[0]["content"] == "hello"
    # 时序契约：ack 先于 chat（前端要先拿到 message_id 再收 delta）
    assert order == ["ack:msg-1", "chat"]
    envelope = json.loads(agent.chat.await_args.args[0])
    assert envelope == {"from": "用户", "content": "hello"}


async def test_idle_with_images_passes_parsed_images(saved_calls):
    agent = _Agent("idle")
    fake_imgs = [{"kind": "image", "data": "x"}]
    with (
        patch.object(agent_manager, "get_agent", return_value=agent),
        patch(
            "hiveweave.services.vision.parse_user_images",
            return_value=fake_imgs,
        ),
    ):
        result = await um.deliver_user_message(
            "agent-1", "看图", "web", images=["data:image/png;base64,AAA"]
        )
    assert result["outcome"] == "started"
    assert saved_calls[0]["images"] == ["data:image/png;base64,AAA"]
    # I15(P2-5)：共用入口显式报 source="chat"（缺省不许落 unknown）
    assert agent.chat.await_args.args[1] == {
        "source": "chat",
        "images": fake_imgs,
    }


async def test_busy_queues_via_inbox_and_skips_chat(saved_calls):
    agent = _Agent("processing")
    send = AsyncMock(return_value={"id": "inbox-1"})
    with (
        patch.object(agent_manager, "get_agent", return_value=agent),
        patch.object(InboxService, "send_message", send),
    ):
        result = await um.deliver_user_message("agent-1", "busy msg", "ball")

    assert result["ok"] is True
    assert result["outcome"] == "queued"
    assert result["user_message_id"] == "msg-1"
    agent.chat.assert_not_awaited()
    send.assert_awaited_once()
    kw = send.await_args.kwargs
    assert kw["from_agent_id"] == "用户"
    assert kw["to_agent_id"] == "agent-1"
    assert kw["message"] == "busy msg"  # 纯文本，不预包信封
    assert kw["message_type"] == "user_message"
    assert kw["trusted_platform"] is True
    assert saved_calls[0]["metadata"] == {"source": "ball"}


async def test_agent_missing_off_duty_auto_replies(saved_calls):
    asst = {"id": "asst-1", "role": "assistant", "content": "我已下班", "created_at": 1}
    with (
        patch.object(agent_manager, "get_agent", return_value=None),
        patch(
            "hiveweave.services.off_duty.is_agent_off_duty",
            AsyncMock(return_value=True),
        ),
        patch(
            "hiveweave.services.off_duty.send_off_duty_auto_reply",
            AsyncMock(return_value=asst),
        ),
    ):
        result = await um.deliver_user_message("agent-1", "在吗", "web")

    assert result["ok"] is True
    assert result["outcome"] == "off_duty"
    assert result["user_message_id"] == "msg-1"
    assert result["assistant_message_id"] == "asst-1"
    assert result["assistant_content"] == "我已下班"
    assert saved_calls[0]["metadata"] == {"source": "web"}


async def test_agent_missing_not_off_duty_is_not_found(saved_calls):
    with (
        patch.object(agent_manager, "get_agent", return_value=None),
        patch(
            "hiveweave.services.off_duty.is_agent_off_duty",
            AsyncMock(return_value=False),
        ),
    ):
        result = await um.deliver_user_message("agent-1", "在吗", "web")
    assert result["ok"] is False
    assert result["outcome"] == "not_found"
    assert saved_calls == []  # 不留 orphan 用户消息


@pytest.mark.parametrize(
    "chat_result, outcome, ok",
    [
        ({"error": "paused"}, "paused", False),
        ({"error": "busy"}, "busy", False),
        ({"ok": True, "queued": True, "held": True}, "started", True),
    ],
)
async def test_chat_result_mapping(saved_calls, chat_result, outcome, ok):
    agent = _Agent("idle", chat_result=chat_result)
    with patch.object(agent_manager, "get_agent", return_value=agent):
        result = await um.deliver_user_message("agent-1", "x", "web")
    assert result["outcome"] == outcome
    assert result["ok"] is ok


async def test_project_not_started_sends_off_duty_reply(saved_calls):
    agent = _Agent("idle", chat_result={"error": "project_not_started"})
    asst = {"id": "asst-2", "role": "assistant", "content": "我已下班", "created_at": 1}
    with (
        patch.object(agent_manager, "get_agent", return_value=agent),
        patch(
            "hiveweave.services.off_duty.send_off_duty_auto_reply",
            AsyncMock(return_value=asst),
        ),
    ):
        result = await um.deliver_user_message("agent-1", "x", "ball")
    assert result["outcome"] == "off_duty"
    assert result["ok"] is True
    assert result["user_message_id"] == "msg-1"  # 用户消息已落库
    assert result["assistant_message_id"] == "asst-2"


# ── phoenix 通道映射 ─────────────────────────────────────────


def _sent(send_calls: list) -> list[tuple[str, dict]]:
    return [(frame[3], frame[4]) for frame in send_calls]


async def _run_phoenix(payload: dict, deliver_result: dict, agent=None):
    from hiveweave.realtime import phoenix_adapter as pa

    send_calls: list = []

    async def send_fn(frame):
        send_calls.append(frame)

    deliver = AsyncMock(return_value=deliver_result)

    async def _deliver_with_ack(agent_id, content, source, *, images=None, on_user_saved=None):
        # 模拟共用入口在落库后回调 ack
        if on_user_saved is not None and deliver_result.get("user_message_id"):
            await on_user_saved({"id": deliver_result["user_message_id"]})
        return await deliver(agent_id, content, source, images=images, on_user_saved=on_user_saved)

    with (
        patch(
            "hiveweave.services.user_message.deliver_user_message",
            _deliver_with_ack,
        ),
        patch(
            "hiveweave.agents.supervisor.agent_manager.get_agent",
            return_value=agent,
        ),
    ):
        await pa._handle_chat_push("agent:agent-1", payload, send_fn)
    return send_calls, deliver


async def test_phoenix_started_acks_user_message_with_source_web():
    send_calls, deliver = await _run_phoenix(
        {"message": "hi"},
        {"ok": True, "outcome": "started", "user_message_id": "u1",
         "assistant_message_id": None, "assistant_content": None, "error": None},
        agent=_Agent("idle"),
    )
    deliver.assert_awaited_once()
    args = deliver.await_args
    assert args.args[:3] == ("agent-1", "hi", "web")  # 网页面 source=web
    events = _sent(send_calls)
    assert events == [
        ("message_id", {"id": "u1", "agentId": "agent-1", "role": "user"}),
    ]


async def test_phoenix_queued_maps_to_queued_message():
    send_calls, _ = await _run_phoenix(
        {"message": "hi"},
        {"ok": True, "outcome": "queued", "user_message_id": "u1",
         "assistant_message_id": None, "assistant_content": None, "error": None},
        agent=_Agent("processing"),
    )
    kinds = [k for k, _ in _sent(send_calls)]
    assert kinds == ["message_id", "queued_message"]


async def test_phoenix_off_duty_sends_assistant_message_id():
    send_calls, _ = await _run_phoenix(
        {"message": "hi"},
        {"ok": True, "outcome": "off_duty", "user_message_id": "u1",
         "assistant_message_id": "a1", "assistant_content": "我已下班", "error": None},
        agent=None,
    )
    events = _sent(send_calls)
    assert events[0][0] == "message_id" and events[0][1]["role"] == "user"
    assert events[1] == (
        "message_id",
        {"id": "a1", "agentId": "agent-1", "role": "assistant", "content": "我已下班"},
    )


@pytest.mark.parametrize(
    "outcome, expected_event, expected_msg",
    [
        ("not_found", "error", "Agent not running"),
        ("busy", "busy", "Agent is busy"),
        ("paused", "error", "System is paused"),
    ],
)
async def test_phoenix_error_outcomes(outcome, expected_event, expected_msg):
    send_calls, _ = await _run_phoenix(
        {"message": "hi"},
        {"ok": False, "outcome": outcome, "user_message_id": None,
         "assistant_message_id": None, "assistant_content": None, "error": expected_msg},
        agent=None,
    )
    events = _sent(send_calls)
    assert events[-1][0] == expected_event
    assert events[-1][1]["message"] == expected_msg


async def test_phoenix_insert_mode_steers_without_shared_entry(saved_calls):
    """插话（busy + mode=insert）保留在通道层：steer，不经 deliver。"""
    from hiveweave.realtime import phoenix_adapter as pa

    agent = _Agent("processing")
    send_calls: list = []

    async def send_fn(frame):
        send_calls.append(frame)

    deliver = AsyncMock()
    with (
        patch("hiveweave.services.user_message.deliver_user_message", deliver),
        patch("hiveweave.agents.supervisor.agent_manager.get_agent", return_value=agent),
    ):
        await pa._handle_chat_push(
            "agent:agent-1", {"message": "插一句", "mode": "insert"}, send_fn
        )

    deliver.assert_not_awaited()
    agent.steer.assert_awaited_once()
    kinds = [k for k, _ in _sent(send_calls)]
    assert kinds == ["message_id", "inserted"]
    assert saved_calls[0]["metadata"] == {"source": "web"}
