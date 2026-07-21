"""Unit tests for Task Ledger — TaskService state machine + DispatchService.

Task 8: 覆盖 services/task.py 的 8 态状态机 + CRUD，以及
services/dispatch.py 的 dispatch_task 返回值 + task_id 全链路串联
(tasks / inbox / work_logs / handoffs)。

测试策略:
  - 用 tempfile 创建真实 per-project DB（DELETE journal mode，与生产一致）
  - patch meta_db.get_project_workspace / get_agent_project_id 路由到临时工作区，
    使 task / dispatch / handoff / inbox 四个服务都落到同一个 per-project DB
  - 每个测试独立，fixture 负责创建连接 + 清理（关闭连接后再删临时目录）
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import dispatch as dispatch_module
from hiveweave.services import handoff as handoff_module
from hiveweave.services import inbox as inbox_module
from hiveweave.services import task as task_module
from hiveweave.services.dispatch import DispatchService
from hiveweave.services.task import TaskService

PROJECT_ID = "test-task-project"
COORDINATOR_ID = "test-coordinator"
EXECUTOR_ID = "test-executor"


@pytest.fixture
async def env():
    """Real per-project DB in a temp workspace with mocked meta_db routing.

    Yields a dict with project_id / workspace_path / coordinator_id / executor_id.
    Cleans up the cached connection before the temp dir is removed (Windows-safe).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        # org_span 硬门（3049834 起）要求真实组织关系：dispatch 只能派直属
        # 下属，且 assignee 须具备 SOURCE_WRITE。夹具插入满足新契约的最小
        # 组织链：coordinator（无上级）→ executor（parent_id=coordinator）。
        _FAKE_AGENTS = {
            COORDINATOR_ID: {
                "id": COORDINATOR_ID,
                "name": "Test Coordinator",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXECUTOR_ID: {
                "id": EXECUTOR_ID,
                "name": "Test Executor",
                "parent_id": COORDINATOR_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        # Reset migration tracking + agent cache so the fresh DB is fully set up.
        # (task._migrated gates the due_at ALTER TABLE; without clearing it the
        #  second test would skip migration on a brand-new DB missing due_at.)
        task_module._migrated.discard(PROJECT_ID)
        dispatch_module._migrated.discard(PROJECT_ID)
        handoff_module._migrated.discard(PROJECT_ID)
        inbox_module._migrated.discard(COORDINATOR_ID)
        inbox_module._migrated.discard(EXECUTOR_ID)
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id), \
             patch("hiveweave.db.meta.get_agent_by_id",
                   fake_get_agent_by_id):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORDINATOR_ID,
                "executor_id": EXECUTOR_ID,
            }

        # Cleanup: close + remove the per-project connection before temp dir
        # deletion (open file handles block Windows directory removal).
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)


# ── Helpers ─────────────────────────────────────────────────


async def _create_claimed(env, svc, assignee=EXECUTOR_ID):
    """Create (no assignee) + claim. Returns task_id."""
    tid = await svc.create_task(
        project_id=env["project_id"], title="T", description="d",
        creator_id=env["coordinator_id"])
    await svc.claim_task(env["project_id"], tid, assignee)
    return tid


async def _create_started(env, svc, assignee=EXECUTOR_ID):
    """Create → claim → start. Returns task_id."""
    tid = await _create_claimed(env, svc, assignee)
    await svc.start_task(env["project_id"], tid)
    return tid


async def _create_submitted(env, svc, assignee=EXECUTOR_ID):
    """Create → claim → start → submit. Returns task_id."""
    tid = await _create_started(env, svc, assignee)
    await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
    return tid


async def _create_reviewing(env, svc, assignee=EXECUTOR_ID):
    """Create → ... → submit → start_review. Returns task_id."""
    tid = await _create_submitted(env, svc, assignee)
    await svc.start_review(env["project_id"], tid)
    return tid


async def _fetch(env, sql, params=None):
    """Run a SELECT on the per-project DB, return list[dict]."""
    conn = await ensure_project_db(env["workspace_path"])
    cursor = await conn.execute(sql, params or [])
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


# ── SubTask 8.1: 状态转换测试 ───────────────────────────────


