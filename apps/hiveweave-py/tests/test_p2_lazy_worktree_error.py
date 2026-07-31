"""P2 回归测试 — 懒创建 worktree 失败时写 worktree_error。

TEST18 发现 A011 (executor/QA) 的 workspace_path=None 且 worktree_error=None，
违反 CLAUDE.md「软失败（success=false）必须写 worktree_error」契约。

根因：`Agent._get_workspace_path` 懒创建路径只在 success 时写回 path，
失败时静默回退 project_ws，不持久化错误。这导致 ws=None+err=None 不一致
无法诊断（reconcile/debug 看不到原因）。

修复后：
- ensure_executor_worktree 返回 {success: False, skipped: True} → 不写 error
  （VERIFY-only / idle 是预期态，不应留错误痕迹）
- ensure_executor_worktree 返回 {success: False, message: ...}（非 skipped）
  → update_agent worktree_error=msg

覆盖：
- 正向：非 skipped 失败 → worktree_error 被写入
- 边界：skipped 失败 → worktree_error 保持 None
- 边界：success → worktree_error 不被设置（path 正常返回）
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _executor_agent_row(short_id="A011", ws_path=None):
    return {
        "id": "agent-a011",
        "short_id": short_id,
        "name": "星火",
        "role": "game test engineer",
        "permission_type": "executor",
        "status": "active",
        "workspace_path": ws_path,
        "worktree_error": None,
    }


def _make_agent():
    """构造最小可用 Agent 实例（不启动 task）。"""
    from hiveweave.agents.agent import Agent

    return Agent(
        agent_id="agent-a011",
        project_id="p-test18",
        config={},
    )


@pytest.fixture
def ws_env():
    """临时项目工作区（含 .git 目录模拟 git repo）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir).resolve()
        (ws / ".git").mkdir(exist_ok=True)  # 模拟 git repo
        yield str(ws)


@pytest.mark.asyncio
async def test_lazy_worktree_failure_persists_worktree_error(ws_env):
    """非 skipped 失败 → worktree_error 被写入 DB。"""
    agent = _make_agent()
    agent_row = _executor_agent_row(ws_path=None)

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=ws_env),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(
                return_value={
                    "success": False,
                    "message": "git worktree add failed: disk full",
                }
            ),
        ) as ensure_mock,
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=True),
        ),
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=agent_row)
        update_call = AsyncMock()
        org.update_agent = update_call

        result_path = await agent._get_workspace_path()

    # 失败 → 回退 project_ws
    assert result_path == ws_env
    # ensure_executor_worktree 被调用
    ensure_mock.assert_awaited_once()
    # P2 核心：worktree_error 被写入
    update_call.assert_awaited()
    # update_agent(self.id, {"worktree_error": err_msg}) — 位置参数
    call_args = update_call.await_args.args
    assert len(call_args) >= 2
    update_payload = call_args[1]
    assert update_payload.get("worktree_error") == "git worktree add failed: disk full"


@pytest.mark.asyncio
async def test_lazy_worktree_skipped_does_not_set_worktree_error(ws_env):
    """skipped 失败（无在途写任务）→ worktree_error 保持 None。

    VERIFY-only / idle 是预期态，不应留错误痕迹。
    """
    agent = _make_agent()
    agent_row = _executor_agent_row(ws_path=None)

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=ws_env),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(
                return_value={
                    "success": False,
                    "skipped": True,
                    "message": "no in-flight write tasks",
                }
            ),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=True),
        ),
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=agent_row)
        update_call = AsyncMock()
        org.update_agent = update_call

        result_path = await agent._get_workspace_path()

    # skipped → 回退 project_ws
    assert result_path == ws_env
    # P2 核心：skipped 时 update_agent 不应被调用写 worktree_error
    # update_agent(self.id, {...}) — 检查所有调用的位置参数 payload
    for call in update_call.await_args_list:
        args = call.args
        if len(args) >= 2 and isinstance(args[1], dict):
            payload = args[1]
            assert "worktree_error" not in payload or payload.get("worktree_error") is None, (
                f"skipped should NOT set worktree_error, got payload={payload}"
            )


@pytest.mark.asyncio
async def test_lazy_worktree_success_does_not_set_worktree_error(ws_env):
    """成功创建 worktree → worktree_error 不被设置，返回新 path。"""
    agent = _make_agent()
    agent_row = _executor_agent_row(ws_path=None)
    new_worktree = str(Path(ws_env) / ".hiveweave" / "worktrees" / "A011")

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=ws_env),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(
                return_value={
                    "success": True,
                    "path": new_worktree,
                }
            ),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=True),
        ),
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=agent_row)
        update_call = AsyncMock()
        org.update_agent = update_call

        result_path = await agent._get_workspace_path()

    # 成功 → 返回新 worktree path
    assert result_path == new_worktree
    # 成功时不应写 worktree_error
    for call in update_call.await_args_list:
        args = call.args
        if len(args) >= 2 and isinstance(args[1], dict):
            payload = args[1]
            assert "worktree_error" not in payload or payload.get("worktree_error") is None, (
                f"success should NOT set worktree_error, got payload={payload}"
            )
