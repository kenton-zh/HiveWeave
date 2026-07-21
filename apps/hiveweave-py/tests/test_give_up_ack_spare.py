"""BUGFIX: give-up ACK must spare review-critical inbox; park exempts escalation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.inbox import (
    should_exempt_from_park,
    should_spare_from_give_up_ack,
)


def test_spare_task_submitted():
    assert should_spare_from_give_up_ack(
        {
            "message": "[TASK SUBMITTED] Task 'x' has been submitted",
            "message_type": "task",
            "expect_report": False,
        }
    )


def test_spare_escalation_and_ask():
    assert should_spare_from_give_up_ack(
        {"message": "[ESCALATION] boom", "message_type": "escalation"}
    )
    assert should_spare_from_give_up_ack(
        {"message": "please reply", "message_type": "ask", "expect_report": True}
    )
    assert should_spare_from_give_up_ack(
        {"message": "hi", "message_type": "normal", "expect_report": True}
    )


def test_ack_ordinary_progress():
    assert not should_spare_from_give_up_ack(
        {
            "message": "FYI done",
            "message_type": "notify",
            "expect_report": False,
        }
    )
    assert not should_spare_from_give_up_ack(
        {"message": "认领任务继续干", "message_type": "normal"}
    )


def test_park_exempts_escalation_and_submitted():
    assert should_exempt_from_park(
        {"message": "[ESCALATION] x", "message_type": "escalation"}
    )
    assert should_exempt_from_park(
        {"message": "[TASK SUBMITTED] x", "message_type": "task"}
    )
    assert should_exempt_from_park(
        {"message": "[LEDGER REVIEW] x", "message_type": "task"}
    )
    assert not should_exempt_from_park(
        {"message": "普通派活", "message_type": "normal"}
    )


@pytest.mark.asyncio
async def test_partition_give_up_ack():
    from hiveweave.services.inbox import InboxService

    svc = InboxService()
    rows = [
        {
            "id": "a1",
            "message": "[TASK SUBMITTED] t",
            "message_type": "task",
            "expect_report": False,
        },
        {
            "id": "a2",
            "message": "noise",
            "message_type": "normal",
            "expect_report": False,
        },
        {
            "id": "a3",
            "message": "q",
            "message_type": "ask",
            "expect_report": True,
        },
    ]

    async def fake_get(agent_id, ids):
        by = {r["id"]: r for r in rows}
        return [by[i] for i in ids if i in by]

    with patch.object(svc, "get_messages_by_ids", side_effect=fake_get):
        ack, spare = await svc.partition_give_up_ack("agent", ["a1", "a2", "a3"])
    assert ack == ["a2"]
    assert spare == ["a1", "a3"]


@pytest.mark.asyncio
async def test_park_pending_skips_escalation():
    from hiveweave.services.inbox import InboxService

    svc = InboxService()
    pending = [
        {
            "id": "e1",
            "message": "[ESCALATION] help",
            "message_type": "escalation",
            "expect_report": 0,
        },
        {
            "id": "n1",
            "message": "普通未读",
            "message_type": "normal",
            "expect_report": 0,
        },
    ]

    executed: list = []

    async def fake_query(agent_id, sql, params=None):
        return pending

    class FakeCursor:
        rowcount = 1

        async def close(self):
            return None

    class FakeConn:
        async def execute(self, sql, params=None):
            executed.append((sql, list(params or [])))
            return FakeCursor()

        async def commit(self):
            return None

    with (
        patch("hiveweave.services.inbox._ensure_schema", AsyncMock()),
        patch("hiveweave.services.inbox.project_db.query", side_effect=fake_query),
        patch(
            "hiveweave.services.inbox.project_db.get_project_db_for_agent",
            AsyncMock(return_value=FakeConn()),
        ),
    ):
        n = await svc.park_pending_wakes("agent-1")

    assert n == 1
    assert executed
    # Only ordinary message id parked
    assert "n1" in executed[0][1]
    assert "e1" not in executed[0][1]


@pytest.mark.asyncio
async def test_ack_inbox_on_give_up_spares_and_injects_ledger():
    from hiveweave.agents.agent import Agent

    ag = MagicMock(spec=Agent)
    ag.id = "coord-1"
    ag.project_id = "p1"
    ag._consecutive_errors = 4
    ag._inbox = MagicMock()
    ag._inbox.partition_give_up_ack = AsyncMock(
        return_value=(["noise"], ["sub1"])
    )
    ag._inbox.mark_read_by_ids = AsyncMock()
    ag._inbox.ensure_wake = AsyncMock()
    ag._inject_ledger_review_wake = AsyncMock()

    # Bind real method
    await Agent._ack_inbox_on_give_up(ag, ["noise", "sub1"])

    ag._inbox.mark_read_by_ids.assert_awaited_once_with("coord-1", ["noise"])
    ag._inbox.ensure_wake.assert_awaited_once_with("coord-1", ["sub1"])
    ag._inject_ledger_review_wake.assert_awaited_once()
