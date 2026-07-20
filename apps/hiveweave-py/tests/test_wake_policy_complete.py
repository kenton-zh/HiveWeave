"""wake_policy — complete disposition must not wake on progress alone.

TEST3: task_transition must pierce unmatched agent/user Wait Contracts.
"""

from hiveweave.agents.trigger import _has_task_gate_messages
from hiveweave.services.wake_policy import should_wake


def test_complete_blocks_progress():
    assert should_wake("progress", disposition="complete") is False


def test_complete_allows_user():
    assert should_wake(
        "command", disposition="complete", from_agent_id="user"
    ) is True


def test_complete_allows_task_transition():
    assert should_wake(
        "task_transition", disposition="complete", from_agent_id="peer-1"
    ) is True


def test_complete_blocks_peer_command():
    # Peer commands must not wake complete agents (TEST4 empty done_slice churn)
    assert should_wake(
        "command", disposition="complete", from_agent_id="peer-1"
    ) is False


def test_complete_blocks_peer_ask():
    assert should_wake(
        "ask", disposition="complete", from_agent_id="peer-1"
    ) is False


def test_waiting_human_still_blocks_peer_command():
    assert should_wake(
        "command", disposition="waiting_human", from_agent_id="peer-1"
    ) is False


def test_waiting_human_allows_task_transition():
    assert should_wake(
        "task_transition",
        disposition="waiting_human",
        from_agent_id="executor-1",
    ) is True


def test_task_transition_pierces_unmatched_agent_wait():
    """TEST3 root cause: coordinator waiting on agent must still wake on submit."""
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


def test_peer_command_still_blocked_by_unmatched_agent_wait():
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
        is False
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
