"""P0-1 回归测试 — get_tasks 返回 waived_by + waiver_expires_at。

TEST18 死锁根因：waiver 状态对 agent 不可查。A007 误以为自己是 waived_by
（实际只有 A005 是），基于错误假设拒绝 approve；A005 也误判，互相推诿。

修复后 get_tasks 在每个有 active waiver 的 task 行下追加：
    waiver: waived_by=<name>(<short_id>) expires_at=<ts>
           (waived_by CANNOT approve; rework clears waiver)

覆盖：
- 正向：task 有 active waiver → get_tasks 输出含 waived_by + expires_at
- 正向：waived_by 映射为 name+short_id（不是裸 UUID）
- 负向：task 无 waiver → get_tasks 输出不含 "waiver:" 行
- 边界：过期 waiver 不显示（仅 active）
- 集成：rework 后（invalidate_valid_waivers）waiver 行消失
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import (
    attestation_service,
    create_waiver,
    has_valid_waiver,
    invalidate_valid_waivers,
)
from hiveweave.services.task import TaskService

PROJECT_ID = "test-p01-waiver-visibility"
COORD_ID = "coord-p01"
EXEC_ID = "exec-p01"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORD_ID, EXEC_ID) else None

        _FAKE_AGENTS = {
            COORD_ID: {
                "id": COORD_ID,
                "name": "测试协调员",
                "short_id": "C001",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXEC_ID: {
                "id": EXEC_ID,
                "name": "测试执行者",
                "short_id": "E001",
                "parent_id": COORD_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.discard(PROJECT_ID)
        task_module._migrated.discard(PROJECT_ID)
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id", fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORD_ID,
                "executor_id": EXEC_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)


async def _create_claimed_task(env, svc):
    tid = await svc.create_task(
        project_id=env["project_id"], title="T", description="d",
        creator_id=env["coordinator_id"])
    await svc.claim_task(env["project_id"], tid, env["executor_id"])
    return tid


@pytest.mark.asyncio
async def test_get_tasks_shows_active_waiver_with_waived_by_name(env):
    """有 active waiver 时，get_tasks 输出含 waived_by name+short_id + expires_at。"""
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    tid = await _create_claimed_task(env, svc)

    await create_waiver(
        env["project_id"], task_id=tid, waived_by=env["coordinator_id"],
        reason="test waiver for visibility",
    )
    assert await has_valid_waiver(env["project_id"], tid) is True

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(), env["executor_id"], env["workspace_path"]
        )

    assert result.success, result.output or result.error
    out = result.output or ""
    # waiver 行存在
    assert "waiver:" in out, f"waiver line missing in output:\n{out}"
    # waived_by 映射为 name + short_id（不是裸 UUID）
    assert "测试协调员" in out, f"waived_by name missing:\n{out}"
    assert "C001" in out, f"waived_by short_id missing:\n{out}"
    # expires_at 存在
    assert "expires_at=" in out, f"expires_at missing:\n{out}"
    # 提示语存在
    assert "waived_by CANNOT approve" in out
    assert "rework clears waiver" in out


@pytest.mark.asyncio
async def test_get_tasks_no_waiver_line_when_task_has_no_waiver(env):
    """无 waiver 的 task 不应出现 waiver: 行。"""
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    tid = await _create_claimed_task(env, svc)
    # 不创建 waiver

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(), env["executor_id"], env["workspace_path"]
        )

    assert result.success
    out = result.output or ""
    assert "waiver:" not in out, (
        f"no waiver should be shown for task without waiver:\n{out}"
    )


@pytest.mark.asyncio
async def test_get_tasks_hides_expired_waiver(env):
    """过期 waiver 不显示（仅 active）。"""
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    tid = await _create_claimed_task(env, svc)

    # 创建已过期的 waiver
    await create_waiver(
        env["project_id"], task_id=tid, waived_by=env["coordinator_id"],
        reason="expired waiver", ttl_ms=-1,
    )
    assert await has_valid_waiver(env["project_id"], tid) is False

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(), env["executor_id"], env["workspace_path"]
        )

    assert result.success
    out = result.output or ""
    assert "waiver:" not in out, (
        f"expired waiver should not be shown:\n{out}"
    )


@pytest.mark.asyncio
async def test_get_tasks_waiver_disappears_after_rework_invalidate(env):
    """rework 调用 invalidate_valid_waivers 后，get_tasks 不再显示 waiver。"""
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    svc = TaskService()
    tid = await _create_claimed_task(env, svc)
    await svc.start_task(env["project_id"], tid)
    await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
    await svc.start_review(env["project_id"], tid)

    await create_waiver(
        env["project_id"], task_id=tid, waived_by=env["coordinator_id"],
        reason="waiver before rework",
    )
    assert await has_valid_waiver(env["project_id"], tid) is True

    # rework → invalidate_valid_waivers
    await svc.review_task(env["project_id"], tid, "rework", feedback="fix")
    assert await has_valid_waiver(env["project_id"], tid) is False

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        result = await get_tasks_tool(
            GetTasksParams(), env["executor_id"], env["workspace_path"]
        )

    assert result.success
    out = result.output or ""
    assert "waiver:" not in out, (
        f"waiver should disappear after rework invalidate:\n{out}"
    )
