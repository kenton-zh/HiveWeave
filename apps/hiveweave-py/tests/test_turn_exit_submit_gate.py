"""turn_exit — ASSIGNEE_MUST_SUBMIT / CREATOR_MUST_REVIEW repair gates."""

from hiveweave.services.turn_exit import (
    ExitContext,
    evaluate_turn_exit,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)


AGENT_ID = "exit-agent"
PROJECT_ID = "exit-project"


def _ctx(**kwargs):
    base = dict(
        agent_id=AGENT_ID,
        project_id=PROJECT_ID,
        tool_calls=[],
        pending_inbox_msgs=[],
        unreplied_asks=[],
        open_task_obligations=[],
        tasks_advanced=set(),
    )
    base.update(kwargs)
    return ExitContext(**base)


def test_done_slice_running_task_requires_submit():
    set_pending_turn_result(
        AGENT_ID, {"phase": "done_slice", "summary": "claimed done"}
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(
                open_task_obligations=[
                    {
                        "id": "task-aaaaaaaa",
                        "status": "running",
                        "role_hint": "assignee",
                    }
                ]
            )
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert decision.ok is False
    assert "ASSIGNEE_MUST_SUBMIT" in decision.violations
    assert decision.should_repair is True


def test_waiting_with_task_ref_allows_idle():
    set_pending_turn_result(
        AGENT_ID,
        {
            "phase": "waiting",
            "summary": "waiting on task",
            "waiting_on": [{"kind": "task", "ref": "task-aaaaaaaa"}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(
                open_task_obligations=[
                    {
                        "id": "task-aaaaaaaa",
                        "status": "running",
                        "role_hint": "assignee",
                    }
                ]
            )
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert "ASSIGNEE_MUST_SUBMIT" not in decision.violations
    assert decision.ok is True


def test_creator_must_review_on_done_slice():
    set_pending_turn_result(
        AGENT_ID, {"phase": "done_slice", "summary": "done reviewing?"}
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(
                open_task_obligations=[
                    {
                        "id": "task-bbbbbbbb",
                        "status": "submitted",
                        "role_hint": "creator",
                    }
                ]
            )
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert "CREATOR_MUST_REVIEW" in decision.violations
    assert decision.should_repair is True


def test_waiting_on_user_with_running_task_requires_submit():
    """waiting_human is not an escape hatch for undeclared running work."""
    set_pending_turn_result(
        AGENT_ID,
        {
            "phase": "waiting",
            "summary": "waiting on user",
            "waiting_on": [{"kind": "user", "ref": "user"}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(
                open_task_obligations=[
                    {
                        "id": "task-aaaaaaaa",
                        "status": "running",
                        "role_hint": "assignee",
                    }
                ]
            )
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert "ASSIGNEE_MUST_SUBMIT" in decision.violations
    assert decision.should_repair is True


def test_submit_advances_clears_assignee_gate():
    set_pending_turn_result(
        AGENT_ID, {"phase": "done_slice", "summary": "submitted"}
    )
    try:
        decision = evaluate_turn_exit(
            _ctx(
                open_task_obligations=[
                    {
                        "id": "task-aaaaaaaa",
                        "status": "running",
                        "role_hint": "assignee",
                    }
                ],
                tasks_advanced={"task-aaaaaaaa"},
            )
        )
    finally:
        clear_pending_turn_result(AGENT_ID)

    assert decision.ok is True
    assert decision.violations == []
