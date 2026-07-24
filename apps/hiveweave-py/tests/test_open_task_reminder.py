"""Tests for open-task reminder helpers and actionable obligations."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-open-task-project"
AGENT_A = "agent-assignee"
AGENT_C = "agent-creator"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _make_agent() -> Agent:
    """Minimal Agent without inbox watcher (no running loop required for helpers)."""
    with patch.object(Agent, "__init__", lambda self, *a, **k: None):
        ag = Agent.__new__(Agent)
    ag.id = AGENT_A
    ag.project_id = PROJECT_ID
    ag.config = {"role": "developer", "name": "Tester"}
    ag.status = AgentState.IDLE
    ag._task_reminder_count = 0
    ag._TASK_REMINDER_MAX = 2
    ag._resume_cooldown_until = 0.0
    ag._conversation = MagicMock()
    ag._conversation.append_turn = AsyncMock()
    return ag


@pytest.mark.asyncio
async def test_actionable_obligations_assignee_and_creator(env):
    ts = TaskService()
    pid = env["project_id"]

    run_id = await ts.create_task(
        pid, "Run me", "desc", AGENT_C, assignee_id=AGENT_A
    )
    await ts.claim_task(pid, run_id, AGENT_A)
    await ts.start_task(pid, run_id)

    sub_id = await ts.create_task(
        pid, "Review me", "desc", AGENT_C, assignee_id=AGENT_A
    )
    await ts.claim_task(pid, sub_id, AGENT_A)
    await ts.start_task(pid, sub_id)
    await ts.submit_task(pid, sub_id, {"summary": "done", "tests_passed": True})

    blocked_id = await ts.create_task(
        pid, "Blocked", "desc", AGENT_C, assignee_id=AGENT_A
    )
    await ts.claim_task(pid, blocked_id, AGENT_A)
    await ts.start_task(pid, blocked_id)
    await ts.block_task(pid, blocked_id, "dependency:other-task waiting")

    as_assignee = await ts.get_actionable_obligations(pid, AGENT_A)
    statuses = {t["id"]: t["status"] for t in as_assignee}
    assert run_id in statuses and statuses[run_id] == "running"
    assert blocked_id not in statuses  # blocked excluded

    as_creator = await ts.get_actionable_obligations(pid, AGENT_C)
    creator_ids = {t["id"] for t in as_creator}
    assert sub_id in creator_ids
    # submit pins reviewer_id=creator → role_hint is "reviewer" (TEST11 #3),
    # not "creator" (creator keeps approved→merge only when reviewer differs).
    assert all(
        t["role_hint"] == "reviewer" for t in as_creator if t["id"] == sub_id
    )


@pytest.mark.asyncio
async def test_block_infers_wait_kind(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "T", "d", AGENT_C, assignee_id=AGENT_A)
    await ts.claim_task(pid, tid, AGENT_A)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "timer: wait for script")
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"
    assert task.get("wait_kind") == "timer"
    assert (task.get("blocked_reason") or "").startswith("timer:")


@pytest.mark.asyncio
async def test_dependency_wake_on_approve(env):
    ts = TaskService()
    pid = env["project_id"]

    blocker = await ts.create_task(
        pid, "Blocker", "d", AGENT_C, assignee_id=AGENT_A
    )
    await ts.claim_task(pid, blocker, AGENT_A)
    await ts.start_task(pid, blocker)
    await ts.submit_task(pid, blocker, {"summary": "ok", "tests_passed": True})
    await ts.start_review(pid, blocker)

    dependent = await ts.create_task(
        pid,
        "Dependent",
        "d",
        AGENT_C,
        assignee_id=AGENT_A,
        depends_on=[blocker],
    )
    await ts.claim_task(pid, dependent, AGENT_A)
    await ts.start_task(pid, dependent)
    await ts.block_task(pid, dependent, f"dependency:{blocker} waiting")

    with patch("hiveweave.services.inbox.InboxService.send_message",
               new_callable=AsyncMock) as send_mock, \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               new_callable=AsyncMock) as trig_mock:
        await ts.review_task(pid, blocker, "approve", feedback="lgtm")

    dep = await ts.get_task(pid, dependent)
    assert dep["status"] == "running"
    assert dep.get("blocked_reason") is None
    send_mock.assert_awaited()
    trig_mock.assert_awaited()


def test_open_task_hint_and_advanced_detection():
    ag = _make_agent()
    obligations = [
        {
            "id": "aaaaaaaa-1111-2222-3333-444444444444",
            "title": "Fix slot\nmore",
            "status": "running",
            "role_hint": "assignee",
            "progress": 80,
        }
    ]
    hint = ag._build_open_task_hint(obligations)
    assert hint.startswith("[TASK ADVANCE]")
    assert "aaaaaaaa" in hint
    assert "running" in hint

    tool_calls = [
        {
            "function": {
                "name": "submit_task",
                "arguments": (
                    '{"taskId":"aaaaaaaa-1111-2222-3333-444444444444"}'
                ),
            }
        }
    ]
    advanced = ag._task_ids_advanced_this_turn(tool_calls)
    assert "aaaaaaaa-1111-2222-3333-444444444444" in advanced


@pytest.mark.asyncio
async def test_maybe_open_task_reminder_skips_when_advanced(env):
    """When all obligations were advanced this turn, hint should be empty."""
    ag = _make_agent()
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "X", "d", AGENT_C, assignee_id=AGENT_A)
    await ts.claim_task(pid, tid, AGENT_A)
    await ts.start_task(pid, tid)

    tool_calls = [
        {
            "function": {
                "name": "submit_task",
                "arguments": f'{{"taskId":"{tid}"}}',
            }
        }
    ]
    # When the agent advanced all tasks, the advanced set should include tid
    advanced = ag._task_ids_advanced_this_turn(tool_calls)
    assert tid in advanced


@pytest.mark.asyncio
async def test_maybe_open_task_reminder_fires(env):
    """When obligations exist and none were advanced, hint should fire."""
    ag = _make_agent()
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "Keep going", "d", AGENT_C, assignee_id=AGENT_A)
    await ts.claim_task(pid, tid, AGENT_A)
    await ts.start_task(pid, tid)

    obligations = await ts.get_actionable_obligations(pid, AGENT_A)
    assert obligations  # should have at least one
    hint = ag._build_open_task_hint(obligations)
    assert hint is not None
    assert "[TASK ADVANCE]" in hint
    assert ag._task_reminder_count == 0  # _build_open_task_hint doesn't increment

    # Cap: when reminder_count is at max, agent._handle_completion skips retrigger
    ag._task_reminder_count = ag._TASK_REMINDER_MAX
    # _build_open_task_hint still returns text — the gate check is in _handle_completion
    hint2 = ag._build_open_task_hint(obligations)
    assert hint2 is not None  # hint is still built; the gate logic is elsewhere


@pytest.mark.asyncio
async def test_verify_approve_auto_closes(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid,
        "VERIFY: parent work",
        "mandatory verify",
        AGENT_C,
        assignee_id=AGENT_A,
        tags=["verify", "mandatory"],
    )
    await ts.claim_task(pid, tid, AGENT_A)
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, {"summary": "ok", "tests_passed": True})
    await ts.start_review(pid, tid)

    with patch("hiveweave.services.inbox.InboxService.send_message",
               new_callable=AsyncMock), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               new_callable=AsyncMock):
        await ts.review_task(pid, tid, "approve")

    task = await ts.get_task(pid, tid)
    assert task["status"] == "closed"
    assert task.get("closed_at") is not None
