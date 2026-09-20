"""TEST_DSH_64 #7① 截断显式化：clip_with_pointer + get_tasks 单任务全文视图。

- clip_with_pointer：恰好等于 limit 不截、0 长度、超限格式（kept/total + 指针）。
- GetTasksParams.taskId：单任务视图渲染 review_feedback 等长字段**不截断**，
  兑现 listing 截断指针（`full: get_tasks(taskId="…")`）的承诺。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.query import (
    GetTasksParams,
    clip_with_pointer,
    get_tasks_tool,
)

PROJECT_ID = "test-dsh64-single-task"
COORD_ID = "coord-dsh64"
EXEC_ID = "exec-dsh64"

FEEDBACK_TAIL = "TAIL-anchor-9f8e7d"


# ── clip_with_pointer 单元 ────────────────────────────────────


def test_clip_exact_limit_returns_unchanged():
    s = "a" * 400
    assert clip_with_pointer(s, 400, 'get_tasks(taskId="t")') == s


def test_clip_zero_length_and_none():
    assert clip_with_pointer("", 10, "p") == ""
    assert clip_with_pointer(None, 10, "p") == ""


def test_clip_under_limit_returns_unchanged():
    assert clip_with_pointer("short", 10, "p") == "short"


def test_clip_over_limit_format():
    out = clip_with_pointer("a" * 500, 400, 'get_tasks(taskId="t1")')
    assert out == (
        "a" * 400
        + ' …[truncated 400/500 chars — full: get_tasks(taskId="t1")]'
    )


def test_gate_accepts_task_id_alias():
    """executor 门禁（TOOL_PARAM_SCHEMAS）必须放行 taskId/task_id。

    截断指针承诺 ``get_tasks(taskId="…")`` 可达；门禁拒收则承诺落空
    （test_no_new_unreachable_aliases_ratchet 的正向对照）。
    """
    from hiveweave.tools.executor import validate_tool_args

    for alias in ("taskId", "task_id"):
        normalized, err = validate_tool_args("get_tasks", {alias: "t1"})
        assert err is None, f"门禁拒收 {alias}: {err}"
        assert "t1" in (normalized or {}).values()


# ── get_tasks 单任务视图（DB 集成）────────────────────────────


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
                "id": COORD_ID, "name": "测试协调员", "short_id": "C064",
                "parent_id": None, "permission_type": "coordinator",
                "role": "架构师", "status": "active",
            },
            EXEC_ID: {
                "id": EXEC_ID, "name": "测试执行者", "short_id": "E064",
                "parent_id": COORD_ID, "permission_type": "executor",
                "role": "engineer", "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.clear()
        task_module._migrated.clear()
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


async def _make_task_with_long_feedback(env) -> str:
    svc = TaskService()
    tid = await svc.create_task(
        project_id=env["project_id"], title="T", description="d",
        creator_id=env["coordinator_id"])
    await svc.claim_task(env["project_id"], tid, env["executor_id"])
    await svc.start_task(env["project_id"], tid)
    await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
    await svc.start_review(env["project_id"], tid)
    await svc.review_task(
        env["project_id"], tid, "rework",
        feedback="F" * 600 + FEEDBACK_TAIL,
        prescription_kind="missing-evidence",
    )
    return tid


async def _get_tasks(env, params: GetTasksParams):
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        return await get_tasks_tool(params, env["executor_id"], env["workspace_path"])


@pytest.mark.asyncio
async def test_listing_truncates_review_feedback_with_pointer(env):
    tid = await _make_task_with_long_feedback(env)
    result = await _get_tasks(env, GetTasksParams())
    assert result.success, result.error
    out = result.output or ""
    assert "…[truncated 400/618" in out  # 600 F + 18 字尾锚 = 618 总长
    assert FEEDBACK_TAIL not in out
    # 指针点名本任务
    assert f'get_tasks(taskId="{tid}")' in out
    # 结构化字段仍带全文（listing 截断不吞结构化数据）
    assert result.extra.get("tasks") is not None


@pytest.mark.asyncio
async def test_single_task_view_returns_full_review_feedback(env):
    tid = await _make_task_with_long_feedback(env)
    result = await _get_tasks(env, GetTasksParams(taskId=tid))
    assert result.success, result.error
    out = result.output or ""
    assert FEEDBACK_TAIL in out  # 全文兑现
    assert "…[truncated" not in out
    # 单任务视图只含这一个任务（Tip 行有 "id= string"，故按 "(id=" 计）
    assert out.count("(id=") == 1


@pytest.mark.asyncio
async def test_single_task_view_no_match_reports_missing(env):
    await _make_task_with_long_feedback(env)
    result = await _get_tasks(env, GetTasksParams(taskId="no-such-id"))
    assert result.success
    assert "No task matching taskId='no-such-id'" in (result.output or "")
