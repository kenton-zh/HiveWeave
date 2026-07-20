"""Poll cache + waiting gate for check_agent_status / get_tasks (TEST3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.llm import streamer as streamer_mod
from hiveweave.llm.streamer import (
    _poll_cache_get,
    _poll_cache_put,
    _poll_waiting_gate_block_async,
    doom_loop_limit,
)


def test_doom_limits_for_poll_tools():
    assert doom_loop_limit("check_agent_status") == 5
    assert doom_loop_limit("get_tasks") == 6
    assert doom_loop_limit("read_file") == 15
    assert doom_loop_limit("commit_turn") == 8


def test_poll_cache_hit_within_ttl():
    streamer_mod._poll_result_cache.clear()
    _poll_cache_put("a1", "get_tasks", "{}", "TASKS: none")
    hit = _poll_cache_get("a1", "get_tasks", "{}")
    assert hit is not None
    assert "TASKS: none" in hit
    assert hit.startswith("[cached")


def test_poll_cache_miss_other_args():
    streamer_mod._poll_result_cache.clear()
    _poll_cache_put("a1", "get_tasks", "{}", "A")
    assert _poll_cache_get("a1", "get_tasks", '{"status":"open"}') is None


@pytest.mark.asyncio
async def test_waiting_gate_blocks_status_not_get_tasks():
    agent = SimpleNamespace(
        disposition="waiting_agent",
        project_id="p1",
    )
    with (
        patch(
            "hiveweave.agents.supervisor.agent_manager.get_agent",
            return_value=agent,
        ),
        patch(
            "hiveweave.services.wait_contract.wait_contract_service.list_active",
            new_callable=AsyncMock,
            return_value=[{"kind": "agent", "ref": "潮汐"}],
        ),
    ):
        status_msg = await _poll_waiting_gate_block_async(
            "a1", "check_agent_status"
        )
        tasks_msg = await _poll_waiting_gate_block_async("a1", "get_tasks")
    assert status_msg is not None
    assert "wait contract active" in status_msg
    assert tasks_msg is None  # resume turns must still get_tasks


@pytest.mark.asyncio
async def test_waiting_gate_allows_when_idle():
    agent = SimpleNamespace(disposition="runnable", project_id="p1")
    with patch(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        return_value=agent,
    ):
        msg = await _poll_waiting_gate_block_async("a1", "check_agent_status")
    assert msg is None


@pytest.mark.asyncio
async def test_get_tasks_hard_reject_third_same_args():
    from hiveweave.llm.streamer import Streamer

    streamer_mod._poll_result_cache.clear()
    streamer = Streamer(max_tool_rounds=5)
    counts: dict[tuple[str, str], int] = {}
    calls = {"n": 0}

    async def on_tool(_name: str, _args: str, _id: str) -> dict:
        calls["n"] += 1
        return {"content": "Tasks (0): none"}

    tc = {"id": "t1", "name": "get_tasks", "arguments": "{}"}
    r1 = await streamer._execute_single_tool(
        "a1", tc, on_tool, poll_turn_counts=counts
    )
    r2 = await streamer._execute_single_tool(
        "a1", {**tc, "id": "t2"}, on_tool, poll_turn_counts=counts
    )
    r3 = await streamer._execute_single_tool(
        "a1", {**tc, "id": "t3"}, on_tool, poll_turn_counts=counts
    )
    assert "Tasks" in r1["content"] or "cached" in r1["content"]
    assert "poll hard reject" in r3["content"]
    assert calls["n"] <= 2  # third call blocked before executor
