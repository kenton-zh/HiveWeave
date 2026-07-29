"""Soft unblock: forbid deadlock cancel + soft reminder; no next-actor routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.unblock_soft import (
    REVIEW_PATH_BLOCKED_REMINDER,
    review_deadlock_blocks_cancel,
    soft_reminder_after_self_review_deny,
)


def test_soft_reminder_only_when_waiver():
    assert soft_reminder_after_self_review_deny(has_waiver=False) == ""
    assert REVIEW_PATH_BLOCKED_REMINDER in soft_reminder_after_self_review_deny(
        has_waiver=True
    )


@pytest.mark.asyncio
async def test_cancel_forbid_when_waiver_in_review_pipe():
    task = {"id": "t-deadlock", "status": "submitted", "evidence": {}}
    with patch(
        "hiveweave.services.attestation.get_valid_waiver",
        AsyncMock(return_value={"id": "w1"}),
    ):
        msg = await review_deadlock_blocks_cancel("p1", task)
    assert msg is not None
    assert "cancel_task refused" in msg
    assert REVIEW_PATH_BLOCKED_REMINDER in msg
    # Must NOT include structured next-actor commands
    assert "next_actor" not in msg
    assert "REVIEW ASSIGNED" not in msg


@pytest.mark.asyncio
async def test_cancel_forbid_when_evidence_attestations():
    task = {
        "id": "t2",
        "status": "reviewing",
        "evidence": {"attestation_ids": ["att-1"], "tests_passed": False},
    }
    with patch(
        "hiveweave.services.attestation.get_valid_waiver",
        AsyncMock(return_value=None),
    ):
        msg = await review_deadlock_blocks_cancel("p1", task)
    assert msg is not None
    assert REVIEW_PATH_BLOCKED_REMINDER in msg


@pytest.mark.asyncio
async def test_cancel_allowed_for_created_without_evidence():
    task = {"id": "t3", "status": "created", "evidence": {}}
    with patch(
        "hiveweave.services.attestation.get_valid_waiver",
        AsyncMock(return_value=None),
    ):
        assert await review_deadlock_blocks_cancel("p1", task) is None


@pytest.mark.asyncio
async def test_cancel_tool_refuses_review_deadlock():
    from hiveweave.tools.task_tools import CancelTaskParams, cancel_task_tool

    task = {
        "id": "t-dead",
        "status": "submitted",
        "evidence": {"tests_passed": True},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value.archive_task = AsyncMock(return_value="submitted")
        result = await cancel_task_tool(
            CancelTaskParams(
                taskId="t-dead",
                reason="stuck in review forever please clear",
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "cancel_task refused" in (result.output or result.error or "")
    TS.return_value.archive_task.assert_not_awaited()
