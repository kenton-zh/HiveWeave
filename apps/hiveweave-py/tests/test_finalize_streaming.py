"""finalize_streaming_message must clear orphans when update_message returns False."""

from __future__ import annotations

import pytest

from hiveweave.services.chat_message import ChatMessageService


@pytest.mark.asyncio
async def test_finalize_falls_back_when_update_returns_false(monkeypatch):
    svc = ChatMessageService()
    calls: list[str] = []

    async def fake_update(agent_id, msg_id, attrs):
        calls.append("update")
        assert attrs.get("is_streaming") is False
        return False  # silent failure — historical zombie root cause

    async def fake_done(agent_id):
        calls.append("done")

    monkeypatch.setattr(svc, "update_message", fake_update)
    monkeypatch.setattr(svc, "update_streaming_messages_done", fake_done)

    ok = await svc.finalize_streaming_message(
        "agent-1",
        "msg-1",
        {"content": "hello"},
    )
    assert ok is True
    assert calls == ["update", "done"]


@pytest.mark.asyncio
async def test_finalize_skips_fallback_when_disabled(monkeypatch):
    svc = ChatMessageService()
    calls: list[str] = []

    async def fake_update(agent_id, msg_id, attrs):
        calls.append("update")
        return False

    async def fake_done(agent_id):
        calls.append("done")

    monkeypatch.setattr(svc, "update_message", fake_update)
    monkeypatch.setattr(svc, "update_streaming_messages_done", fake_done)

    ok = await svc.finalize_streaming_message(
        "agent-1",
        "msg-1",
        allow_agent_wide_fallback=False,
    )
    assert ok is False
    assert calls == ["update"]


@pytest.mark.asyncio
async def test_finalize_success_without_fallback(monkeypatch):
    svc = ChatMessageService()
    calls: list[str] = []

    async def fake_update(agent_id, msg_id, attrs):
        calls.append("update")
        return True

    async def fake_done(agent_id):
        calls.append("done")

    monkeypatch.setattr(svc, "update_message", fake_update)
    monkeypatch.setattr(svc, "update_streaming_messages_done", fake_done)

    ok = await svc.finalize_streaming_message("agent-1", "msg-1", {"content": "x"})
    assert ok is True
    assert calls == ["update"]


@pytest.mark.asyncio
async def test_finalize_no_msg_id_uses_agent_wide(monkeypatch):
    svc = ChatMessageService()
    calls: list[str] = []

    async def fake_update(agent_id, msg_id, attrs):
        calls.append("update")
        return True

    async def fake_done(agent_id):
        calls.append("done")

    monkeypatch.setattr(svc, "update_message", fake_update)
    monkeypatch.setattr(svc, "update_streaming_messages_done", fake_done)

    ok = await svc.finalize_streaming_message("agent-1", None)
    assert ok is True
    assert calls == ["done"]
