"""Tests for TurnResult ABI + turn exit gates."""

from __future__ import annotations

import json

import pytest

from hiveweave.services.turn_exit import (
    ExitContext,
    collect_unreplied_asks,
    evaluate_turn_exit,
)
from hiveweave.services.turn_result import (
    parse_turn_result,
    validate_phase_fields,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    pop_pending_turn_result,
    set_pending_turn_result,
)


def test_parse_turn_result_and_phase_fields():
    tr = parse_turn_result(
        {
            "phase": "done_slice",
            "summary": "Verified tools",
            "result": {},
        }
    )
    assert tr.phase == "done_slice"
    assert validate_phase_fields(tr) == []

    waiting = parse_turn_result(
        {"phase": "waiting", "summary": "Waiting on 拾柒", "waitingOn": []}
    )
    assert "WAITING_ON_REQUIRED" in validate_phase_fields(waiting)

    waiting_ok = parse_turn_result(
        {
            "phase": "waiting",
            "summary": "Waiting on 拾柒",
            "waiting_on": [{"kind": "agent", "ref": "拾柒"}],
        }
    )
    assert validate_phase_fields(waiting_ok) == []


def test_exit_requires_commit_turn():
    clear_pending_turn_result("a1")
    d = evaluate_turn_exit(
        ExitContext(agent_id="a1", project_id="p1", tool_calls=[])
    )
    assert not d.ok
    assert "MISSING_COMMIT_TURN" in d.violations
    assert "commit_turn" in d.hint


def test_exit_ok_done_slice():
    clear_pending_turn_result("a1")
    set_pending_turn_result(
        "a1",
        {
            "schema_version": 1,
            "phase": "done_slice",
            "summary": "All clear",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    d = evaluate_turn_exit(
        ExitContext(agent_id="a1", project_id="p1", tool_calls=[])
    )
    assert d.ok
    assert not d.continue_work
    pop_pending_turn_result("a1")


def test_exit_in_progress_continues():
    clear_pending_turn_result("a1")
    set_pending_turn_result(
        "a1",
        {
            "phase": "in_progress",
            "summary": "Still coding",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    d = evaluate_turn_exit(
        ExitContext(agent_id="a1", project_id="p1", tool_calls=[])
    )
    assert d.ok
    assert d.continue_work
    pop_pending_turn_result("a1")


def test_exit_blocks_unreplied_asks_and_open_tasks():
    clear_pending_turn_result("a1")
    set_pending_turn_result(
        "a1",
        {
            "phase": "done_slice",
            "summary": "Pretend done",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    d = evaluate_turn_exit(
        ExitContext(
            agent_id="a1",
            project_id="p1",
            tool_calls=[],
            unreplied_asks=[{"id": "m1", "from_name": "归零", "message": "请回复"}],
            open_task_obligations=[{"id": "task-1", "status": "running"}],
            tasks_advanced=set(),
        )
    )
    assert not d.ok
    assert "UNREPLIED_ASKS" in d.violations
    assert "OPEN_TASKS_UNDECLARED" in d.violations
    pop_pending_turn_result("a1")


def test_collect_unreplied_asks_cleared_by_ask_agent():
    pending = [
        {
            "id": "m1",
            "from_agent_id": "ceo",
            "message": "工具验证，请回复结果",
            "expect_report": 0,
            "message_type": "ask",
        }
    ]
    tool_calls = [
        {
            "function": {
                "name": "ask_agent",
                "arguments": json.dumps(
                    {"recipients": ["归零"], "message": "ok"}
                ),
            }
        }
    ]
    unreplied = collect_unreplied_asks(
        pending, tool_calls, {"ceo": "归零"}
    )
    assert unreplied == []


@pytest.mark.asyncio
async def test_commit_turn_tool_buffers_result():
    from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

    clear_pending_turn_result("agent-x")
    params = CommitTurnParams(
        phase="waiting",
        summary="Waiting for engineers",
        waiting_on=[{"kind": "agent", "ref": "拾柒"}],
    )
    result = await commit_turn_tool(params, "agent-x", "", ctx=None)
    assert result.success
    from hiveweave.services.turn_session import get_pending_turn_result

    pending = get_pending_turn_result("agent-x")
    assert pending is not None
    assert pending["phase"] == "waiting"
    pop_pending_turn_result("agent-x")
