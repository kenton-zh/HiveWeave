"""T2 gate regressions: self-merge ownership, waiver identity, fail-count max, reply_contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.attestation import count_reported_test_failures
from hiveweave.services.inbox import InboxService


def test_count_reported_test_failures_takes_max():
    text = "Test Suites: 0 failed, 2 passed\nTests: 3 failed, 10 passed"
    assert count_reported_test_failures(text) == 3


def test_count_reported_test_failures_none_when_absent():
    assert count_reported_test_failures("all green") is None
    assert count_reported_test_failures("") is None


def test_row_to_msg_includes_reply_contract_id():
    row = {
        "id": "m1",
        "from_agent_id": "a",
        "to_agent_id": "b",
        "message": "hi",
        "read": 0,
        "created_at": 1,
        "message_type": "ask",
        "expect_report": 1,
        "priority": "normal",
        "task_id": None,
        "wake": 1,
        "parked": 0,
        "wake_category": None,
        "triage_batch_id": None,
        "reply_contract_id": "rc-abc",
        "reply_to": None,
    }

    class _R(dict):
        def keys(self):
            return dict.keys(self)

    msg = InboxService._row_to_msg(_R(row))
    assert msg["reply_contract_id"] == "rc-abc"
    assert msg.get("reply_to") is None


@pytest.mark.asyncio
async def test_self_merge_gate_rejects_foreign_task_id():
    from hiveweave.tools.misc_tools import _check_self_merge_gate

    foreign = {
        "id": "aaaaaaaa-1111-2222-3333-444444444444",
        "assignee_id": "other-agent",
        "status": "approved",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.get_task = AsyncMock(return_value=foreign)
        err = await _check_self_merge_gate(
            "p1",
            "me-agent",
            foreign["id"],
            "hw/ME01/work",
        )
    assert err is not None
    assert "not assigned to you" in err or "another agent" in err


@pytest.mark.asyncio
async def test_self_merge_gate_rejects_empty_assignee():
    from hiveweave.tools.misc_tools import _check_self_merge_gate

    orphan = {
        "id": "bbbbbbbb-1111-2222-3333-444444444444",
        "assignee_id": None,
        "status": "approved",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    with patch("hiveweave.services.task.TaskService") as TS:
        TS.return_value.get_task = AsyncMock(return_value=orphan)
        err = await _check_self_merge_gate(
            "p1",
            "me-agent",
            orphan["id"],
            "hw/ME01/work",
        )
    assert err is not None
    assert "no assignee" in err


@pytest.mark.asyncio
async def test_review_self_forbidden_even_with_waiver():
    """Waiver must NOT short-circuit assignee==reviewer identity gate."""
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-v",
        "title": "VERIFY: x",
        "tags": ["verify"],
        "assignee_id": "qa1",
        "status": "submitted",
        "parent_task_id": "t-parent",
        "evidence": {},
    }
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value={"id": "w1", "waived_by": "ceo1"}),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=True)
        result = await review_task_tool(
            ReviewTaskParams(taskId="t-v", decision="approve", comment="lgtm"),
            agent_id="qa1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    assert "Self-review is forbidden" in out
    assert "勿以 cancel 清场" in out


@pytest.mark.asyncio
async def test_non_verify_self_review_still_gets_soft_reminder_with_waiver():
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-code",
        "title": "Implement feature",
        "tags": [],
        "assignee_id": "exec1",
        "status": "submitted",
        "evidence": {},
    }
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value={"id": "w1", "waived_by": "ceo1"}),
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(taskId="t-code", decision="approve", comment="ok"),
            agent_id="exec1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "勿以 cancel 清场" in (result.output or result.error or "")
