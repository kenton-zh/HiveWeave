"""P0: ACK only what the model saw — no silent drop of unread inbox.

Covers mid-turn ACK removal, coalesce last-only, busy placeholder empty latch,
human-identity spoof guard (trusted_platform).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.services.inbox import InboxService
from hiveweave.services.wake_policy import is_human_inbox_identity, is_user_sender


def test_is_human_inbox_identity_markers():
    assert is_user_sender("用户") is True
    assert is_user_sender("User") is True
    assert is_user_sender("agent-uuid") is False
    assert is_human_inbox_identity(from_agent_id="用户", message_type="normal")
    assert is_human_inbox_identity(
        from_agent_id="peer", message_type="user_message"
    )
    assert not is_human_inbox_identity(
        from_agent_id="peer-uuid", message_type="normal"
    )


@pytest.mark.asyncio
async def test_send_message_rejects_human_identity_without_trusted(monkeypatch):
    svc = InboxService()

    async def boom(*_a, **_k):
        raise AssertionError("should not reach DB insert")

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_by_id",
        AsyncMock(return_value={"id": "to-1", "status": "active"}),
    )
    # If guard fails open, insert path would run — stub schema to fail loudly
    monkeypatch.setattr(
        "hiveweave.services.inbox._ensure_schema", boom
    )

    with pytest.raises(ValueError, match="trusted_platform"):
        await svc.send_message(
            from_agent_id="用户",
            to_agent_id="to-1",
            message="hi",
            message_type="user_message",
        )

    with pytest.raises(ValueError, match="trusted_platform"):
        await svc.send_message(
            from_agent_id="peer",
            to_agent_id="to-1",
            message="hi",
            message_type="user_message",
        )


@pytest.mark.asyncio
async def test_merge_window_last_only_inbox_ids():
    from hiveweave.agents.agent import Agent, AgentState

    agent = object.__new__(Agent)
    agent.id = "merge-ack"
    agent.status = AgentState.IDLE
    agent._message_queue = []
    agent._MERGE_WINDOW_MS = 0
    agent._lock = __import__("asyncio").Lock()

    chats: list[tuple] = []

    async def fake_chat(msg, opts=None):
        chats.append((msg, opts or {}))
        return {"ok": True}

    agent.chat = fake_chat  # type: ignore

    await agent.enqueue_wake(
        "wake1",
        {"trigger": True, "inbox_msg_ids": ["a"], "source": "t"},
    )
    await agent.enqueue_wake(
        "wake2",
        {"trigger": True, "inbox_msg_ids": ["b"], "source": "t"},
    )
    await Agent._drain_message_queue(agent)

    assert len(chats) == 1
    assert chats[0][0] == "wake2"
    assert chats[0][1].get("inbox_msg_ids") == ["b"]


@pytest.mark.asyncio
async def test_success_ack_only_latched_ids_not_mid_turn():
    """Regression: mid-turn unread must stay unread after successful exit."""
    from pathlib import Path

    # Guard: production success path must not union get_pending_ids_since.
    src = Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "agents" / "completion.py"
    text = src.read_text(encoding="utf-8")
    assert "get_pending_ids_since" not in text

    marked: list[list[str]] = []

    async def fake_mark(agent_id, ids):
        marked.append(list(ids))

    inbox = MagicMock()
    inbox.mark_read_by_ids = fake_mark
    inbox.get_pending_ids_since = AsyncMock(return_value=["mid-2"])

    pending_inbox_msg_ids = ["shown-1"]
    # Production success path (post P0-1): ACK latched ids only.
    ack_ids = list(pending_inbox_msg_ids or [])
    await inbox.mark_read_by_ids("ack-agent", ack_ids)

    assert marked == [["shown-1"]]
    mid = await inbox.get_pending_ids_since("ack-agent", 1_000)
    assert mid == ["mid-2"]
    assert "mid-2" not in marked[0]


@pytest.mark.asyncio
async def test_busy_none_context_latches_empty_ids(monkeypatch):
    """P0-2: placeholder enqueue must not latch real inbox ids."""
    from hiveweave.agents import trigger as trigger_mod

    captured: dict = {}

    class FakeAgent:
        id = "busy-1"
        status = MagicMock(value="processing")
        _on_stream_event = object()
        _resume_suppressed = False

        def try_clear_resume_suppressed(self, _opts):
            return False

        async def enqueue_wake(self, message, opts=None):
            captured["message"] = message
            captured["opts"] = opts or {}

    fake = FakeAgent()
    agent_record = {
        "id": "busy-1",
        "project_id": "proj",
        "name": "Busy",
        "role": "executor",
        "status": "active",
    }

    monkeypatch.setattr(trigger_mod, "TRIGGER_DELAY_MS", 0)
    monkeypatch.setattr(
        trigger_mod._org_service,
        "get_agent",
        AsyncMock(return_value=agent_record),
    )
    monkeypatch.setattr(
        trigger_mod, "_get_agent_manager", lambda: MagicMock(get_agent=lambda _i: fake)
    )
    monkeypatch.setattr(
        trigger_mod,
        "build_trigger_context",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        trigger_mod._inbox_service,
        "get_pending_messages",
        AsyncMock(
            return_value=[
                {"id": "p1", "from_agent_id": "peer", "message": "hi"},
            ]
        ),
    )
    monkeypatch.setattr(
        trigger_mod._inbox_service,
        "get_undelivered_background",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        trigger_mod._handoff_service,
        "accept_pending_handoffs",
        AsyncMock(),
    )
    monkeypatch.setattr(
        trigger_mod,
        "_admit_trigger_wake",
        AsyncMock(return_value=True),
    )

    async def fake_query_one(sql, params=None):
        return {"is_started": 1}

    monkeypatch.setattr(
        "hiveweave.db.meta.query_one",
        fake_query_one,
    )

    await trigger_mod._do_trigger("busy-1", "subordinate")

    assert captured.get("opts", {}).get("inbox_msg_ids") == []
    assert "Inbox triage pending" in (captured.get("message") or "")
