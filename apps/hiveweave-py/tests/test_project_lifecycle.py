"""Tests for project deactivate/activate lifecycle (park + stop + resume)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.project_lifecycle import (
    OFF_DUTY_CANCEL_REASON,
    deliver_resume_briefings,
    park_project_inbox,
    stop_project_cleanly,
)


@pytest.mark.asyncio
async def test_park_project_inbox_sums_agent_parks():
    inbox = MagicMock()
    inbox.park_pending_wakes = AsyncMock(side_effect=[3, 2])
    with patch(
        "hiveweave.services.project_lifecycle.InboxService", return_value=inbox
    ):
        n = await park_project_inbox("proj", ["a1", "a2"])
    assert n == 5
    assert inbox.park_pending_wakes.await_count == 2


@pytest.mark.asyncio
async def test_stop_project_cleanly_cancels_with_off_duty():
    agent = MagicMock()
    agent.project_id = "proj"
    agent.cancel = AsyncMock()
    manager = MagicMock()
    manager._agents = {"a1": agent}
    manager.get_agent = MagicMock(return_value=agent)
    manager.stop_agent = AsyncMock()

    with (
        patch(
            "hiveweave.services.project_lifecycle._project_agent_ids",
            AsyncMock(return_value=["a1"]),
        ),
        patch(
            "hiveweave.agents.supervisor.agent_manager",
            manager,
        ),
    ):
        # re-import path used inside function
        with patch(
            "hiveweave.services.project_lifecycle.agent_manager",
            manager,
            create=True,
        ):
            # stop_project_cleanly imports agent_manager from supervisor
            import hiveweave.services.project_lifecycle as pl

            with patch(
                "hiveweave.agents.supervisor.agent_manager", manager
            ):
                result = await pl.stop_project_cleanly("proj")

    agent.cancel.assert_awaited()
    assert agent.cancel.await_args.kwargs.get("reason") == OFF_DUTY_CANCEL_REASON
    assert result["stopped"] >= 1


@pytest.mark.asyncio
async def test_deliver_resume_briefings_counts():
    inbox = MagicMock()
    inbox.deliver_parked_briefing = AsyncMock(
        side_effect=[(4, True), (0, False)]
    )
    with (
        patch(
            "hiveweave.services.project_lifecycle._project_agent_ids",
            AsyncMock(return_value=["a1", "a2"]),
        ),
        patch(
            "hiveweave.services.project_lifecycle.park_project_inbox",
            AsyncMock(return_value=7),
        ),
        patch(
            "hiveweave.services.project_lifecycle.InboxService",
            return_value=inbox,
        ),
    ):
        stats = await deliver_resume_briefings("proj")
    assert stats["briefed"] == 1
    assert stats["parked_cleared"] == 4
    assert stats["pre_parked"] == 7
