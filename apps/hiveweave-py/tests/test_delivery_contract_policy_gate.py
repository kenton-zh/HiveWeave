"""T2.4：delivery_contract 的 R1 一致性检查与 verify_ids 口径对齐。

场景（TEST_DSH_35 实测）：任务 policy 只认 browse_e2e（如 ui_browser_e2e），
工作区存在历史成功 test_run 时，契约声明写 ``N/A—<原因>`` 应被接受 ——
被 policy 排除的 kind 不得参与「声明与凭证库矛盾」判定。
此前 ``has_successful_test_run`` 的独立 SQL 完全不看 policy_id，
与 attestation 门对同一凭证给出相反评价。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services.delivery_contract import has_successful_test_run

PROJECT_ID = "test-t24-project"
EXEC_ID = "test-executor"


@pytest.fixture
async def env():
    with __import__("tempfile").TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        att_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            # 建 tool_attestations 表（ensure_schema 走 execute_by_project，
            # 依赖 meta 路由 → 已被 patch 指到 tmp workspace）
            await att_module.attestation_service.ensure_schema(PROJECT_ID)
            from hiveweave.db.project import ensure_project_db

            conn = await ensure_project_db(workspace_path)
            yield {"workspace_path": workspace_path, "conn": conn}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _insert_test_run(conn, task_id: str, *, exit_code: int = 0) -> None:
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tool_attestations (id, task_id, agent_id, kind, "
        "command_or_url, exit_code, created_at, expires_at, project_id) "
        "VALUES (?, ?, ?, 'test_run', 'pytest', ?, ?, ?, ?)",
        [f"att-{task_id}-{exit_code}-{now}", task_id, EXEC_ID,
         exit_code, now, now + 3_600_000, PROJECT_ID],
    )
    await conn.commit()


def _task(policy_id: str, task_id: str = "t-1") -> dict:
    return {
        "id": task_id,
        "title": "实现",
        "tags": [],
        "policy_id": policy_id,
        "evidence": {},
    }


@pytest.mark.asyncio
async def test_policy_excluding_test_run_does_not_contradict_na(env):
    """policy 只认 browse_e2e → 有成功 test_run 也不打回 N/A 声明。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-1", exit_code=0)
    task = _task("ui_browser_e2e")
    assert await has_successful_test_run(PROJECT_ID, "t-1", task=task) is False


@pytest.mark.asyncio
async def test_policy_requiring_test_run_still_contradicts_na(env):
    """policy 要求 test_run（generic_tests）→ 矛盾判定保持生效。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-2", exit_code=0)
    task = _task("generic_tests", "t-2")
    assert await has_successful_test_run(PROJECT_ID, "t-2", task=task) is True


@pytest.mark.asyncio
async def test_soft_policy_keeps_legacy_contradiction_check(env):
    """policy soft（coordinator_review → None）→ 保持旧行为（看凭证库）。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-3", exit_code=0)
    task = _task("coordinator_review", "t-3")
    assert await has_successful_test_run(PROJECT_ID, "t-3", task=task) is True


@pytest.mark.asyncio
async def test_failed_test_run_never_contradicts(env):
    """exit_code≠0 的 test_run 不构成矛盾（只有成功凭证才算）。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-4", exit_code=1)
    task = _task("generic_tests", "t-4")
    assert await has_successful_test_run(PROJECT_ID, "t-4", task=task) is False


@pytest.mark.asyncio
async def test_unknown_policy_falls_back_to_legacy(env):
    """未知 policy（fail-closed 成 _unknown_policy）→ 看不到 test_run 要求
    → 不构成矛盾（与 required_attestation_kinds 的 fail-closed 方向一致）。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-5", exit_code=0)
    task = _task("no_such_policy", "t-5")
    assert await has_successful_test_run(PROJECT_ID, "t-5", task=task) is False


@pytest.mark.asyncio
async def test_task_lookup_fallback_when_not_provided(env, monkeypatch):
    """调用方没透传 task 时兜底自查：拿得到任务行仍走 policy 过滤。"""
    conn = env["conn"]
    await _insert_test_run(conn, "t-6", exit_code=0)

    class _FakeTaskService:
        async def get_task(self, project_id: str, task_id: str):
            return _task("ui_browser_e2e", "t-6")

    # 函数内是 `from hiveweave.services.task import TaskService`（源模块引用）
    monkeypatch.setattr(
        "hiveweave.services.task.TaskService", _FakeTaskService
    )
    assert await has_successful_test_run(PROJECT_ID, "t-6") is False
