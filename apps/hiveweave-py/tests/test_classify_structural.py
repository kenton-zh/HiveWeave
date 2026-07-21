"""Wake policy without category taxonomy — always message, always wake."""

from __future__ import annotations

from hiveweave.services.wake_policy import classify_message, should_wake


def test_classify_always_message():
    assert (
        classify_message(
            message="Please hire 4 people",
            message_type="normal",
            from_agent_id="ceo",
        )
        == "message"
    )
    assert (
        classify_message(
            message="FYI",
            message_type="notify",
            from_agent_id="peer",
        )
        == "message"
    )
    assert (
        classify_message(
            message="whatever",
            message_type="ask",
            expect_report=True,
            from_agent_id="a",
        )
        == "message"
    )


def test_always_wakes():
    assert should_wake("message") is True
    assert should_wake("ask") is True
    assert should_wake(None, disposition="waiting_human") is True
