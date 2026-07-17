"""Task archive/unclaim — BUGFIX #5 误绑任务无法纠正。

回归场景（井字棋实测）：验收任务误绑潮汐(A004)，正确接收者是棱镜(A005)。
旧系统：claimed 状态只能 →running/created，无废弃路径 → 误绑任务永远卡死。
修复：archive_task（废弃，留审计）+ unclaim_task（释放回 created 重分配）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-archive-project"
COORD_ID = "test-coordinator"
EXEC_A = "test-executor-a"
EXEC_B = "test-executor-b"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORD_ID, EXEC_A, EXEC_B) else None

        task_module._migrated.discard(PROJECT_ID)
        for aid in (COORD_ID, EXEC_A, EXEC_B):
            project_db._agent_cache.pop(aid, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        for aid in (COORD_ID, EXEC_A, EXEC_B):
            project_db._agent_cache.pop(aid, None)


async def _claimed(svc: TaskService, assignee: str = EXEC_A) -> str:
    tid = await svc.create_task(
        project_id=PROJECT_ID, title="验收任务", description="d",
        creator_id=COORD_ID,
    )
    await svc.claim_task(PROJECT_ID, tid, assignee)
    return tid


@pytest.mark.asyncio
async def test_archive_misassigned_claimed_task(env):
    """误绑的 claimed 任务可废弃：留审计字段，从列表和 obligations 消失。"""
    svc = TaskService()
    tid = await _claimed(svc, EXEC_A)  # 误绑给 A

    from_status = await svc.archive_task(
        PROJECT_ID, tid, archived_by=COORD_ID, reason="误绑，已新建正确任务"
    )
    assert from_status == "claimed"

    # 审计字段
    conn = await project_db.ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT is_archived, archived_by, archived_reason, archived_at "
        "FROM tasks WHERE id = ?", [tid],
    )
    row = (await cur.fetchall())[0]
    assert row[0] == 1
    assert row[1] == COORD_ID
    assert "误绑" in row[2]
    assert row[3] is not None
    await cur.close()

    # 列表与 obligations 均不再出现
    tasks = await svc.list_tasks(PROJECT_ID)
    assert all(t["id"] != tid for t in tasks)
    obligations = await svc.get_actionable_obligations(PROJECT_ID, EXEC_A)
    assert all(t["id"] != tid for t in obligations)


@pytest.mark.asyncio
async def test_archive_requires_reason_and_existing_task(env):
    svc = TaskService()
    tid = await _claimed(svc)

    with pytest.raises(ValueError, match="reason"):
        await svc.archive_task(PROJECT_ID, tid, archived_by=COORD_ID, reason=" ")
    with pytest.raises(ValueError, match="not found"):
        await svc.archive_task(
            PROJECT_ID, "no-such-task", archived_by=COORD_ID, reason="x"
        )
    # 重复废弃
    await svc.archive_task(PROJECT_ID, tid, archived_by=COORD_ID, reason="r")
    with pytest.raises(ValueError, match="already archived"):
        await svc.archive_task(PROJECT_ID, tid, archived_by=COORD_ID, reason="r")


@pytest.mark.asyncio
async def test_unclaim_releases_for_reassignment(env):
    """误绑纠正全流程：claimed → unclaim → created（清空 assignee）→ 重新认领。"""
    svc = TaskService()
    tid = await _claimed(svc, EXEC_A)  # 误绑 A

    await svc.unclaim_task(PROJECT_ID, tid)

    conn = await project_db.ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT status, assignee_id FROM tasks WHERE id = ?", [tid]
    )
    status, assignee = (await cur.fetchall())[0]
    await cur.close()
    assert status == "created"
    assert assignee is None

    # 正确接收者可以认领
    await svc.claim_task(PROJECT_ID, tid, EXEC_B)
    tasks = await svc.get_tasks_for_agent(PROJECT_ID, EXEC_B)
    assert [t["id"] for t in tasks] == [tid]


@pytest.mark.asyncio
async def test_unclaim_rejects_running_task(env):
    """running 中的任务不能直接 unclaim（须先 block/submit）。"""
    svc = TaskService()
    tid = await _claimed(svc)
    await svc.start_task(PROJECT_ID, tid)
    with pytest.raises(ValueError, match="Illegal transition"):
        await svc.unclaim_task(PROJECT_ID, tid)
