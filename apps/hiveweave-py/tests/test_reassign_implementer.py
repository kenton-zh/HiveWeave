"""TEST21 M2/M5: reassign audit + implementer lock."""

from __future__ import annotations

import json
import uuid

import pytest

from hiveweave.services.task import TaskService, _query


@pytest.mark.asyncio
async def test_reassign_writes_event_and_keeps_implementer(tmp_path, monkeypatch):
    """Running reassign must emit task.reassigned and preserve implementer."""
    from hiveweave.db import meta as meta_db
    from hiveweave.db.project import ensure_project_db

    ws = tmp_path / "proj"
    ws.mkdir()
    project_id = str(uuid.uuid4())
    creator = str(uuid.uuid4())
    implementer = str(uuid.uuid4())
    new_owner = str(uuid.uuid4())

    async def _fake_workspace(_pid: str):
        return str(ws)

    monkeypatch.setattr(meta_db, "get_project_workspace", _fake_workspace)
    await ensure_project_db(str(ws))

    ts = TaskService()
    tid = await ts.create_task(
        project_id,
        title="Sub-task",
        description="code",
        creator_id=creator,
        assignee_id=implementer,
    )
    await ts.start_task(project_id, tid)

    # Pin a fake worktree path (may be empty if agent lookup fails — ok)
    rows = await _query(
        project_id,
        "SELECT implementer_id, status FROM tasks WHERE id = ?",
        [tid],
    )
    assert rows[0]["status"] == "running"
    # Force implementer in case worktree lookup failed to lock
    if not rows[0]["implementer_id"]:
        await ts.lock_implementer_if_needed(project_id, tid, implementer)
    rows = await _query(
        project_id,
        "SELECT implementer_id FROM tasks WHERE id = ?",
        [tid],
    )
    assert rows[0]["implementer_id"] == implementer

    info = await ts.reassign_task(
        project_id,
        tid,
        new_assignee_id=new_owner,
        reassigned_by=creator,
        reason="proxy after timeout",
    )
    assert info["to_assignee"] == new_owner
    assert info.get("implementer_id") == implementer

    after = await ts.get_task(project_id, tid)
    assert after["assignee_id"] == new_owner
    assert after["implementer_id"] == implementer

    ev = await _query(
        project_id,
        "SELECT event_type, payload, actor_id FROM task_events "
        "WHERE task_id = ? AND event_type = 'task.reassigned'",
        [tid],
    )
    assert len(ev) == 1
    payload = json.loads(ev[0]["payload"])
    assert payload["from_assignee"] == implementer
    assert payload["to_assignee"] == new_owner
    assert payload["implementer_id"] == implementer
    assert ev[0]["actor_id"] == creator


@pytest.mark.asyncio
async def test_owner_parked_roundtrip(tmp_path, monkeypatch):
    from hiveweave.db import meta as meta_db
    from hiveweave.db.project import ensure_project_db

    ws = tmp_path / "proj2"
    ws.mkdir()
    project_id = str(uuid.uuid4())
    agent = str(uuid.uuid4())

    async def _fake_workspace(_pid: str):
        return str(ws)

    monkeypatch.setattr(meta_db, "get_project_workspace", _fake_workspace)
    await ensure_project_db(str(ws))

    ts = TaskService()
    tid = await ts.create_task(
        project_id,
        title="Park me",
        description="x",
        creator_id=agent,
        assignee_id=agent,
    )
    await ts.set_owner_parked(project_id, [tid], parked=True)
    row = await ts.get_task(project_id, tid)
    assert row.get("owner_parked") in (1, True)
    await ts.clear_owner_parked_for_agent(project_id, agent)
    row2 = await ts.get_task(project_id, tid)
    assert not row2.get("owner_parked")
