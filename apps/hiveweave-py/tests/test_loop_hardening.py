"""Regression tests for Multi-Agent loop hardening.

Covers:
- GameTimeService.start idempotency
- Coordinator-only git_worktree_merge/create permission
- submit_task / review_task verification gates
- post-approve VERIFY task spawn
- Agent timeout resume cooldown helpers
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services import task as task_module
from hiveweave.services.game_time import GameTimeService, _states
from hiveweave.services.permission import (
    COORDINATOR_ONLY_TOOLS,
    READONLY_TOOLS,
    READWRITE_TOOLS,
    PermissionService,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.task_tools import (
    SubmitTaskParams,
    _spawn_post_approve_verify_task,
    nudge_verify_tasks_after_merge,
    submit_task_tool,
)


PROJECT_ID = "loop-hardening-project"
COORDINATOR_ID = "loop-coordinator"
EXECUTOR_ID = "loop-executor"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        task_module._migrated.discard(PROJECT_ID)
        inbox_module._migrated.discard(COORDINATOR_ID)
        inbox_module._migrated.discard(EXECUTOR_ID)
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORDINATOR_ID,
                "executor_id": EXECUTOR_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)
        _states.pop(PROJECT_ID, None)


# ── GameTime idempotency ─────────────────────────────────────


class TestGameTimeStartIdempotent:
    async def test_second_start_does_not_stack_ticks(self, env):
        svc = GameTimeService()
        pid = env["project_id"]

        with patch.object(svc, "_load_state", AsyncMock(return_value={
            "current_game_seconds": 0,
            "real_started_at": int(time.time()),
            "alarms": [],
        })):
            await svc.start(pid)
            first_task = _states[pid]["task"]
            assert first_task is not None and not first_task.done()

            await svc.start(pid)
            second_task = _states[pid]["task"]
            assert second_task is first_task

            await svc.stop(pid)


# ── Permission: coordinator owns worktree lifecycle ──────────


class TestWorktreePermissionBoundary:
    def test_merge_tools_are_coordinator_only(self):
        for name in (
            "git_worktree_create",
            "git_worktree_merge",
            "git_worktree_remove",
        ):
            assert name in COORDINATOR_ONLY_TOOLS
            assert name not in READONLY_TOOLS
            assert name not in READWRITE_TOOLS

    def test_executor_keeps_checkpoint_only(self):
        assert "git_worktree_checkpoint" in READWRITE_TOOLS
        assert "git_worktree_checkpoint" not in COORDINATOR_ONLY_TOOLS

    async def test_evaluate_allows_merge_for_coordinator_only(self):
        svc = PermissionService()

        async def fake_get(agent_id: str):
            if agent_id == "c1":
                return {
                    "id": "c1",
                    "permission_type": "coordinator",
                    "permission_mode": "readonly",
                    "denied_tools": "[]",
                    "ask_tools": "[]",
                    "allowed_tools": "[]",
                }
            return {
                "id": "e1",
                "permission_type": "executor",
                "permission_mode": "readwrite",
                "denied_tools": "[]",
                "ask_tools": "[]",
                "allowed_tools": "[]",
            }

        with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_get):
            assert await svc.evaluate("c1", "git_worktree_merge") == "allow"
            assert await svc.evaluate("e1", "git_worktree_merge") == "deny"
            assert await svc.evaluate("e1", "git_worktree_checkpoint") == "allow"


# ── Verification gates ───────────────────────────────────────


class TestVerificationGates:
    async def test_submit_tool_requires_tests_passed(self):
        params = SubmitTaskParams(
            taskId="t1",
            summary="done",
            testsPassed=False,
        )
        with patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ):
            result = await submit_task_tool(params, EXECUTOR_ID, "/tmp")
        assert result.success is False
        assert "testsPassed=true" in (result.error or "")

    async def test_spawn_verify_task_after_approve(self, env):
        ts = TaskService()
        pid = env["project_id"]
        parent_id = await ts.create_task(
            pid,
            title="Implement auth",
            description="build auth module",
            creator_id=env["coordinator_id"],
            assignee_id=env["executor_id"],
        )
        await ts.claim_task(pid, parent_id, env["executor_id"])
        await ts.start_task(pid, parent_id)
        await ts.submit_task(pid, parent_id, {"summary": "done", "tests_passed": True})
        await ts.start_review(pid, parent_id)
        await ts.review_task(pid, parent_id, "approve")

        parent = await ts.get_task(pid, parent_id)
        verify_id = await _spawn_post_approve_verify_task(
            ts, pid, env["coordinator_id"], parent
        )
        assert verify_id
        verify = await ts.get_task(pid, verify_id)
        assert verify["parent_task_id"] == parent_id
        assert "verify" in (verify.get("tags") or [])
        assert verify["assignee_id"] == env["executor_id"]
        assert verify["status"] == "created"

        # idempotent — second spawn returns existing
        again = await _spawn_post_approve_verify_task(
            ts, pid, env["coordinator_id"], parent
        )
        assert again == verify_id

    async def test_nudge_verify_after_merge(self, env):
        ts = TaskService()
        pid = env["project_id"]
        parent_id = await ts.create_task(
            pid,
            title="Feature X",
            description="x",
            creator_id=env["coordinator_id"],
            assignee_id=env["executor_id"],
        )
        parent = await ts.get_task(pid, parent_id)
        verify_id = await _spawn_post_approve_verify_task(
            ts, pid, env["coordinator_id"], parent
        )

        with patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(),
        ) as trig:
            nudged = await nudge_verify_tasks_after_merge(
                pid, env["coordinator_id"]
            )
        assert nudged == 1
        trig.assert_awaited()

        # Inbox should contain POST-MERGE VERIFY for executor
        from hiveweave.services.inbox import InboxService
        ib = InboxService()
        msgs = await ib.get_pending_messages(env["executor_id"])
        assert any("POST-MERGE VERIFY" in (m.get("message") or "") for m in msgs)
        assert any(m.get("task_id") == verify_id for m in msgs)


# ── Resume cooldown helpers ──────────────────────────────────


class TestResumeCooldown:
    def test_arm_and_check_cooldown(self):
        from hiveweave.agents.agent import Agent, TIMEOUT_RESUME_COOLDOWN_S

        agent = Agent.__new__(Agent)
        agent.id = "a1"
        agent.project_id = PROJECT_ID
        agent._resume_cooldown_until = 0.0

        assert agent._in_resume_cooldown() is False
        agent._arm_resume_cooldown(TIMEOUT_RESUME_COOLDOWN_S)
        assert agent._in_resume_cooldown() is True

        agent._resume_cooldown_until = time.monotonic() - 1
        assert agent._in_resume_cooldown() is False
