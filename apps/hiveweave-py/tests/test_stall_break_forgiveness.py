"""TEST21 M1/M6 unit checks for forensics v2 first batch."""

from __future__ import annotations

from hiveweave.agents.agent import _turn_has_substantial_progress


def test_turn_progress_readonly_only_is_not_substantial():
    tools = [
        {"name": "get_tasks", "arguments": {}},
        {"name": "read_file", "arguments": {"path": "a.ts"}},
    ]
    assert _turn_has_substantial_progress(tools) is False


def test_turn_progress_write_is_substantial():
    tools = [
        {"name": "get_tasks", "arguments": {}},
        {"name": "write_file", "arguments": {"path": "a.ts", "content": "x"}},
    ]
    assert _turn_has_substantial_progress(tools) is True


def test_turn_progress_tasks_advanced_counts():
    assert _turn_has_substantial_progress([], {"abc"}) is True
