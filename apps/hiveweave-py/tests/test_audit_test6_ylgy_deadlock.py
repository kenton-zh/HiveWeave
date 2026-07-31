"""TEST6 audit (2026-07-30): approval deadlock + attestation bind fixes.

Covers S1–S7 acceptance from audit-test6-ylgy-report:
- S1: CEO without TEST_RUN consumes assignee attestation
- S2: small-team waive→self-approve exemption
- S3/S6: deadlock / mismatch diagnostics on reject
- S4/S5: reviewer-path bind priority + multi-review refuse silent bind
- S7: cancel escape when no lawful approver
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.unblock_soft import (
    DEADLOCK_CANCEL_REASON_MIN,
    cancel_allowed_due_to_approve_deadlock,
    is_small_team_sole_reviewer,
    no_lawful_approver,
    review_deadlock_blocks_cancel,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _ceo(**extra):
    return {"id": "ceo1", "role": "ceo", "status": "active", **extra}


def _coord(**extra):
    return {
        "id": "coord1",
        "role": "frontend tech lead",
        "permission_type": "coordinator",
        "status": "active",
        **extra,
    }


def _exec(**extra):
    return {
        "id": "exec1",
        "role": "board engineer",
        "permission_type": "executor",
        "status": "active",
        **extra,
    }


# ── S2 / S3 / S7: small team + deadlock detection ─────────────────────────


@pytest.mark.asyncio
async def test_small_team_sole_reviewer_true_when_only_ceo():
    agents = [_ceo(), _coord(id="coord1"), _exec()]
    with patch("hiveweave.services.org.OrgService") as Org:
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        assert await is_small_team_sole_reviewer(
            "p1", assignee_id="coord1", reviewer_id="ceo1"
        )


@pytest.mark.asyncio
async def test_small_team_false_when_two_review_holders():
    agents = [
        _ceo(),
        _coord(id="coord1"),
        _coord(id="coord2", role="backend lead"),
        _exec(),
    ]
    with patch("hiveweave.services.org.OrgService") as Org:
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        assert not await is_small_team_sole_reviewer(
            "p1", assignee_id="exec1", reviewer_id="ceo1"
        )


@pytest.mark.asyncio
async def test_no_lawful_approver_when_waive_and_sole_ceo_without_exemption_path():
    """With waiver by CEO and another coordinator present → not deadlocked."""
    task = {"id": "t1", "assignee_id": "exec1", "status": "submitted"}
    agents = [_ceo(), _coord(), _exec()]
    with patch("hiveweave.services.org.OrgService") as Org:
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        # CEO waived; coord can still approve → no deadlock
        msg = await no_lawful_approver(
            "p1", task, waiver_row={"agent_id": "ceo1"}
        )
    assert msg is None


@pytest.mark.asyncio
async def test_no_lawful_approver_deadlock_when_only_waiving_ceo():
    """assignee=coord (has REVIEW but is assignee) + CEO waived → empty set
    without small-team exemption applied inside no_lawful_approver when sole."""
    task = {"id": "t1", "assignee_id": "coord1", "status": "submitted"}
    # Only CEO has REVIEW beside assignee-coord; small-team exemption applies
    # inside no_lawful_approver → NOT deadlocked (sole_ok).
    agents = [_ceo(), _coord(), _exec()]
    with patch("hiveweave.services.org.OrgService") as Org:
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        msg = await no_lawful_approver(
            "p1", task, waiver_row={"agent_id": "ceo1"}
        )
    assert msg is None  # small-team sole reviewer exemption


@pytest.mark.asyncio
async def test_cancel_allowed_when_approve_deadlocked():
    task = {
        "id": "t-dead",
        "status": "submitted",
        "assignee_id": "coord1",
        "evidence": {"tests_passed": True},
    }
    # No REVIEW holders at all beside assignee → deadlock
    agents = [_coord(), _exec()]  # coord is assignee; exec has no REVIEW
    with (
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value={"agent_id": "ghost"}),
        ),
    ):
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        assert await cancel_allowed_due_to_approve_deadlock("p1", task)


@pytest.mark.asyncio
async def test_cancel_escape_requires_long_reason():
    task = {
        "id": "t-dead",
        "status": "reviewing",
        "assignee_id": "coord1",
        "evidence": {"attestation_ids": ["a1"]},
    }
    agents = [_coord()]  # only assignee has REVIEW → empty holders
    with (
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        short = await review_deadlock_blocks_cancel(
            "p1", task, cancel_reason="too short"
        )
        assert short is not None
        assert "reason ≥" in short or str(DEADLOCK_CANCEL_REASON_MIN) in short

        long_reason = "x" * DEADLOCK_CANCEL_REASON_MIN
        ok = await review_deadlock_blocks_cancel(
            "p1", task, cancel_reason=long_reason
        )
        assert ok is None  # escape allowed


@pytest.mark.asyncio
async def test_cancel_escape_fail_closed_when_org_list_fails():
    """Org roster unreadable → keep cancel blocked (do not false-escape)."""
    task = {
        "id": "t-org-fail",
        "status": "submitted",
        "assignee_id": "coord1",
        "evidence": {"tests_passed": True},
    }
    with (
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.list_agents = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        msg = await review_deadlock_blocks_cancel(
            "p1", task, cancel_reason="x" * 40
        )
        assert msg is not None
        assert "org roster unreadable" in msg or "cannot determine" in msg
        assert await cancel_allowed_due_to_approve_deadlock("p1", task) is False


@pytest.mark.asyncio
async def test_no_lawful_approver_org_failure_is_sentinel():
    from hiveweave.services.unblock_soft import (
        is_org_lookup_failed,
        no_lawful_approver,
    )

    task = {"id": "t1", "assignee_id": "exec1", "status": "submitted"}
    with patch("hiveweave.services.org.OrgService") as Org:
        Org.return_value.list_agents = AsyncMock(side_effect=OSError("boom"))
        msg = await no_lawful_approver("p1", task)
    assert is_org_lookup_failed(msg)


@pytest.mark.asyncio
async def test_cancel_still_forbidden_when_lawful_approver_exists():
    task = {
        "id": "t2",
        "status": "submitted",
        "assignee_id": "exec1",
        "evidence": {"tests_passed": True},
    }
    agents = [_ceo(), _coord(), _exec()]
    with (
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        msg = await review_deadlock_blocks_cancel(
            "p1", task, cancel_reason="x" * 40
        )
    assert msg is not None
    assert "cancel_task refused" in msg


# ── S2: review_task small-team waive self-approve ─────────────────────────


@pytest.mark.asyncio
async def test_review_waive_self_approve_small_team_allowed():
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-code",
        "title": "Implement feature",
        "tags": [],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"tests_passed": True},
    }
    agents = [_ceo(), _coord(), _exec()]
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value={"id": "w1", "agent_id": "ceo1"}),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=True),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.task._execute",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            AsyncMock(return_value={"id": "m1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        Org.return_value.get_agent = AsyncMock(
            side_effect=lambda aid: _ceo() if aid == "ceo1" else _coord()
        )
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = MagicMock(return_value=False)
        ts.start_review = AsyncMock()
        ts.review_task = AsyncMock(return_value=task)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-code", decision="approve", comment="lgtm small team"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True, result.output or result.error


@pytest.mark.asyncio
async def test_review_waive_self_approve_blocked_when_other_reviewer():
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-code",
        "title": "Implement feature",
        "tags": [],
        "assignee_id": "exec1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {},
    }
    agents = [_ceo(), _coord(), _exec()]
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value={"id": "w1", "agent_id": "ceo1"}),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=True),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.list_agents = AsyncMock(return_value=agents)
        Org.return_value.get_agent = AsyncMock(return_value=_ceo())
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-code", decision="approve", comment="should fail"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    assert "issued the waiver" in out


# ── S1: CEO consume path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ceo_approve_via_consume_assignee_attestation():
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "Optimize game",
        "tags": [],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
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
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=True),
        ) as find_att,
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            AsyncMock(return_value={"id": "m1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            side_effect=lambda aid: _ceo() if aid == "ceo1" else _coord()
        )
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = MagicMock(return_value=False)
        ts.start_review = AsyncMock()
        ts.review_task = AsyncMock(return_value=task)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="consume ok"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True, result.output or result.error
    # CEO path: reviewer_must_hold=False
    kwargs = find_att.await_args.kwargs
    assert kwargs.get("reviewer_must_hold") is False
    assert "coord1" in (kwargs.get("consume_agent_ids") or [])


@pytest.mark.asyncio
async def test_ceo_approve_reject_explains_no_test_run_capability():
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "Optimize game",
        "tags": [],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
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
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.list_reviewer_attestations_diag",
            AsyncMock(
                return_value=[
                    {"id": "att-other", "kind": "test_run", "task_id": "other"}
                ]
            ),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.get_agent = AsyncMock(return_value=_ceo())
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="no evidence"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    assert "TEST_RUN" in out
    assert "mismatch" in out or "bound_task" in out


# ── S4 / S5: attestation bind priority ────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_prefers_reviewer_path_over_assignee():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    parent = {
        "id": "parent-1",
        "status": "running",
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "title": "parent",
    }
    child = {
        "id": "child-1",
        "status": "submitted",
        "assignee_id": "exec1",
        "creator_id": "coord1",
        "title": "child",
    }
    with patch("hiveweave.services.task.TaskService") as TS:
        ts = TS.return_value
        ts.list_tasks = AsyncMock(
            side_effect=lambda pid, assignee_id=None: (
                [parent] if assignee_id == "coord1" else [parent, child]
            )
        )
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "coord1", None
        )
    assert tid == "child-1"
    assert note == ""


@pytest.mark.asyncio
async def test_bind_refuses_silent_when_multiple_reviewing():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    children = [
        {
            "id": f"child-{i}",
            "status": "submitted",
            "assignee_id": f"e{i}",
            "creator_id": "coord1",
            "title": f"c{i}",
        }
        for i in range(3)
    ]
    parent = {
        "id": "parent-1",
        "status": "running",
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "title": "parent",
    }
    with patch("hiveweave.services.task.TaskService") as TS:
        ts = TS.return_value
        ts.list_tasks = AsyncMock(
            side_effect=lambda pid, assignee_id=None: (
                [parent] if assignee_id == "coord1" else [parent, *children]
            )
        )
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "coord1", None
        )
    assert tid is None
    assert "UNBOUND" in note
    assert "child-0" in note
    assert "taskId=" in note
