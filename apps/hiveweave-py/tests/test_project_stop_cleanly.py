"""Delete project must stop_project_cleanly before evict/rmtree."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from hiveweave.api.projects import delete_project


def test_delete_project_uses_stop_project_cleanly() -> None:
    src = inspect.getsource(delete_project)
    assert "stop_project_cleanly" in src
    assert "stop_project_agents(" not in src


@pytest.mark.asyncio
async def test_delete_project_stops_cleanly_before_evict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hiveweave.api import projects as api

    order: list[str] = []

    async def fake_query_one(sql, params=None):
        return {"workspace_path": str(tmp_path), "id": "proj-del"}

    async def fake_execute(*args, **kwargs):
        sql = str(args[0] if args else "")
        if "is_started" in sql:
            order.append("mark_stopped")
        if "DELETE FROM projects" in sql:
            order.append("delete_meta")
        return None

    async def fake_stop(project_id: str):
        order.append("stop_cleanly")
        return {"offturn_reaped": 1, "stopped": 0}

    async def fake_evict(*args, **kwargs):
        order.append("evict")
        return None

    async def no_sleep(*args, **kwargs):
        return None

    class FakeGT:
        def __init__(self, *args, **kwargs):
            pass

        async def stop(self, *args, **kwargs):
            return None

    async def fake_get_active():
        return None

    async def fake_set_active(_value):
        return None

    monkeypatch.setattr(api.meta_db, "query_one", fake_query_one)
    monkeypatch.setattr(api.meta_db, "execute", fake_execute)
    monkeypatch.setattr(api.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(api, "GameTimeService", FakeGT)
    monkeypatch.setattr(api.project_db, "evict_project_db", fake_evict)
    monkeypatch.setattr(
        api.project_db, "evict_project_db_for_agent", fake_evict
    )
    monkeypatch.setattr(
        "hiveweave.services.project_lifecycle.stop_project_cleanly",
        fake_stop,
    )
    monkeypatch.setattr(api, "_get_active_project_id", fake_get_active)
    monkeypatch.setattr(api, "_set_active_project_id", fake_set_active)

    result = await api.delete_project("proj-del")
    assert result.get("ok") is True
    assert "stop_cleanly" in order
    assert "evict" in order
    assert order.index("stop_cleanly") < order.index("evict")
    assert order.index("stop_cleanly") < order.index("delete_meta")
