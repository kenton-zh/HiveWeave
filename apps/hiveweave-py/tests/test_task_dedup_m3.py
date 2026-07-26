"""TEST21 M3 — structured + title dedup and force gate."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-dedup-m3"
CREATOR = "creator-1"
ASSIGNEE_A = "assignee-a"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid == CREATOR else None

        task_module._migrated.discard(PROJECT_ID)
        await ensure_project_db(workspace_path)
        ts = TaskService()
        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ), patch(
            "hiveweave.db.meta.get_agent_project_id",
            fake_get_agent_project_id,
        ):
            parent_id = await ts.create_task(
                PROJECT_ID,
                title="Parent",
                description="parent",
                creator_id=CREATOR,
            )
            yield {
                "ts": ts,
                "parent_id": parent_id,
                "workspace": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_find_structured_open_dup_by_modules(env):
    ts: TaskService = env["ts"]
    parent_id = env["parent_id"]
    mods = ["auth", "leaderboard"]
    open_id = await ts.create_task(
        PROJECT_ID,
        title="Slice A",
        description="d",
        creator_id=CREATOR,
        parent_task_id=parent_id,
        expected_modules=mods,
    )
    dup = await ts.find_structured_open_dup(
        PROJECT_ID,
        parent_task_id=parent_id,
        expected_modules=mods,
    )
    assert dup is not None
    assert dup["id"] == open_id


@pytest.mark.asyncio
async def test_find_similar_include_unassigned(env):
    ts: TaskService = env["ts"]
    title = "Build login page for mobile"
    await ts.create_task(
        PROJECT_ID,
        title=title,
        description="d",
        creator_id=CREATOR,
        assignee_id=None,
    )
    dup = await ts.find_similar_open_task(
        PROJECT_ID,
        title,
        assignee_id=ASSIGNEE_A,
        include_unassigned=True,
    )
    assert dup is not None
    assert not dup.get("assignee_id")


@pytest.mark.asyncio
async def test_create_task_tool_structured_dup_blocks_without_force(env):
    from hiveweave.tools.task_tools import CreateTaskParams, create_task_tool

    ts: TaskService = env["ts"]
    parent_id = env["parent_id"]
    mods = ["ui", "canvas"]
    await ts.create_task(
        PROJECT_ID,
        title="Existing",
        description="d",
        creator_id=CREATOR,
        parent_task_id=parent_id,
        expected_modules=mods,
    )

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value=PROJECT_ID),
    ):
        res = await create_task_tool(
            CreateTaskParams(
                title="New slice",
                description="d",
                parentTaskId=parent_id,
                expectedModules=mods,
                force=False,
            ),
            CREATOR,
            env["workspace"],
        )
    assert not res.success
    assert "结构化重复" in (res.error or res.output or "")


@pytest.mark.asyncio
async def test_create_task_tool_force_allows_unassigned_dup(env):
    from hiveweave.tools.task_tools import CreateTaskParams, create_task_tool

    ts: TaskService = env["ts"]
    title = "Implement scoreboard widget v2"
    await ts.create_task(
        PROJECT_ID,
        title=title,
        description="d",
        creator_id=CREATOR,
        assignee_id=None,
    )

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value=PROJECT_ID),
    ):
        res = await create_task_tool(
            CreateTaskParams(
                title=title,
                description="d2",
                force=True,
            ),
            CREATOR,
            env["workspace"],
        )
    assert res.success
    assert "force=true" in (res.output or "")
