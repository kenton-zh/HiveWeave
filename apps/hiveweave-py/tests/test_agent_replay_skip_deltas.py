"""Agent replay must not store token deltas (round_start eviction → stacked prose)."""
from __future__ import annotations

import pytest

from hiveweave.realtime.event_bus import AGENT_REPLAY_BUFFER, StatusEventBus
from hiveweave.realtime.phoenix_adapter import _map_event


@pytest.mark.asyncio
async def test_replay_skips_token_deltas_keeps_round_start() -> None:
    bus = StatusEventBus()
    aid = "agent-replay-1"
    for i in range(AGENT_REPLAY_BUFFER + 10):
        await bus.publish_stream_event(aid, {"type": "text_delta", "content": str(i)})
        await bus.publish_stream_event(aid, {"type": "thinking_delta", "content": "t"})
        await bus.publish_stream_event(aid, {"type": "thinking", "elapsed_s": 1})
    await bus.publish_stream_event(aid, {"type": "round_start", "round": 1})
    await bus.publish_stream_event(aid, {"type": "tool_call_start", "tool_name": "bash"})
    await bus.publish_stream_event(aid, {"type": "done"})

    replay = bus.get_agent_replay(aid)
    types = [e["type"] for e in replay]
    assert "text_delta" not in types
    assert "thinking_delta" not in types
    assert "thinking" not in types
    assert types == ["round_start", "tool_call_start", "done"]
    assert bus.get_agent_replay(aid) == []


@pytest.mark.asyncio
async def test_round_start_survives_delta_flood_after_it() -> None:
    """Original eviction shape: round_start first, then 50+ deltas."""
    bus = StatusEventBus()
    aid = "agent-replay-2"
    await bus.publish_stream_event(aid, {"type": "round_start", "round": 1})
    for i in range(AGENT_REPLAY_BUFFER + 10):
        await bus.publish_stream_event(aid, {"type": "text_delta", "content": str(i)})
    assert [e["type"] for e in bus.get_agent_replay(aid)] == ["round_start"]


@pytest.mark.asyncio
async def test_start_clears_prior_turn_done_from_replay() -> None:
    bus = StatusEventBus()
    aid = "agent-replay-3"
    await bus.publish_stream_event(aid, {"type": "round_start", "round": 0})
    await bus.publish_stream_event(aid, {"type": "done"})
    await bus.publish_stream_event(aid, {"type": "start"})
    await bus.publish_stream_event(aid, {"type": "round_start", "round": 0})
    types = [e["type"] for e in bus.get_agent_replay(aid)]
    assert types == ["start", "round_start"]
    assert "done" not in types


@pytest.mark.asyncio
async def test_emit_activity_skips_token_deltas() -> None:
    bus = StatusEventBus()
    aid = "agent-replay-4"
    for i in range(5):
        await bus.publish_stream_event(aid, {"type": "text_delta", "content": str(i)})
    await bus.publish_stream_event(aid, {"type": "round_start", "round": 1})
    types = [e["type"] for e in bus.get_recent_activity()]
    assert "text_delta" not in types
    assert "round_start" in types


def test_map_event_round_start_is_not_stream_chunk() -> None:
    name, payload = _map_event({"type": "round_start", "round": 0, "agentId": "a"})
    assert name == "round_start"
    assert name != "stream_chunk"
    assert payload.get("round") == 0
