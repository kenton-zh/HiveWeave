"""Waiting on a dispatched child parks the coordinator's claimed umbrella."""

from __future__ import annotations

from hiveweave.services.turn_exit import (
    ExitContext,
    assignee_must_submit,
    evaluate_turn_exit,
    waiting_covers_assignee_task,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)

PARENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWN = "dddddddd-dddd-dddd-dddd-dddddddddddd"
CHILD = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
COORD = "coord-agent-id"


def _waiting(ref: str) -> list[dict]:
    return [{"kind": "task", "ref": ref}]


def _child(parent: str | None = PARENT) -> dict:
    return {
        "id": CHILD,
        "parent_task_id": parent,
        "assignee_id": "leaf-id",
        "creator_id": COORD,
        "status": "claimed",
    }


def test_parent_link_covers_claimed_umbrella():
    waiting = _waiting(CHILD)
    delegated = [_child(PARENT)]
    assert waiting_covers_assignee_task(
        waiting,
        PARENT,
        delegated_in_flight=delegated,
        assignee_status="claimed",
        claimed_assignee_ids=[PARENT],
    )
    assert not assignee_must_submit(
        "waiting",
        [PARENT],
        waiting,
        delegated_in_flight=delegated,
        assignee_status_by_id={PARENT: "claimed"},
    )


def test_missing_parent_link_covers_unique_claimed_only():
    waiting = _waiting(CHILD)
    delegated = [_child(None)]
    assert waiting_covers_assignee_task(
        waiting,
        PARENT,
        delegated_in_flight=delegated,
        assignee_status="claimed",
        claimed_assignee_ids=[PARENT],
    )
    assert not waiting_covers_assignee_task(
        waiting,
        PARENT,
        delegated_in_flight=delegated,
        assignee_status="claimed",
        claimed_assignee_ids=[PARENT, OWN],
    )
    assert assignee_must_submit(
        "waiting",
        [PARENT],
        waiting,
        delegated_in_flight=delegated,
        assignee_status_by_id={PARENT: "running"},
    )


def test_linked_child_does_not_park_other_claimed_task():
    waiting = _waiting(CHILD)
    delegated = [_child(PARENT)]
    assert waiting_covers_assignee_task(
        waiting,
        PARENT,
        delegated_in_flight=delegated,
        assignee_status="claimed",
        claimed_assignee_ids=[PARENT, OWN],
    )
    assert not waiting_covers_assignee_task(
        waiting,
        OWN,
        delegated_in_flight=delegated,
        assignee_status="claimed",
        claimed_assignee_ids=[PARENT, OWN],
    )
    assert assignee_must_submit(
        "waiting",
        [PARENT, OWN],
        waiting,
        delegated_in_flight=delegated,
        assignee_status_by_id={PARENT: "claimed", OWN: "claimed"},
    )


def test_unrelated_wait_does_not_cover():
    waiting = _waiting("cccccccccccccccc-cccc-cccc-cccc-cccccccccccc")
    delegated = [_child(PARENT)]
    assert assignee_must_submit(
        "waiting",
        [PARENT],
        waiting,
        delegated_in_flight=delegated,
        assignee_status_by_id={PARENT: "claimed"},
    )


def test_done_slice_never_parks_on_child_wait():
    waiting = _waiting(CHILD)
    delegated = [_child(PARENT)]
    assert assignee_must_submit(
        "done_slice",
        [PARENT],
        waiting,
        delegated_in_flight=delegated,
        assignee_status_by_id={PARENT: "claimed"},
    )


def test_waiting_on_parent_itself_still_covers():
    waiting = _waiting(PARENT)
    assert not assignee_must_submit(
        "waiting",
        [PARENT],
        waiting,
        delegated_in_flight=[],
        assignee_status_by_id={PARENT: "claimed"},
    )


def test_evaluate_exit_ok_when_waiting_on_dispatched_child():
    set_pending_turn_result(
        COORD,
        {
            "phase": "waiting",
            "summary": "wait for leaf submit",
            "waiting_on": [{"kind": "task", "ref": CHILD}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=COORD,
                project_id="proj",
                tool_calls=[],
                open_task_obligations=[
                    {
                        "id": PARENT,
                        "status": "claimed",
                        "role_hint": "assignee",
                        "title": "umbrella",
                    }
                ],
                delegated_in_flight=[_child(PARENT)],
            )
        )
        assert decision.ok
        assert "ASSIGNEE_MUST_SUBMIT" not in decision.violations
        assert decision.disposition == "waiting_agent"
    finally:
        clear_pending_turn_result(COORD)


def test_evaluate_exit_two_claimed_only_parks_linked_parent():
    set_pending_turn_result(
        COORD,
        {
            "phase": "waiting",
            "summary": "wait for leaf",
            "waiting_on": [{"kind": "task", "ref": CHILD}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=COORD,
                project_id="proj",
                tool_calls=[],
                open_task_obligations=[
                    {
                        "id": PARENT,
                        "status": "claimed",
                        "role_hint": "assignee",
                        "title": "umbrella",
                    },
                    {
                        "id": OWN,
                        "status": "claimed",
                        "role_hint": "assignee",
                        "title": "own coding",
                    },
                ],
                delegated_in_flight=[_child(PARENT)],
            )
        )
        assert not decision.ok
        assert "ASSIGNEE_MUST_SUBMIT" in decision.violations
    finally:
        clear_pending_turn_result(COORD)


def test_evaluate_exit_repairs_when_waiting_on_nothing_useful():
    set_pending_turn_result(
        COORD,
        {
            "phase": "waiting",
            "summary": "wait somehow",
            "waiting_on": [{"kind": "task", "ref": "unrelated-task-id-0001"}],
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=COORD,
                project_id="proj",
                tool_calls=[],
                open_task_obligations=[
                    {
                        "id": PARENT,
                        "status": "claimed",
                        "role_hint": "assignee",
                        "title": "umbrella",
                    }
                ],
                delegated_in_flight=[_child(PARENT)],
            )
        )
        assert not decision.ok
        assert "ASSIGNEE_MUST_SUBMIT" in decision.violations
        assert "子任务" in decision.hint
        assert "催" in decision.hint
    finally:
        clear_pending_turn_result(COORD)
