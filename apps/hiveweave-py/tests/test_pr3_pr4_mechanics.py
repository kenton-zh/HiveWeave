"""PR3 progress floors + PR4 wake/dispatch/latch mechanics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.wake_policy import classify_message, should_wake


def test_fyi_without_reply_is_progress():
    cat = classify_message(
        message="FYI merge landed on main, no action needed",
        message_type="normal",
        from_agent_id="peer-1",
    )
    assert cat == "progress"
    assert should_wake(cat) is False


def test_reply_request_still_command():
    cat = classify_message(
        message="请依次回复结果",
        message_type="normal",
        from_agent_id="peer-1",
    )
    assert cat == "command"
    assert should_wake(cat) is True


def test_task_dispatch_still_wakes():
    cat = classify_message(
        message="Implement the login page",
        message_type="task",
        from_agent_id="boss",
        task_id="abc-123",
    )
    assert cat == "task_transition"
    assert should_wake(cat) is True


def test_approval_message_still_wakes():
    cat = classify_message(
        message="[TASK APPROVED] Task 'x' has been approved",
        message_type="task",
        from_agent_id="boss",
        task_id="abc-123",
    )
    assert cat == "task_transition"
    assert should_wake(cat) is True


@pytest.mark.asyncio
async def test_emit_task_event_raises_progress_floor():
    from hiveweave.services.task import TaskService

    ts = TaskService()
    executed: list[tuple] = []

    async def fake_query(project_id, sql, params=None):
        if "SELECT progress" in sql:
            return [{"progress": 5}]
        return []

    async def fake_execute(project_id, sql, params=None):
        executed.append((sql, params))

    with (
        patch("hiveweave.services.task._ensure_schema", AsyncMock()),
        patch("hiveweave.services.task._query", side_effect=fake_query),
        patch("hiveweave.services.task._execute", side_effect=fake_execute),
        patch(
            "hiveweave.services.work_log.WorkLogService.append_log",
            AsyncMock(),
        ),
    ):
        await ts.emit_task_event(
            "pid", "tid-1", "claimed", agent_id="a1", summary="claimed"
        )

    assert any("progress = ?" in sql for sql, _ in executed)
    progress_call = next(p for s, p in executed if "progress = ?" in s)
    assert progress_call[0] == 10  # claimed floor


@pytest.mark.asyncio
async def test_update_progress_never_decreases():
    from hiveweave.services.task import TaskService

    ts = TaskService()
    executed: list = []

    async def fake_query(project_id, sql, params=None):
        if "SELECT" in sql and "progress" in sql:
            return [{"progress": 80}]
        return []

    async def fake_execute(project_id, sql, params=None):
        executed.append((sql, list(params or [])))

    with (
        patch("hiveweave.services.task._ensure_schema", AsyncMock()),
        patch("hiveweave.services.task._query", side_effect=fake_query),
        patch("hiveweave.services.task._execute", side_effect=fake_execute),
    ):
        await ts.update_progress("pid", "tid", 50)

    # Already at 80 — must not write a lower value
    assert executed == []


@pytest.mark.asyncio
async def test_dispatch_triggers_assignee():
    from hiveweave.tools.task_tools import DispatchTaskParams, dispatch_task_tool

    trigger = AsyncMock()
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="pid"),
        ),
        patch(
            "hiveweave.tools.task_tools.resolve_agent_id",
            AsyncMock(return_value="assignee-1"),
        ),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_similar_open_task",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.dispatch.DispatchService.dispatch_task",
            AsyncMock(
                return_value={
                    "success": True,
                    "task_id": "t1",
                    "to_agent_id": "assignee-1",
                }
            ),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            trigger,
        ),
    ):
        result = await dispatch_task_tool(
            DispatchTaskParams(target="A004", task="Do the thing"),
            "boss",
            "/ws",
            ctx=None,
        )

    assert result.success is True
    trigger.assert_awaited_once_with("assignee-1")


def test_resume_suppressed_decay_and_task_unlock():
    import time

    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = "a1"
    agent._resume_suppressed = True
    agent._resume_suppressed_at = time.monotonic() - 1
    agent._RESUME_SUPPRESS_DECAY_S = 30 * 60

    # Generic trigger still blocked
    assert agent.try_clear_resume_suppressed({"trigger": True, "source": "trigger"})
    assert agent._resume_suppressed is True

    # Task unlock
    assert (
        agent.try_clear_resume_suppressed(
            {"trigger": True, "source": "task", "message_type": "task"}
        )
        is False
    )
    assert agent._resume_suppressed is False

    # Decay unlock
    agent._resume_suppressed = True
    agent._resume_suppressed_at = time.monotonic() - (31 * 60)
    assert (
        agent.try_clear_resume_suppressed({"trigger": True, "source": "trigger"})
        is False
    )
    assert agent._resume_suppressed is False
