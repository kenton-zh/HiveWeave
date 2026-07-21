"""Assign = claim: assignee set → status claimed (VERIFY stays created)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-assign-claim"
COORD = "coord-1"
EXEC = "exec-1"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_create_with_assignee_starts_claimed(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "Ship UI", "desc", COORD, assignee_id=EXEC
    )
    t = await ts.get_task(pid, tid)
    assert t["status"] == "claimed"
    assert t["assignee_id"] == EXEC
    assert t["claimed_at"] is not None


@pytest.mark.asyncio
async def test_verify_with_assignee_stays_created(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid,
        "VERIFY: Ship UI",
        "verify",
        COORD,
        assignee_id=EXEC,
        tags=["verify", "mandatory"],
    )
    t = await ts.get_task(pid, tid)
    assert t["status"] == "created"
    assert t["assignee_id"] == EXEC
    assert t["claimed_at"] is None


@pytest.mark.asyncio
async def test_promote_heals_legacy_assigned_created(env):
    """Old rows: assignee + created → promote to claimed for obligations."""
    ts = TaskService()
    pid = env["project_id"]
    # Insert legacy shape via create unassigned then raw assignee without claim
    tid = await ts.create_task(pid, "Legacy", "d", COORD, assignee_id=None)
    await ts.update_task(pid, tid, assignee_id=EXEC)
    # update_task already ensure_assignee_claimed — should be claimed
    t = await ts.get_task(pid, tid)
    assert t["status"] == "claimed"

    # Force legacy again for promote path
    conn = await project_db.ensure_project_db(env["workspace_path"])
    await conn.execute(
        "UPDATE tasks SET status = 'created', claimed_at = NULL WHERE id = ?",
        [tid],
    )
    await conn.commit()

    n = await ts.promote_assigned_created(pid, EXEC)
    assert n == 1
    t2 = await ts.get_task(pid, tid)
    assert t2["status"] == "claimed"
    obs = await ts.get_actionable_obligations(pid, EXEC)
    assert tid in [x["id"] for x in obs]


@pytest.mark.asyncio
async def test_claim_idempotent_when_already_assigned(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(pid, "X", "d", COORD, assignee_id=EXEC)
    await ts.claim_task(pid, tid, EXEC)  # no-op
    assert (await ts.get_task(pid, tid))["status"] == "claimed"
