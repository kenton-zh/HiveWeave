"""Wake policy — any message wakes (progress is label-only)."""

from hiveweave.agents.trigger import _has_task_gate_messages
from hiveweave.services.wake_policy import should_wake


def test_complete_still_wakes_on_progress():
    assert should_wake("progress", disposition="complete") is True


def test_complete_allows_user():
    assert should_wake(
        "command", disposition="complete", from_agent_id="user"
    ) is True


def test_complete_allows_task_transition():
    assert should_wake(
        "task_transition", disposition="complete", from_agent_id="peer-1"
    ) is True


def test_complete_allows_peer_command():
    assert should_wake(
        "command", disposition="complete", from_agent_id="peer-1"
    ) is True


def test_complete_allows_peer_ask():
    assert should_wake(
        "ask", disposition="complete", from_agent_id="peer-1"
    ) is True


def test_waiting_human_allows_peer_command():
    assert should_wake(
        "command", disposition="waiting_human", from_agent_id="peer-1"
    ) is True


def test_waiting_human_allows_task_transition():
    assert should_wake(
        "task_transition",
        disposition="waiting_human",
        from_agent_id="executor-1",
    ) is True


def test_task_transition_wakes_with_agent_wait():
    waits = [
        {
            "kind": "agent",
            "ref": "天线",
            "wake_on": ["ask_reply", "message_from_ref", "timeout"],
        }
    ]
    assert (
        should_wake(
            "task_transition",
            disposition="waiting_human",
            from_agent_id="executor-1",
            from_agent_name="星野",
            from_short_id="A004",
            active_waits=waits,
        )
        is True
    )


def test_peer_command_wakes_with_unmatched_agent_wait():
    waits = [
        {
            "kind": "agent",
            "ref": "天线",
            "wake_on": ["ask_reply", "message_from_ref", "timeout"],
        }
    ]
    assert (
        should_wake(
            "command",
            disposition="waiting_human",
            from_agent_id="peer-1",
            from_agent_name="星野",
            active_waits=waits,
        )
        is True
    )


def test_has_task_gate_messages_detects_submitted():
    assert _has_task_gate_messages(
        [{"message": "[TASK SUBMITTED] Task 'x' has been submitted"}]
    )
    assert _has_task_gate_messages(
        [{"message": "[REWORK REQUESTED] Task 'x' needs rework"}]
    )
    assert _has_task_gate_messages(
        [{"message": "fyi only", "message_type": "task", "task_id": "t1"}]
    )
    assert not _has_task_gate_messages(
        [{"message": "全部完成，无需处理", "message_type": "notify"}]
    )
    assert not _has_task_gate_messages([])
