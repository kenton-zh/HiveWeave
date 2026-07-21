"""admit_wake — any message wakes."""

from __future__ import annotations

from hiveweave.services.wake_policy import admit_wake


def test_admit_always_wakes_including_progress():
    ask = admit_wake(
        "ask", disposition="complete", from_agent_id="peer-1"
    )
    assert ask.ok and ask.reason == "always_wake"

    progress = admit_wake(
        "progress", disposition="complete", from_agent_id="peer-1"
    )
    assert progress.ok and progress.reason == "always_wake"


def test_admit_peer_command_on_complete():
    r = admit_wake(
        "command",
        disposition="complete",
        from_agent_id="peer-1",
        recipient_parent_id="parent-1",
    )
    assert r.ok


def test_question_tool_timeout_outlives_question_wait():
    """streamer must not cancel question before QUESTION_TIMEOUT_S."""
    from hiveweave.llm.streamer import (
        TOOL_EXECUTION_TIMEOUT_S,
        _QUESTION_TOOL_TIMEOUT_S,
    )
    from hiveweave.tools.question import QUESTION_TIMEOUT_S

    assert TOOL_EXECUTION_TIMEOUT_S == 120.0
    assert QUESTION_TIMEOUT_S == 180
    assert _QUESTION_TOOL_TIMEOUT_S > QUESTION_TIMEOUT_S
