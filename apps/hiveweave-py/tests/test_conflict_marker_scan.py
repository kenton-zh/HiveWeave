"""merge 后 main 残留冲突标记 — 扫描 + 清理任务路由。

事故背景: git_worktree_merge 显示成功, 但 main 上遗留未解决的
<<<<<<< / >>>>>>> 标记, 无人认领清理。修复: merge 成功后扫描目标树,
命中则自动给被合并 worktree 的 owner 创建「清理合并残留冲突标记」任务;
建账失败只降级为警告, 不回滚 merge。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import (
    GitWorktreeService,
    scan_conflict_markers,
)
from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)

MARKED = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


# ── scan_conflict_markers 纯函数测试 ─────────────────────────


def test_scan_detects_line_anchored_markers(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(MARKED, encoding="utf-8")
    (tmp_path / "clean.py").write_text("print('ok')\n", encoding="utf-8")

    hits = scan_conflict_markers(str(tmp_path))

    assert hits == ["src/app.py"]  # POSIX 风格相对路径


def test_scan_clean_tree_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('ok')\n", encoding="utf-8")

    assert scan_conflict_markers(str(tmp_path)) == []


def test_scan_ignores_non_anchored_angle_runs(tmp_path: Path) -> None:
    """缩进/行内的 < 串不是冲突标记 (行首锚定); 6 个 < 也不算。"""
    (tmp_path / "a.py").write_text(
        "x = '<<<<<<<'\n  >>>>>>>\nless <<<<<<< more\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text("<<<<<< six only\n", encoding="utf-8")

    assert scan_conflict_markers(str(tmp_path)) == []


def test_scan_skips_system_dirs_binary_and_large_files(tmp_path: Path) -> None:
    for d in (".git", "node_modules", ".hiveweave", "dist", "build"):
        sub = tmp_path / d / "nested"
        sub.mkdir(parents=True)
        (sub / "marked.txt").write_text(MARKED, encoding="utf-8")
    # 二进制 (含 NUL 字节) 与超大文件跳过
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01" + MARKED.encode() * 4)
    (tmp_path / "big.txt").write_text(MARKED + "x" * 1_100_000,
                                      encoding="utf-8")

    assert scan_conflict_markers(str(tmp_path)) == []


def test_scan_missing_root_returns_empty(tmp_path: Path) -> None:
    assert scan_conflict_markers(str(tmp_path / "nope")) == []


# ── merge 结果 conflict_markers 字段 + tool 警告路由 ─────────


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


async def _make_branch_with_file(repo: Path, short_id: str, task: str,
                                 filename: str, content: str) -> None:
    """创建 executor worktree 并在其分支上提交一个文件。"""
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, task)
    assert res["success"] is True, res
    wt = Path(res["path"])
    (wt / filename).write_text(content, encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")


async def _make_marked_branch(repo: Path, short_id: str, task: str,
                              filename: str) -> None:
    """executor 把含冲突标记的文件提交进自己分支 (残留标记来源)。"""
    await _make_branch_with_file(repo, short_id, task, filename, MARKED)


async def _call_merge_tool(repo: Path, caller: str, branch_name: str,
                           create_task_mock: AsyncMock):
    params = GitWorktreeMergeParams(branchName=branch_name)
    # 注意: patch 上下文必须覆盖 await 执行期, 不能只在创建协程时生效
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(repo), caller, "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch(
        "hiveweave.tools.task_tools.resolve_agent_id_by_short_id",
        new=AsyncMock(return_value="agent-owner-1"),
    ), patch(
        "hiveweave.services.task.TaskService.create_task", create_task_mock
    ):
        return await git_worktree_merge_tool(params, "agent-x", str(repo))


async def test_service_merge_reports_conflict_markers(git_repo: Path) -> None:
    """service 层: merge 成功结果携带 conflict_markers (相对路径)。"""
    await _make_marked_branch(git_repo, "A004", "feat-x", "marked.py")

    gwt = GitWorktreeService()
    res = await gwt.merge_by_branch(str(git_repo), "hw/A004/feat-x", "main")

    assert res["success"] is True, res
    assert res["conflict_markers"] == ["marked.py"]


async def test_service_clean_merge_has_no_conflict_markers(
    git_repo: Path,
) -> None:
    await _make_branch_with_file(git_repo, "A004", "feat-clean", "clean.py",
                                 "print('ok')\n")

    gwt = GitWorktreeService()
    res = await gwt.merge_by_branch(str(git_repo), "hw/A004/feat-clean", "main")

    assert res["success"] is True, res
    assert "conflict_markers" not in res


async def test_merge_tool_creates_cleanup_task_for_owner(
    git_repo: Path,
) -> None:
    """tool 层: 命中标记 → 建清理任务给被合并 worktree 的 owner + 警告。"""
    await _make_marked_branch(git_repo, "A004", "feat-x", "marked.py")
    create_mock = AsyncMock(return_value="task-cleanup-1")

    result = await _call_merge_tool(git_repo, "A003", "A004", create_mock)

    assert result.success is True, result.error
    # merge 本身成功 — 残留标记确实随提交落到 main
    assert (git_repo / "marked.py").exists()
    out = result.output
    assert "WARNING" in out
    assert "marked.py" in out
    assert "task-cleanup-1" in out
    assert "A004" in out  # assignee 提示含 owner short_id
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs["title"] == "清理合并残留冲突标记"
    assert kwargs["assignee_id"] == "agent-owner-1"
    assert "marked.py" in kwargs["description"]


async def test_merge_tool_clean_merge_no_warning_no_task(
    git_repo: Path,
) -> None:
    await _make_branch_with_file(git_repo, "A004", "feat-clean", "clean.py",
                                 "print('ok')\n")
    create_mock = AsyncMock(return_value="task-x")

    result = await _call_merge_tool(git_repo, "A003", "A004", create_mock)

    assert result.success is True, result.error
    assert "conflict marker" not in result.output.lower()
    create_mock.assert_not_awaited()


async def test_merge_tool_task_failure_degrades_to_warning(
    git_repo: Path,
) -> None:
    """建账失败不回滚 merge — 结果仍成功, 警告注明 fallback 手动 rework。"""
    await _make_marked_branch(git_repo, "A004", "feat-x", "marked.py")
    create_mock = AsyncMock(side_effect=RuntimeError("db down"))

    result = await _call_merge_tool(git_repo, "A003", "A004", create_mock)

    assert result.success is True, result.error
    assert (git_repo / "marked.py").exists()  # merge 未回滚
    out = result.output
    assert "WARNING" in out
    assert "marked.py" in out
    assert "db down" in out
    assert "manually rework A004" in out
