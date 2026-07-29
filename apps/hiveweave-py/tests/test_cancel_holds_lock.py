"""T1#3: cancel() must hold self._lock (serializes with chat setup)."""

from __future__ import annotations

import asyncio
import inspect

from hiveweave.agents.agent import Agent


def test_cancel_source_holds_agent_lock():
    """Regression: cancel body must enter async with self._lock."""
    src = inspect.getsource(Agent.cancel)
    assert "async with self._lock:" in src
    # await of cancelled tasks must be outside the lock section
    lock_idx = src.index("async with self._lock:")
    # The for-await loop should appear after the indented lock block ends;
    # at minimum the source documents lock-then-await pattern.
    assert "task_to_await" in src
    assert "for t in (task_to_await, watcher_to_await):" in src
    await_loop_idx = src.index("for t in (task_to_await, watcher_to_await):")
    assert await_loop_idx > lock_idx
