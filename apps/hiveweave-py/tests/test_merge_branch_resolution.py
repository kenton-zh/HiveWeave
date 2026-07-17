"""git_worktree_merge 分支解析 — BUGFIX #3 分支命名空间 bug。

回归场景（井字棋实测）：coordinator 砚白(A003) 传 branchName='feat/tictactoe-a004'
合并 executor 潮汐(A004) 的分支，旧 fallback 静默拼调用者前缀
→ 解析成 hw/A001/feat-tictactoe-a004（错误分支）→ 假冲突。

修复：fallback 先查调用者分支，查不到则全局搜索 hw/*/<slug>，
唯一匹配则合并，零/多匹配报错并列出候选分支。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


async def _make_executor_branch(repo: Path, short_id: str, task: str,
                                filename: str) -> None:
    """创建 executor worktree 并在其分支上提交一个文件。"""
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, task)
    assert res["success"] is True, res
    wt = Path(res["path"])
    (wt / filename).write_text(f"print('from {short_id}')\n", encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")


async def _call_tool(repo: Path, caller: str, branch_name: str):
    params = GitWorktreeMergeParams(branchName=branch_name)
    # 注意：patch 上下文必须覆盖 await 执行期，不能只在创建协程时生效
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(repo), caller, "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ):
        return await git_worktree_merge_tool(params, "agent-x", str(repo))


@pytest.mark.asyncio
async def test_coordinator_merges_executor_branch_by_name(git_repo: Path) -> None:
    """井字棋事故调用形状：coordinator 传 executor 的分支名（无 hw/ 前缀）。

    旧行为解析为 hw/<caller>/feat-tictactoe-a004（错误）；现全局搜索命中
    hw/A004/feat-tictactoe-a004 并成功合并。
    """
    await _make_executor_branch(git_repo, "A004", "feat/tictactoe-a004",
                                "tictactoe.py")

    result = await _call_tool(git_repo, "A003", "feat-tictactoe-a004")

    assert result.success is True, result.error
    # 文件已合入主树
    assert (git_repo / "tictactoe.py").exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "hw/*/*"],
        cwd=git_repo, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout
    assert "feat-tictactoe-a004" not in branches  # 分支已清理


@pytest.mark.asyncio
async def test_caller_own_branch_still_works(git_repo: Path) -> None:
    """向后兼容：调用者合并自己的分支（原行为保留）。"""
    await _make_executor_branch(git_repo, "A003", "后端工程师", "api.py")

    result = await _call_tool(git_repo, "A003", "后端工程师")

    assert result.success is True, result.error
    assert (git_repo / "api.py").exists()


@pytest.mark.asyncio
async def test_unknown_branch_errors_with_candidates(git_repo: Path) -> None:
    """零匹配：报错并列出可用分支（供 LLM 自我纠正）。"""
    await _make_executor_branch(git_repo, "A004", "feat/tictactoe-a004",
                                "tictactoe.py")

    result = await _call_tool(git_repo, "A003", "不存在的分支名")

    assert result.success is False
    assert "Available worktree branches" in (result.error or "")
    assert "hw/A004/feat-tictactoe-a004" in (result.error or "")


@pytest.mark.asyncio
async def test_ambiguous_branch_errors_with_matches(git_repo: Path) -> None:
    """多匹配：报错并列出候选，要求传全名。"""
    await _make_executor_branch(git_repo, "A004", "feat-x", "a.py")
    await _make_executor_branch(git_repo, "A005", "feat-x", "b.py")

    result = await _call_tool(git_repo, "A003", "feat-x")

    assert result.success is False
    assert "Ambiguous" in (result.error or "")
    assert "hw/A004/feat-x" in (result.error or "")
    assert "hw/A005/feat-x" in (result.error or "")