class TestTaskTransitions:
    """8.1 — each legal state transition sets the right status + fields."""

    async def test_created_to_claimed(self, env):
        svc = TaskService()
        tid = await svc.create_task(
            project_id=env["project_id"], title="T", description="d",
            creator_id=env["coordinator_id"])
        await svc.claim_task(env["project_id"], tid, env["executor_id"])
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "claimed"
        assert t["assignee_id"] == env["executor_id"]
        assert t["claimed_at"] is not None

    async def test_claimed_to_running(self, env):
        svc = TaskService()
        tid = await _create_claimed(env, svc)
        await svc.start_task(env["project_id"], tid)
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "running"

    async def test_running_to_blocked(self, env):
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.block_task(env["project_id"], tid, "等待依赖")
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "blocked"
        assert t["blocked_reason"] == "等待依赖"

    async def test_blocked_to_running(self, env):
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.block_task(env["project_id"], tid, "等待依赖")
        await svc.unblock_task(env["project_id"], tid)
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "running"
        assert t["blocked_reason"] is None

    async def test_running_to_submitted(self, env):
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "submitted"
        assert t["evidence"] == {"files": ["a.py"]}
        assert t["submitted_at"] is not None

    async def test_submitted_to_reviewing(self, env):
        svc = TaskService()
        tid = await _create_submitted(env, svc)
        await svc.start_review(env["project_id"], tid)
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "reviewing"

    async def test_reviewing_to_approved(self, env):
        svc = TaskService()
        tid = await _create_reviewing(env, svc)
        await svc.review_task(env["project_id"], tid, "approve")
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "approved"

    async def test_approved_to_closed(self, env):
        svc = TaskService()
        tid = await _create_reviewing(env, svc)
        await svc.review_task(env["project_id"], tid, "approve")
        await svc.close_task(env["project_id"], tid)
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "closed"
        assert t["closed_at"] is not None


# ── SubTask 8.2: 完整流程测试 ───────────────────────────────


class TestFullWorkflow:
    """8.2 — full happy-path: create → claim → start → submit → review → close."""

    async def test_create_claim_start_submit_review_approve_close(self, env):
        svc = TaskService()
        pid = env["project_id"]

        tid = await svc.create_task(pid, "Feature", "desc",
                                    env["coordinator_id"])
        t = await svc.get_task(pid, tid)
        assert t["status"] == "created"
        assert t["created_at"] is not None

        await svc.claim_task(pid, tid, env["executor_id"])
        assert (await svc.get_task(pid, tid))["status"] == "claimed"

        await svc.start_task(pid, tid)
        assert (await svc.get_task(pid, tid))["status"] == "running"

        await svc.submit_task(pid, tid, {"diff": "abc"})
        t = await svc.get_task(pid, tid)
        assert t["status"] == "submitted"
        assert t["submitted_at"] is not None

        await svc.start_review(pid, tid)
        assert (await svc.get_task(pid, tid))["status"] == "reviewing"

        await svc.review_task(pid, tid, "approve", feedback="good")
        assert (await svc.get_task(pid, tid))["status"] == "approved"

        await svc.close_task(pid, tid)
        t = await svc.get_task(pid, tid)
        assert t["status"] == "closed"
        assert t["closed_at"] is not None


# ── SubTask 8.3: rework 回路测试 ────────────────────────────


class TestReworkLoop:
    """8.3 — rework sends task back to running; can re-submit."""

    async def test_submit_review_rework_running_resubmit(self, env):
        svc = TaskService()
        pid = env["project_id"]

        tid = await _create_submitted(env, svc)
        await svc.start_review(pid, tid)
        await svc.review_task(pid, tid, "rework", feedback="fix bugs")
        t = await svc.get_task(pid, tid)
        assert t["status"] == "running"

        # Re-submit after rework (running → submitted)
        await svc.submit_task(pid, tid, {"diff": "fixed"})
        t = await svc.get_task(pid, tid)
        assert t["status"] == "submitted"
        assert t["submitted_at"] is not None


# ── SubTask 8.4: blocked 流程测试 ───────────────────────────


