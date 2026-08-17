"""CEO [SHIP READY] nudge after VERIFY / last-task close."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.tasks.ship_nudge import (
    SHIP_READY_PREFIX,
    maybe_nudge_ceo_ship_ready,
    ship_anchor_id,
    ship_ready_message,
)


def test_verify_anchor_uses_parent():
    assert (
        ship_anchor_id(
            {
                "id": "verify-1",
                "parent_task_id": "parent-9",
                "title": "VERIFY: MAIN QA",
            }
        )
        == "parent-9"
    )


def test_non_verify_child_anchor_uses_parent():
    assert (
        ship_anchor_id(
            {
                "id": "leaf-1",
                "parent_task_id": "parent-9",
                "title": "implement the page",
            }
        )
        == "parent-9"
    )


def test_orphan_verify_anchor_is_self():
    assert (
        ship_anchor_id({"id": "verify-1", "title": "VERIFY: MAIN QA"})
        == "verify-1"
    )


def test_message_uses_platform_prefix():
    text = ship_ready_message(
        {"id": "be50cb1a-16f7-4bbb", "title": "VERIFY: MAIN"}
    )
    assert text.startswith(SHIP_READY_PREFIX)
    assert "message_user" in text
    assert "be50cb1a-16f7-4bbb" in text


@pytest.mark.asyncio
async def test_verify_close_sends_and_triggers():
    send = AsyncMock(
        return_value={"should_wake": True, "deduped": False, "id": "m1"}
    )
    trigger = AsyncMock()
    ceo_rows = [{"id": "ceo-uuid"}]

    async def fake_query(_pid, sql, _params):
        if "role" in sql:
            return ceo_rows
        return []

    with (
        patch(
            "hiveweave.services.tasks.ship_nudge._query",
            fake_query,
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            send,
        ),
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            trigger,
        ),
    ):
        await maybe_nudge_ceo_ship_ready(
            "proj-1",
            {
                "id": "v1",
                "parent_task_id": "p1",
                "title": "VERIFY: MAIN QA",
            },
        )

    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["to_agent_id"] == "ceo-uuid"
    assert kwargs["message"].startswith(SHIP_READY_PREFIX)
    assert kwargs["wake"] is True
    assert kwargs["message_type"] == "task"
    assert kwargs["idempotency_key"] == "ship-ready:proj-1:p1"
    trigger.assert_awaited_once_with("ceo-uuid")


@pytest.mark.asyncio
async def test_non_verify_skips_when_ledger_still_open():
    send = AsyncMock()

    async def fake_query(_pid, sql, _params):
        if "FROM tasks" in sql:
            return [{"id": "still-open"}]
        return [{"id": "ceo-uuid"}]

    with (
        patch(
            "hiveweave.services.tasks.ship_nudge._query",
            fake_query,
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            send,
        ),
    ):
        await maybe_nudge_ceo_ship_ready(
            "proj-1",
            {"id": "parent-1", "title": "deliver the page"},
        )

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_deduped_ship_ready_does_not_retrigger():
    send = AsyncMock(
        return_value={"should_wake": False, "deduped": True, "id": "m1"}
    )
    trigger = AsyncMock()

    async def fake_query(_pid, sql, _params):
        if "role" in sql:
            return [{"id": "ceo-uuid"}]
        return []

    with (
        patch(
            "hiveweave.services.tasks.ship_nudge._query",
            fake_query,
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            send,
        ),
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            trigger,
        ),
    ):
        await maybe_nudge_ceo_ship_ready(
            "proj-1",
            {"id": "v1", "parent_task_id": "p1", "title": "VERIFY: x"},
        )

    trigger.assert_not_awaited()
