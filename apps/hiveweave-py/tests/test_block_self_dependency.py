"""Reject block_task depends_on that includes the task's own id.

TEST_DSH_06 P3 ``bcf2ec48`` stored ``depends_on=[bcf2ec48]`` — a self-dep
never unblocks and can occupy the VERIFY serial lock.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.lifecycle import SELF_DEPENDENCY_BLOCK_ERROR

PROJECT_ID = "test-block-self-dep"
SELF_ID = "bcf2ec48-aaaa-4bbb-8ccc-ddddeeee0001"
OTHER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0002"


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


async def _insert_task(ws: str, task_id: str, *, status: str = "running"):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", "creator", "assignee", status, now, now],
    )
    await conn.commit()


def _depends_on_ids(task: dict) -> list[str]:
    raw = task.get("depends_on") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            raw = []
    return list(raw) if isinstance(raw, list) else []


@pytest.mark.asyncio
async def test_block_rejects_self_id_and_stays_running(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, SELF_ID, status="running")
    ts = TaskService()

    with pytest.raises(ValueError) as ei:
        await ts.block_task(
            pid,
            SELF_ID,
            "waiting on myself",
            depends_on_task_ids=[SELF_ID],
            wait_kind="dependency",
        )

    assert str(ei.value) == SELF_DEPENDENCY_BLOCK_ERROR
    task = await ts.get_task(pid, SELF_ID)
    assert task is not None
    assert task["status"] == "running"


@pytest.mark.asyncio
async def test_block_other_task_id_succeeds(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, SELF_ID, status="running")
    await _insert_task(ws, OTHER_ID, status="running")
    ts = TaskService()

    await ts.block_task(
        pid,
        SELF_ID,
        "waiting on sibling",
        depends_on_task_ids=[OTHER_ID],
        wait_kind="dependency",
    )

    task = await ts.get_task(pid, SELF_ID)
    assert task is not None
    assert task["status"] == "blocked"
    assert OTHER_ID in _depends_on_ids(task)
    assert SELF_ID not in _depends_on_ids(task)


@pytest.mark.asyncio
async def test_apply_depends_on_rejects_self_id_before_persist(env):
    """dispatch_task dependsOn used apply_depends_on, bypassing block_task."""
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, SELF_ID, status="claimed")
    ts = TaskService()

    with pytest.raises(ValueError) as ei:
        await ts.apply_depends_on(pid, SELF_ID, [SELF_ID])

    assert str(ei.value) == SELF_DEPENDENCY_BLOCK_ERROR
    task = await ts.get_task(pid, SELF_ID)
    assert task is not None
    assert task["status"] == "claimed"
    assert SELF_ID not in _depends_on_ids(task)


@pytest.mark.asyncio
async def test_apply_depends_on_rejects_self_prefix(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, SELF_ID, status="created")
    ts = TaskService()

    with pytest.raises(ValueError) as ei:
        await ts.apply_depends_on(pid, SELF_ID, [SELF_ID[:8]])

    assert str(ei.value) == SELF_DEPENDENCY_BLOCK_ERROR
    task = await ts.get_task(pid, SELF_ID)
    assert task is not None
    assert task["status"] == "created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dep_ref",
    [
        SELF_ID.replace("-", ""),
        SELF_ID[:8],
        SELF_ID.upper(),
        SELF_ID.replace("-", "").upper(),
    ],
)
async def test_block_rejects_self_id_variants(env, dep_ref):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, SELF_ID, status="running")
    ts = TaskService()

    with pytest.raises(ValueError) as ei:
        await ts.block_task(
            pid,
            SELF_ID,
            "waiting on myself",
            depends_on_task_ids=[dep_ref],
            wait_kind="dependency",
        )

    assert str(ei.value) == SELF_DEPENDENCY_BLOCK_ERROR
    task = await ts.get_task(pid, SELF_ID)
    assert task is not None
    assert task["status"] == "running"