class TestBlockedFlow:
    """8.4 — block with reason, unblock clears reason."""

    async def test_block_with_reason(self, env):
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.block_task(env["project_id"], tid, "等待依赖")
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "blocked"
        assert t["blocked_reason"] == "等待依赖"

    async def test_unblock_clears_reason(self, env):
        svc = TaskService()
        tid = await _create_started(env, svc)
        await svc.block_task(env["project_id"], tid, "等待依赖")
        await svc.unblock_task(env["project_id"], tid)
        t = await svc.get_task(env["project_id"], tid)
        assert t["status"] == "running"
        assert t["blocked_reason"] is None


# ── SubTask 8.5: dispatch_task 返回值测试 ───────────────────


class TestDispatchReturn:
    """8.5 — dispatch_task returns success=True + non-empty task_id."""

    async def test_dispatch_returns_success_and_task_id(self, env):
        svc = DispatchService()
        result = await svc.dispatch_task(
            project_id=env["project_id"],
            from_agent_id=env["coordinator_id"],
            to_agent_id=env["executor_id"],
            description="实现登录模块",
        )
        assert result["success"] is True
        assert isinstance(result["task_id"], str)
        assert len(result["task_id"]) > 0
        assert result["handoff_id"] is not None
        task = await TaskService().get_task(env["project_id"], result["task_id"])
        assert task["status"] == "claimed"
        assert task["assignee_id"] == env["executor_id"]
        assert task["claimed_at"] is not None


# ── SubTask 8.6: task_id 串联测试 ───────────────────────────


class TestTaskIdPropagation:
    """8.6 — dispatch_task threads task_id through tasks/inbox/work_logs/handoffs."""

    async def test_dispatch_propagates_task_id_to_all_tables(self, env):
        svc = DispatchService()
        result = await svc.dispatch_task(
            project_id=env["project_id"],
            from_agent_id=env["coordinator_id"],
            to_agent_id=env["executor_id"],
            description="实现注册模块",
        )
        task_id = result["task_id"]

        # tasks 表: id == task_id
        tasks = await _fetch(env, "SELECT id FROM tasks WHERE id = ?", [task_id])
        assert len(tasks) == 1

        # inbox 表: task_id 列 = 同一个 task_id
        inbox_rows = await _fetch(
            env, "SELECT task_id FROM inbox WHERE task_id = ?", [task_id])
        assert len(inbox_rows) == 1
        assert inbox_rows[0]["task_id"] == task_id

        # work_logs 表: task_id 列 = 同一个 task_id
        log_rows = await _fetch(
            env, "SELECT task_id FROM work_logs WHERE task_id = ?", [task_id])
        assert len(log_rows) == 1
        assert log_rows[0]["task_id"] == task_id

        # handoffs 表: task_id 列 = 同一个 task_id
        handoff_rows = await _fetch(
            env, "SELECT task_id FROM handoffs WHERE task_id = ?", [task_id])
        assert len(handoff_rows) == 1
        assert handoff_rows[0]["task_id"] == task_id


# ── SubTask 8.7: 非法转换测试 ───────────────────────────────


class TestIllegalTransitions:
    """8.7 — illegal transitions raise ValueError."""

    async def test_created_cannot_submit(self, env):
        # created → submitted 非法（created 只能 → claimed | closed）
        svc = TaskService()
        tid = await svc.create_task(env["project_id"], "T", "d",
                                    env["coordinator_id"])
        with pytest.raises(ValueError, match="Illegal transition"):
            await svc.submit_task(env["project_id"], tid, {"f": "a"})

    async def test_closed_cannot_submit(self, env):
        # closed 是终态，不能 submit
        svc = TaskService()
        tid = await _create_reviewing(env, svc)
        await svc.review_task(env["project_id"], tid, "approve")
        await svc.close_task(env["project_id"], tid)
        with pytest.raises(ValueError, match="Illegal transition"):
            await svc.submit_task(env["project_id"], tid, {"f": "a"})

    async def test_approved_cannot_submit(self, env):
        # approved 只能 → closed，不能 → submitted
        svc = TaskService()
        tid = await _create_reviewing(env, svc)
        await svc.review_task(env["project_id"], tid, "approve")
        with pytest.raises(ValueError, match="Illegal transition"):
            await svc.submit_task(env["project_id"], tid, {"f": "a"})
