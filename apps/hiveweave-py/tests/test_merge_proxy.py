"""MERGE PROXY escalation + stale-ledger / duty-session nudge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.inbox import (
    should_exempt_from_park,
    should_spare_from_give_up_ack,
)


def test_merge_proxy_spared():
    msg = {
        "message": "[MERGE PROXY] Task 'x' approved",
        "message_type": "escalation",
    }
    assert should_spare_from_give_up_ack(msg)
    assert should_exempt_from_park(msg)


@pytest.mark.asyncio
async def test_escalate_merge_proxy_finds_coordinator_parent():
    from hiveweave.services.merge_proxy import escalate_merge_proxy

    agents = {
        "exec": {
            "id": "exec",
            "parent_id": "hr",
            "name": "E",
            "short_id": "A004",
            "permission_type": "executor",
            "role": "engineer",
        },
        "hr": {
            "id": "hr",
            "parent_id": "ceo",
            "name": "HR",
            "short_id": "A002",
            "permission_type": "coordinator",
            "role": "人力资源",
        },
        "ceo": {
            "id": "ceo",
            "parent_id": None,
            "name": "CEO",
            "short_id": "A001",
            "permission_type": "coordinator",
            "role": "CEO",
        },
        "mid": {
            "id": "mid",
            "parent_id": "ceo",
            "name": "Tech",
            "short_id": "A003",
            "permission_type": "coordinator",
            "role": "architect",
        },
    }
    task = {
        "id": "task-1",
        "title": "feat",
        "creator_id": "mid",
        "assignee_id": "exec",
        "status": "approved",
    }
    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {}

    with patch(
        "hiveweave.services.inbox.InboxService",
        return_value=FakeInbox(),
    ):
        with patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            new_callable=AsyncMock,
        ) as trig:
            parent = await escalate_merge_proxy(
                "p1", task, reason="overdue", agents_by_id=agents
            )

    assert parent == "ceo"
    assert sent and sent[0]["to_agent_id"] == "ceo"
    assert sent[0]["message"].startswith("[MERGE PROXY]")
    assert sent[0]["message_type"] == "escalation"
    trig.assert_awaited()


@pytest.mark.asyncio
async def test_nudge_stale_ledger_duty_session_and_off_duty():
    from hiveweave.services import game_time as gt

    project_id = "proj-duty-1"
    now = 1_700_000_000_000
    # Wall age huge (overnight) but duty session just started
    old_updated = now - 8 * 3600 * 1000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - 60_000,  # only 1 min on duty
        "silence_trackers": {},
    }

    tasks = [
        {
            "id": "t1",
            "creator_id": "coord",
            "assignee_id": "exec",
            "status": "submitted",
            "title": "overnight",
            "tags": [],
            "updated_at": old_updated,
        }
    ]
    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {}

    svc = gt.GameTimeService(project_id)
    svc._watchdog_trigger = AsyncMock()

    # Off duty → no-op
    with patch(
        "hiveweave.db.meta.query_one",
        new=AsyncMock(return_value={"is_started": 0}),
    ):
        await svc._nudge_stale_ledger(project_id)
    assert sent == []

    # On duty but session age < threshold → no nudge despite old updated_at
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
                    "hiveweave.services.org.OrgService.list_agents",
                    new=AsyncMock(
                        return_value=[
                            {
                                "id": "coord",
                                "short_id": "A001",
                                "parent_id": None,
                                "permission_type": "coordinator",
                                "role": "CEO",
                            }
                        ]
                    ),
                ):
                    with patch(
                        "hiveweave.services.inbox.InboxService",
                        return_value=FakeInbox(),
                    ):
                        with patch("time.time", return_value=now / 1000):
                            await svc._nudge_stale_ledger(project_id)
    assert sent == []

    # Session long enough → nudge
    gt._states[project_id]["duty_session_started_at_ms"] = (
        now - gt.LEDGER_REVIEW_STALE_MS - 1000
    )
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
                    "hiveweave.services.org.OrgService.list_agents",
                    new=AsyncMock(
                        return_value=[
                            {
                                "id": "coord",
                                "short_id": "A001",
                                "parent_id": None,
                                "permission_type": "coordinator",
                                "role": "CEO",
                            }
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

    gt._states.pop(project_id, None)


@pytest.mark.asyncio
async def test_nudge_merge_proxy_on_stale_approved():
    from hiveweave.services import game_time as gt

    project_id = "proj-proxy-1"
    now = 1_700_000_000_000
    gt._states[project_id] = {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": now - gt.MERGE_PROXY_STALE_MS - 1000,
        "silence_trackers": {},
    }
    tasks = [
        {
            "id": "ta",
            "creator_id": "mid",
            "assignee_id": "exec",
            "status": "approved",
            "title": "need proxy",
            "tags": [],
            "updated_at": now - gt.MERGE_PROXY_STALE_MS - 1000,
        }
    ]
    agents = [
        {
            "id": "mid",
            "parent_id": "ceo",
            "short_id": "A003",
            "name": "Mid",
            "permission_type": "coordinator",
            "role": "architect",
        },
        {
            "id": "ceo",
            "parent_id": None,
            "short_id": "A001",
            "name": "CEO",
            "permission_type": "coordinator",
            "role": "CEO",
        },
        {
            "id": "exec",
            "parent_id": "mid",
            "short_id": "A004",
            "name": "E",
            "permission_type": "executor",
            "role": "eng",
        },
    ]
    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {}

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
                        new=AsyncMock(return_value=agents),
                    ):
                        with patch(
                            "hiveweave.services.inbox.InboxService",
                            return_value=FakeInbox(),
                        ):
                            with patch(
                                "hiveweave.agents.trigger.trigger_coordinator",
                                new_callable=AsyncMock,
                            ):
                                with patch("time.time", return_value=now / 1000):
                                    await svc._nudge_stale_ledger(project_id)

    assert any(m["message"].startswith("[MERGE PROXY]") for m in sent)
    assert any(m["to_agent_id"] == "ceo" for m in sent)

    gt._states.pop(project_id, None)
