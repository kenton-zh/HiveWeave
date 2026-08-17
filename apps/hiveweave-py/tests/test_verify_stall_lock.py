"""VERIFY serial-lock hints, TASK STALL live-job skip, executor stall reassign."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.verify_spawn import (
    VERIFY_LOCK_UNLOCK_HINT,
    format_assignee_lock_label,
    maybe_reassign_stalled_verify,
)
from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401
from tests.test_verify_lifecycle import _make_verify

QA2 = "qa-other-1"


def test_format_assignee_lock_label_prefers_short_id_and_name():
    assert (
        format_assignee_lock_label(
            {"short_id": "Q001", "name": "Nova"},
            "aaaaaaaa-bbbb-cccc",
        )
        == "Q001 (Nova)"
    )
    assert format_assignee_lock_label(None, "abcdefghijkl") == "abcdefghijkl"
    assert format_assignee_lock_label({}, None) == "?"


def _fake_get_agent(table: dict):
    async def fake(aid, *a, **k):
        return table.get(str(aid))

    return fake


@pytest.mark.asyncio
async def test_get_tasks_lock_string_contains_unlock_condition(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    await ts.claim_task(pid, first_id, EXEC)

    agents = {
        EXEC: {
            "id": EXEC,
            "name": "Nova",
            "short_id": "Q001",
            "status": "active",
            "parent_id": COORD,
        },
        COORD: {
            "id": COORD,
            "name": "Lead",
            "short_id": "C001",
            "status": "active",
        },
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(side_effect=_fake_get_agent(agents)),
        ),
    ):
        result = await get_tasks_tool(
            GetTasksParams(), EXEC, task_env["workspace"]
        )

    assert result.success, result.output or result.error
    out = result.output or ""
    assert "verify_serial_lock:" in out
    assert "closed/cancelled" in out
    assert "not when claimed" in out
    assert VERIFY_LOCK_UNLOCK_HINT.split(";")[0] in out
    assert "Q001" in out
    assert "Nova" in out
    assert "verify_lock:" in out
    assert first_id[:8] in out
    assert second_id[:8] in out


@pytest.mark.asyncio
async def test_claim_verify_blocked_hint_has_holder_and_unlock(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    await ts.claim_task(pid, first_id, EXEC)

    agents = {
        EXEC: {
            "id": EXEC,
            "name": "Nova",
            "short_id": "Q001",
            "status": "active",
        },
    }
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(side_effect=_fake_get_agent(agents)),
    ):
        with pytest.raises(ValueError) as exc:
            await ts.claim_task(pid, second_id, EXEC)

    msg = str(exc.value)
    assert "closed/cancelled" in msg
    assert "not when claimed" in msg
    assert "no estimated duration" in msg
    assert "Q001" in msg
    assert first_id[:8] in msg


def _stall_nudge_patches(*, live_jobs: bool, tasks: list, agents: list, now: int):
    from hiveweave.services import game_time as gt

    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"id": "m1", "should_wake": True}

    svc = gt.GameTimeService(tasks[0].get("id") and "proj")
    svc._watchdog_trigger = AsyncMock()
    return sent, svc, [
        patch(
            "hiveweave.db.meta.query_one",
            new=AsyncMock(return_value={"is_started": 1}),
        ),
        patch(
            "hiveweave.services.system_state.system_state.paused",
            return_value=False,
        ),
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            new=AsyncMock(return_value=tasks),
        ),
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents),
        ),
        patch(
            "hiveweave.services.inbox.InboxService",
            return_value=FakeInbox(),
        ),
        patch(
            "hiveweave.services.wait_contract.wait_contract_service.list_all_active",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.offturn.agent_has_live_job_for_task",
            return_value=live_jobs,
        ),
        patch("time.time", return_value=now / 1000),
    ]


@pytest.mark.asyncio
async def test_task_stall_skips_when_has_live_jobs_for_agent():
    from hiveweave.services import game_time as gt

    project_id = "proj-stall-livejob"
    now = 1_700_000_000_000
    stale = now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000,
        "silence_trackers": {},
        "task_stall_counts": {},
    }
    tasks = [
        {
            "id": "claimed-1",
            "creator_id": COORD,
            "assignee_id": EXEC,
            "status": "claimed",
            "title": "Feature",
            "tags": [],
            "updated_at": stale,
            "progress": 10,
        },
    ]
    agents = [
        {"id": COORD, "parent_id": None, "short_id": "C001"},
        {"id": EXEC, "parent_id": COORD, "short_id": "E001"},
    ]
    sent, svc, patches = _stall_nudge_patches(
        live_jobs=True, tasks=tasks, agents=agents, now=now
    )
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            await svc._nudge_stale_ledger(project_id)
    finally:
        gt._states.pop(project_id, None)

    assert not any(
        (m.get("message") or "").startswith("[TASK STALL]") for m in sent
    )


@pytest.mark.asyncio
async def test_task_stall_nudges_when_live_job_is_other_task():
    """A bg-bash bound to another task must not freeze THIS task's dwell clock.

    Platform records the stall clock; no [TASK STALL] inbox催.
    """
    from hiveweave.services import game_time as gt

    project_id = "proj-stall-other-job"
    now = 1_700_000_000_000
    stale = now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000,
        "silence_trackers": {},
        "task_stall_counts": {},
    }
    tasks = [
        {
            "id": "claimed-1",
            "creator_id": COORD,
            "assignee_id": EXEC,
            "status": "claimed",
            "title": "Feature",
            "tags": [],
            "updated_at": stale,
            "progress": 10,
        },
    ]
    agents = [
        {"id": COORD, "parent_id": None, "short_id": "C001"},
        {"id": EXEC, "parent_id": COORD, "short_id": "E001"},
    ]
    sent, svc, patches = _stall_nudge_patches(
        live_jobs=False, tasks=tasks, agents=agents, now=now
    )

    def other_task_only(agent_id, task_id):
        return str(task_id) == "other-task"

    patches[6] = patch(
        "hiveweave.services.offturn.agent_has_live_job_for_task",
        side_effect=other_task_only,
    )
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            await svc._nudge_stale_ledger(project_id)
        assert (gt._states[project_id].get("task_stall_counts") or {}).get(
            "claimed-1", 0
        ) >= 1
    finally:
        gt._states.pop(project_id, None)

    assert sent == []


@pytest.mark.asyncio
async def test_task_stall_nudges_idle_verify_lock_waiter():
    """Queued VERIFY (created) with no wait / no live job: clock ticks, no inbox催."""
    from hiveweave.services import game_time as gt

    project_id = "proj-stall-lock-waiter"
    now = 1_700_000_000_000
    stale = now - gt.TASK_STALL_THRESHOLDS["created"] - 1000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.TASK_STALL_THRESHOLDS["created"] - 1000,
        "silence_trackers": {},
        "task_stall_counts": {},
    }
    tasks = [
        {
            "id": "verify-queued-1",
            "creator_id": COORD,
            "assignee_id": EXEC,
            "status": "created",
            "title": "VERIFY: UI B",
            "tags": ["verify"],
            "updated_at": stale,
            "progress": 0,
        },
    ]
    agents = [
        {"id": COORD, "parent_id": None, "short_id": "C001"},
        {"id": EXEC, "parent_id": COORD, "short_id": "E001"},
    ]
    sent, svc, patches = _stall_nudge_patches(
        live_jobs=False, tasks=tasks, agents=agents, now=now
    )
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            await svc._nudge_stale_ledger(project_id)
        assert (gt._states[project_id].get("task_stall_counts") or {}).get(
            "verify-queued-1", 0
        ) >= 1
    finally:
        gt._states.pop(project_id, None)

    assert sent == []


@pytest.mark.asyncio
async def test_maybe_reassign_stalled_verify_to_other_qa(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    verify_id = await _make_verify(pid, ts, title="UI stall")
    await ts.claim_task(pid, verify_id, EXEC)
    task = await ts.get_task(pid, verify_id)
    assert task["status"] == "claimed"

    send = AsyncMock()
    trigger = AsyncMock()
    by_id = {
        EXEC: {"id": EXEC, "parent_id": COORD, "name": "exec", "short_id": "E1"},
        COORD: {"id": COORD, "parent_id": None, "name": "coord", "short_id": "C1"},
        QA2: {"id": QA2, "parent_id": COORD, "role": "qa_engineer"},
    }
    with (
        patch(
            "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
            new=AsyncMock(return_value=QA2),
        ),
        patch("hiveweave.services.inbox.InboxService.send_message", send),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
    ):
        handled = await maybe_reassign_stalled_verify(
            pid, task, agents_by_id=by_id
        )

    assert handled is True
    after = await ts.get_task(pid, verify_id)
    assert after["assignee_id"] == QA2
    assert after["status"] == "claimed"
    assert after["status"] not in ("approved", "closed")
    trigger.assert_awaited_with(QA2)
    bodies = [str(c.kwargs.get("message") or "") for c in send.await_args_list]
    assert any("[VERIFY EXECUTOR STALL]" in b for b in bodies)
    targets = [c.kwargs.get("to_agent_id") for c in send.await_args_list]
    assert COORD in targets
    assert QA2 in targets
    assert not any("auto-approve" in b.lower() and "will auto" in b.lower() for b in bodies)


@pytest.mark.asyncio
async def test_maybe_reassign_stalled_verify_no_qa_inbox_only(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    verify_id = await _make_verify(pid, ts, title="UI noqa")
    await ts.claim_task(pid, verify_id, EXEC)
    task = await ts.get_task(pid, verify_id)

    send = AsyncMock()
    trigger = AsyncMock()
    reassign = AsyncMock()
    by_id = {
        EXEC: {"id": EXEC, "parent_id": COORD, "name": "exec", "short_id": "E1"},
        COORD: {"id": COORD, "parent_id": None, "name": "coord", "short_id": "C1"},
    }
    with (
        patch(
            "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
            new=AsyncMock(return_value=None),
        ),
        patch("hiveweave.services.inbox.InboxService.send_message", send),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
        patch(
            "hiveweave.services.task.TaskService.reassign_task",
            reassign,
        ),
    ):
        handled = await maybe_reassign_stalled_verify(
            pid, task, agents_by_id=by_id
        )

    assert handled is True
    after = await ts.get_task(pid, verify_id)
    assert after["assignee_id"] == EXEC
    assert after["status"] == "claimed"
    reassign.assert_not_awaited()
    trigger.assert_not_awaited()
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs.get("to_agent_id") == COORD
    assert "[VERIFY EXECUTOR STALL]" in (kwargs.get("message") or "")
    assert "do not auto-approve" in (kwargs.get("message") or "")


@pytest.mark.asyncio
async def test_maybe_reassign_skips_submitted_verify():
    """submitted is reviewer-owned; do not undo submit by reassigning QA."""
    handled = await maybe_reassign_stalled_verify(
        "p",
        {
            "id": "v1",
            "title": "VERIFY: x",
            "status": "submitted",
            "assignee_id": EXEC,
            "creator_id": COORD,
        },
        agents_by_id={},
    )
    assert handled is False


@pytest.mark.asyncio
async def test_nudge_stale_ledger_verify_escalation_calls_reassign():
    from hiveweave.services import game_time as gt

    project_id = "proj-verify-exec-stall"
    now = 1_700_000_000_000
    stale = now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000
    tid = "verify-claimed-stall"
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.TASK_STALL_THRESHOLDS["claimed"] - 1000,
        "silence_trackers": {},
        "task_stall_counts": {tid: gt.STALL_ESCALATION_THRESHOLD},
    }
    tasks = [
        {
            "id": tid,
            "creator_id": COORD,
            "assignee_id": EXEC,
            "status": "claimed",
            "title": "VERIFY: UI stall",
            "tags": ["verify"],
            "updated_at": stale,
            "progress": 10,
        },
    ]
    agents = [
        {"id": COORD, "parent_id": None, "short_id": "C001"},
        {"id": EXEC, "parent_id": COORD, "short_id": "E001"},
    ]
    called: list[str] = []

    async def fake_reassign(pid, task, agents_by_id=None):
        called.append(str(task.get("id")))
        return True

    sent, svc, patches = _stall_nudge_patches(
        live_jobs=False, tasks=tasks, agents=agents, now=now
    )
    try:
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patch(
                "hiveweave.tools.tasks.verify_spawn.maybe_reassign_stalled_verify",
                new=fake_reassign,
            ),
        ):
            await svc._nudge_stale_ledger(project_id)
    finally:
        gt._states.pop(project_id, None)

    assert called == [tid]
    assert not any(
        (m.get("message") or "").startswith("[TASK STALL]") for m in sent
    )
    assert not any(
        (m.get("message") or "").startswith("[TASK STALL ESCALATION]")
        for m in sent
    )
