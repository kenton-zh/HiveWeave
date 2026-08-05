"""Auto task-completion memory tests (task closed → LLM summary → memory)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.memory import MemoryService
from hiveweave.services.task import TaskService
from hiveweave.services.task_memory import maybe_write_task_completion_memory
from hiveweave.services.work_log import WorkLogService

PROJECT_ID = "test-task-memory"
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


async def _write_log(pid: str, tid: str, summary: str) -> None:
    await WorkLogService().append_log(
        pid, EXEC, "completion", summary, details={"task_id": tid}
    )


def _patch_llm(summary: str | None):
    """Patch resolve_compactor_callback to return a canned LLM callback."""
    async def fake_callback(prompt: str) -> str | None:
        return summary

    async def fake_resolve(agent_id: str, kind: str = "conversation"):
        return fake_callback

    return patch(
        "hiveweave.conversation.compaction.resolve_compactor_callback",
        fake_resolve,
    )


@pytest.mark.asyncio
async def test_writes_task_completion_memory(env):
    pid = env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(pid, "签到排行榜", "desc", COORD, assignee_id=EXEC)
    await _write_log(pid, tid, "用 Redis 做去重")
    await _write_log(pid, tid, "修复窗口竞态")

    summary = "### 完成的工作\n- 完成签到排行榜\n### 关键决策\n- 用 Redis 去重"
    with _patch_llm(summary):
        await maybe_write_task_completion_memory(pid, tid)

    mem = MemoryService()
    got = await mem.get_all_agent_memories(EXEC, pid)
    assert len(got) == 1
    assert got[0]["content"] == summary
    assert got[0]["type"] == "task_completion"
    meta = got[0]["metadata"] or {}
    assert meta.get("task_id") == tid
    assert meta.get("source") == "task_completion"


@pytest.mark.asyncio
async def test_skips_duplicate(env):
    pid = env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(pid, "X", "d", COORD, assignee_id=EXEC)
    await _write_log(pid, tid, "做了点事")

    with _patch_llm("summary-1"):
        await maybe_write_task_completion_memory(pid, tid)
    with _patch_llm("summary-2"):
        await maybe_write_task_completion_memory(pid, tid)

    mem = MemoryService()
    got = await mem.get_all_agent_memories(EXEC, pid)
    # 幂等：即便第二次 LLM 返回不同内容，也不重复写。
    assert len(got) == 1


@pytest.mark.asyncio
async def test_skips_no_assignee(env):
    pid = env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(pid, "X", "d", COORD, assignee_id=None)

    with _patch_llm("summary"):
        await maybe_write_task_completion_memory(pid, tid)

    mem = MemoryService()
    assert await mem.get_all_agent_memories(EXEC, pid) == []


@pytest.mark.asyncio
async def test_skips_empty_summary(env):
    pid = env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(pid, "X", "d", COORD, assignee_id=EXEC)
    # 有工作日志，确保走到 LLM 阶段（而非被无实质内容提前跳过）。
    await _write_log(pid, tid, "做了一些事")

    # LLM 返回 None（无可用模型/调用失败）→ 不写记忆。
    with _patch_llm(None):
        await maybe_write_task_completion_memory(pid, tid)

    mem = MemoryService()
    assert await mem.get_all_agent_memories(EXEC, pid) == []


@pytest.mark.asyncio
async def test_skips_empty_task_no_content(env):
    """P2-6：无工作日志且证据空洞的容器/umbrella 任务不写低价值记忆。"""
    pid = env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(pid, "汇总容器", "d", COORD, assignee_id=EXEC)

    with _patch_llm("summary"):
        await maybe_write_task_completion_memory(pid, tid)

    mem = MemoryService()
    assert await mem.get_all_agent_memories(EXEC, pid) == []