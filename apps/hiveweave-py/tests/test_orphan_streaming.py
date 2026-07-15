"""Orphan streaming auto-heal — product must clear zombies without human help."""

from __future__ import annotations

import time

import pytest

from hiveweave.services.chat_message import ChatMessageService


@pytest.mark.asyncio
async def test_clear_orphan_streaming_spares_processing_agents(monkeypatch):
    """Idle agents' streaming rows clear; processing agents keep young streams."""
    svc = ChatMessageService()
    now = int(time.time() * 1000)
    idle_id = "agent-idle"
    busy_id = "agent-busy"

    executed: list[tuple[str, list]] = []

    class FakeCursor:
        rowcount = 2

        async def close(self):
            return None

    class FakeConn:
        async def execute(self, sql, params=None):
            executed.append((sql, list(params or [])))
            return FakeCursor()

        async def commit(self):
            return None

    async def fake_get_db(project_id: str):
        assert project_id == "proj-1"
        return FakeConn()

    monkeypatch.setattr(
        "hiveweave.db.project.get_project_db_by_project_id",
        fake_get_db,
    )

    cleared = await svc.clear_orphan_streaming(
        "proj-1",
        protect_agent_ids={busy_id},
        hard_age_ms=11 * 60 * 1000,
    )
    assert cleared == 2
    assert len(executed) == 1
    sql, params = executed[0]
    assert "is_streaming = 1" in sql
    assert "NOT IN" in sql
    assert busy_id in params
    assert params[-1] < now  # cutoff timestamp


@pytest.mark.asyncio
async def test_clear_orphan_streaming_clears_all_when_none_processing(monkeypatch):
    svc = ChatMessageService()
    executed: list[str] = []

    class FakeCursor:
        rowcount = 5

        async def close(self):
            return None

    class FakeConn:
        async def execute(self, sql, params=None):
            executed.append(sql)
            return FakeCursor()

        async def commit(self):
            return None

    async def fake_get_db(project_id: str):
        return FakeConn()

    monkeypatch.setattr(
        "hiveweave.db.project.get_project_db_by_project_id",
        fake_get_db,
    )

    cleared = await svc.clear_orphan_streaming("proj-1", protect_agent_ids=set())
    assert cleared == 5
    assert "NOT IN" not in executed[0]
    assert "WHERE is_streaming = 1" in executed[0]
