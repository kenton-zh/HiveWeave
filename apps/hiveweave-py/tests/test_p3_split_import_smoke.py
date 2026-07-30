"""Regression: P3 split must not drop module-level imports used by moved code."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_task_event_mark_delivered_binds_time():
    """events.py must import time — NameError shipped green without this."""
    from hiveweave.services.task import TaskEventService

    with patch(
        "hiveweave.services.tasks.events._execute",
        new_callable=AsyncMock,
    ) as execute:
        await TaskEventService().mark_delivered("proj-p3", ["evt-1"])
    execute.assert_awaited_once()
    args = execute.await_args.args
    assert args[0] == "proj-p3"
    assert isinstance(args[2][0], int)  # delivered_at ms from time.time()


@pytest.mark.asyncio
async def test_task_event_get_undelivered_error_binds_log():
    from hiveweave.services.task import TaskEventService

    with patch(
        "hiveweave.services.tasks.events._query",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db down"),
    ):
        rows = await TaskEventService().get_undelivered("proj-p3")
    assert rows == []


def test_http_stream_module_exports_time():
    """http_stream._iter_sse_with_timeout uses time.monotonic()."""
    from hiveweave.llm.streamer import http_stream

    assert hasattr(http_stream, "time")
    assert callable(http_stream.time.monotonic)
    # co_names still references time — ensure global resolves
    names = http_stream.HttpStreamMixin._iter_sse_with_timeout.__code__.co_names
    assert "time" in names
    assert "time" in http_stream.__dict__ or hasattr(http_stream, "time")
