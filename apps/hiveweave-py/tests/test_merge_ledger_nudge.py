"""Merge obligation + ledger re-nudge (CREATOR_MUST_MERGE / stale ledger)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.hooks.handlers.task_advance import (
    build_task_advance_hint,
    decide_task_advance_nudge,
)
from hiveweave.services.inbox import (
    should_exempt_from_park,
    should_spare_from_give_up_ack,
)
from hiveweave.services.turn_exit import (
    REPAIR_VIOLATIONS,
    ExitContext,
    evaluate_turn_exit,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)


def test_merge_pending_spared_and_park_exempt():
    msg = {
        "message": "[MERGE PENDING] Task 'x' needs merge",
        "message_type": "task",
        "expect_report": False,
    }
    assert should_spare_from_give_up_ack(msg)
    assert should_exempt_from_park(msg)
    assert should_spare_from_give_up_ack(
        {
            "message": "[PEER_REVIEW_DEADLOCK] A↔B",
            "message_type": "escalation",
        }
    )


def test_creator_must_merge_in_repair_set():
    assert "CREATOR_MUST_MERGE" in REPAIR_VIOLATIONS


def test_turn_exit_creator_must_merge():
    agent_id = "coord-merge-1"
    set_pending_turn_result(
        agent_id, {"phase": "done_slice", "summary": "done reviewing"}
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=agent_id,
                project_id="p1",
                tool_calls=[],
                open_task_obligations=[
                    {
                        "id": "task-approved-001",
                        "title": "engine",
                        "status": "approved",
                        "role_hint": "creator",
                    }
                ],
                tasks_advanced=set(),
            )
        )
    finally:
        clear_pending_turn_result(agent_id)
    assert not decision.ok
    assert "CREATOR_MUST_MERGE" in decision.violations
    assert decision.should_repair
    assert "git_worktree_merge" in decision.hint


def test_turn_exit_ok_when_approved_not_in_obligations():
    agent_id = "coord-merge-2"
    set_pending_turn_result(
        agent_id, {"phase": "done_slice", "summary": "merged"}
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=agent_id,
                project_id="p1",
                tool_calls=[],
                open_task_obligations=[],
                tasks_advanced=set(),
            )
        )
    finally:
        clear_pending_turn_result(agent_id)
    assert decision.ok


def test_task_advance_hint_for_approved():
    hint = build_task_advance_hint(
        [
            {
                "id": "aaaaaaaa-bbbb",
                "title": "feature",
                "status": "approved",
                "role_hint": "creator",
            }
        ]
    )
    assert "git_worktree_merge" in hint


def test_task_advance_nudge_approved():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[
            {
                "id": "cccccccc-dddd",
                "title": "x",
                "status": "approved",
                "role_hint": "creator",
            }
        ],
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
    assert "git_worktree_merge" in hint


@pytest.mark.asyncio
async def test_get_actionable_obligations_includes_approved():
    from hiveweave.services.task import TaskService

    svc = TaskService()
    rows = [
        {
            "id": "t-approved",
            "creator_id": "c1",
            "assignee_id": "a1",
            "status": "approved",
            "title": "feat",
            "tags": None,
            "is_archived": 0,
        },
        {
            "id": "t-verify",
            "creator_id": "c1",
            "assignee_id": "a1",
            "status": "approved",
            "title": "VERIFY: feat",
            "tags": '["verify"]',
            "is_archived": 0,
        },
        {
            "id": "t-submitted",
            "creator_id": "c1",
            "assignee_id": "a1",
            "status": "submitted",
            "title": "other",
            "tags": None,
            "is_archived": 0,
        },
    ]

    async def fake_query(project_id, sql, params=None):
        return rows

    with patch("hiveweave.services.task._ensure_schema", new_callable=AsyncMock):
        with patch("hiveweave.services.task._query", side_effect=fake_query):
            out = await svc.get_actionable_obligations("p1", "c1")

    ids = {t["id"] for t in out}
    assert "t-approved" in ids
    assert "t-submitted" in ids
    assert "t-verify" not in ids
    approved = next(t for t in out if t["id"] == "t-approved")
    assert approved["role_hint"] == "creator"


@pytest.mark.asyncio
async def test_nudge_stale_ledger_review_and_merge():
    from hiveweave.services import game_time as gt

    project_id = "proj-ledger-1"
    now = 1_700_000_000_000
    stale_review = now - gt.LEDGER_REVIEW_STALE_MS - 1000
    stale_merge = now - gt.MERGE_PENDING_STALE_MS - 1000

    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.LEDGER_REVIEW_STALE_MS - 1000,
        "silence_trackers": {},
    }

    tasks = [
        {
            "id": "rev-1",
            "creator_id": "coord",
            "assignee_id": "exec",
            "status": "submitted",
            "title": "need review",
            "tags": [],
            "updated_at": stale_review,
        },
        {
            "id": "mer-1",
            "creator_id": "coord",
            "assignee_id": "exec",
            "status": "approved",
            "title": "need merge",
            "tags": [],
            "updated_at": stale_merge,
        },
        {
            "id": "fresh",
            "creator_id": "coord",
            "assignee_id": "exec",
            "status": "submitted",
            "title": "fresh",
            "tags": [],
            "updated_at": now,
        },
    ]

    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"id": "m1", "should_wake": True}

    svc = gt.GameTimeService(project_id)
    svc._watchdog_trigger = AsyncMock()

    with patch(
        "hiveweave.db.meta.query_one",
        new=AsyncMock(return_value={"is_started": 1}),
    ):
        with patch(
            "hiveweave.services.system_state.system_state.paused",
            return_value=False,
        ):
            with patch(
                "hiveweave.services.task.TaskService.list_tasks",
                new=AsyncMock(return_value=tasks),
            ):
                with patch(
                    "hiveweave.services.task.TaskService._is_verify_task",
                    staticmethod(lambda t: False),
                ):
                    with patch(
                        "hiveweave.services.org.OrgService.list_agents",
                        new=AsyncMock(
                            return_value=[
                                {
                                    "id": "coord",
                                    "short_id": "A001",
                                    "parent_id": None,
                                    "permission_type": "coordinator",
                                    "role": "CEO",
                                },
                                {
                                    "id": "exec",
                                    "short_id": "A002",
                                    "parent_id": "coord",
                                    "permission_type": "executor",
                                    "role": "eng",
                                },
                            ]
                        ),
                    ):
                        with patch(
                            "hiveweave.services.inbox.InboxService",
                            return_value=FakeInbox(),
                        ):
                            with patch("time.time", return_value=now / 1000):
                                await svc._nudge_stale_ledger(project_id)

    assert any(m["message"].startswith("[LEDGER REVIEW]") for m in sent)
    assert any(m["message"].startswith("[MERGE PENDING]") for m in sent)
    assert not any("fresh" in m["message"] for m in sent)
    assert svc._watchdog_trigger.await_count >= 2
    sent.clear()
    with patch(
        "hiveweave.db.meta.query_one",
        new=AsyncMock(return_value={"is_started": 1}),
    ):
        with patch(
            "hiveweave.services.system_state.system_state.paused",
            return_value=False,
        ):
            with patch(
                "hiveweave.services.task.TaskService.list_tasks",
                new=AsyncMock(return_value=tasks),
            ):
                with patch(
                    "hiveweave.services.task.TaskService._is_verify_task",
                    staticmethod(lambda t: False),
                ):
                    with patch(
                        "hiveweave.services.org.OrgService.list_agents",
                        new=AsyncMock(return_value=[]),
                    ):
                        with patch(
                            "hiveweave.services.inbox.InboxService",
                            return_value=FakeInbox(),
                        ):
                            with patch("time.time", return_value=now / 1000):
                                await svc._nudge_stale_ledger(project_id)
    assert sent == []

    gt._states.pop(project_id, None)


@pytest.mark.asyncio
async def test_peer_review_deadlock_nudge():
    from hiveweave.services import game_time as gt

    project_id = "proj-peer-1"
    now = 1_700_000_000_000
    stale = now - gt.PEER_REVIEW_DEADLOCK_MS - 1000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.PEER_REVIEW_DEADLOCK_MS - 1000,
        "silence_trackers": {},
    }

    tasks = [
        {
            "id": "t-ab",
            "creator_id": "agent-a",
            "assignee_id": "agent-b",
            "status": "submitted",
            "title": "review B",
            "tags": ["peer_review"],
            "updated_at": stale,
        },
        {
            "id": "t-ba",
            "creator_id": "agent-b",
            "assignee_id": "agent-a",
            "status": "submitted",
            "title": "review A",
            "tags": ["peer_review"],
            "updated_at": stale,
        },
    ]
    agents = [
        {"id": "agent-a", "parent_id": "boss", "short_id": "A004"},
        {"id": "agent-b", "parent_id": "boss", "short_id": "A005"},
        {"id": "boss", "parent_id": None, "short_id": "A001"},
    ]
    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {}

    svc = gt.GameTimeService(project_id)
    svc._watchdog_trigger = AsyncMock()

    with patch(
        "hiveweave.services.inbox.InboxService",
        return_value=FakeInbox(),
    ):
        with patch("time.time", return_value=now / 1000):
            await svc._nudge_peer_review_deadlocks(
                project_id, agents=agents, tasks=tasks, now_ms=now
            )

    targets = {m["to_agent_id"] for m in sent}
    assert "agent-a" in targets
    assert "agent-b" in targets
    assert "boss" in targets
    assert all(
        m["message"].startswith("[PEER_REVIEW_DEADLOCK]") for m in sent
    )
    assert all(m["message_type"] == "escalation" for m in sent)

    gt._states.pop(project_id, None)
