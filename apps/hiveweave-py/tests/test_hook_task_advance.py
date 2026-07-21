"""Tests for agent.turn.after task-advance nudge + defer_task_advance."""

from __future__ import annotations

import pytest

from hiveweave.hooks.handlers.task_advance import (
    decide_task_advance_nudge,
    on_agent_turn_after,
    remaining_obligations,
    task_ids_advanced,
)
from hiveweave.services.turn_session import (
    clear_task_advance_deferred,
    is_task_advance_deferred,
    set_task_advance_deferred,
)


def _obl(tid: str, *, role: str = "assignee", status: str = "running") -> dict:
    return {
        "id": tid,
        "title": f"task-{tid[:4]}",
        "role_hint": role,
        "status": status,
    }


def test_skip_when_no_obligations():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[],
        tool_calls=[],
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert hint is None
    assert skip == "no_obligations"


def test_skip_when_declared_waiting():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("t1")],
        tool_calls=[],
        phase="waiting",
        disposition="waiting_agent",
        gate_repairing=False,
        continue_slice=False,
    )
    assert hint is None
    assert skip == "declared_wait"


def test_nudge_when_hollow_done_with_running_task():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("aaaaaaaa-1111")],
        tool_calls=[
            {"function": {"name": "commit_turn", "arguments": "{}"}},
        ],
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert skip == ""
    assert hint is not None
    assert "[TASK ADVANCE]" in hint
    assert "defer_task_advance" in hint


def test_skip_when_defer_tool_called():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("bbbbbbbb-2222")],
        tool_calls=[
            {
                "function": {
                    "name": "defer_task_advance",
                    "arguments": '{"reason":"waiting on design"}',
                }
            },
            {"function": {"name": "commit_turn", "arguments": "{}"}},
        ],
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert hint is None
    assert skip == "deferred"


def test_skip_when_deferred_flag_set():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("cccccccc-3333")],
        tool_calls=[],
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
        deferred=True,
    )
    assert hint is None
    assert skip == "deferred"


def test_skip_when_task_submitted():
    tid = "dddddddd-4444"
    tool_calls = [
        {
            "function": {
                "name": "submit_task",
                "arguments": f'{{"taskId": "{tid}"}}',
            }
        }
    ]
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl(tid)],
        tool_calls=tool_calls,
        tasks_advanced=task_ids_advanced(tool_calls),
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert hint is None
    assert skip == "all_advanced"


def test_nudge_when_assignee_wrote_code_without_ledger():
    """write_file alone is not enough — still nudge to submit/update_progress."""
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("eeeeeeee-5555")],
        tool_calls=[
            {"function": {"name": "write_file", "arguments": "{}"}},
            {"function": {"name": "commit_turn", "arguments": "{}"}},
        ],
        phase="in_progress",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert skip == ""
    assert hint is not None
    assert "[TASK ADVANCE]" in hint


def test_nudge_when_fake_blocked_with_open_task():
    """disposition=blocked must not silence task-advance (use defer instead)."""
    hint, skip = decide_task_advance_nudge(
        open_obligations=[_obl("bbbbbbbb-blocked", status="claimed")],
        tool_calls=[
            {"function": {"name": "commit_turn", "arguments": "{}"}},
        ],
        phase="blocked",
        disposition="blocked",
        gate_repairing=False,
        continue_slice=False,
    )
    assert skip == ""
    assert hint is not None


def test_nudge_creator_even_if_wrote_files():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[
            _obl("ffffffff-6666", role="creator", status="submitted"),
        ],
        tool_calls=[
            {"function": {"name": "write_file", "arguments": "{}"}},
        ],
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
    )
    assert skip == ""
    assert hint is not None
    assert "review_task" in hint


def test_defer_flag_session_helpers():
    clear_task_advance_deferred("agent-x")
    assert not is_task_advance_deferred("agent-x")
    set_task_advance_deferred("agent-x", True)
    assert is_task_advance_deferred("agent-x")
    clear_task_advance_deferred("agent-x")
    assert not is_task_advance_deferred("agent-x")


@pytest.mark.asyncio
async def test_defer_tool_sets_flag():
    from hiveweave.tools.turn_tools import (
        DeferTaskAdvanceParams,
        defer_task_advance_tool,
    )

    clear_task_advance_deferred("agent-defer")
    result = await defer_task_advance_tool(
        DeferTaskAdvanceParams(reason="blocked on HR reply"),
        "agent-defer",
        "",
        ctx=None,
    )
    assert result.success
    assert is_task_advance_deferred("agent-defer")
    clear_task_advance_deferred("agent-defer")


@pytest.mark.asyncio
async def test_hook_mutates_output():
    out: dict = {"hint": None}
    await on_agent_turn_after(
        {
            "agent_id": "a1",
            "open_obligations": [_obl("gggggggg-7777")],
            "tool_calls": [],
            "phase": "done_slice",
            "disposition": "runnable",
            "gate_repairing": False,
            "continue_slice": False,
            "deferred": False,
            "reminder_count": 0,
            "reminder_max": 2,
        },
        out,
    )
    assert out.get("nudge_kind") == "task_advance"
    assert "[TASK ADVANCE]" in (out.get("hint") or "")


def test_builtin_skill_exists():
    from hiveweave.services.skill_registry import BUILTIN_SKILLS

    slugs = {s["slug"] for s in BUILTIN_SKILLS}
    assert "task-advance" in slugs


def test_remaining_obligations_prefix_match():
    rem = remaining_obligations(
        [_obl("ffffffff-6666-full")],
        {"ffffffff"},
    )
    assert rem == []
