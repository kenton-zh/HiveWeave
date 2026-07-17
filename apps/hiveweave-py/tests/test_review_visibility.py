"""审查可见性 — BUGFIX #4 grep/list_files 看不到 worktree。

回归场景（井字棋实测）：reviewer 在主树 grep tictactoe 找不到 → 误判
"代码未交付"，实际代码在 .hiveweave/worktrees/A004/（被 gitignore + 隐藏
目录规则 + IGNORED_DIRS 三重屏蔽）。

修复：include_ignored 参数（rg --no-ignore-vcs --hidden；fallback 放开
.hiveweave）；worktrees 路径下自动放开。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.tools.file import list_files
from hiveweave.tools.grep import _walk_files, execute_grep


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".hiveweave" / "worktrees" / "A004").mkdir(parents=True)
    (ws / ".hiveweave" / "worktrees" / "A004" / "tictactoe.py").write_text(
        "def minimax():\n    return 0\n", encoding="utf-8"
    )
    (ws / ".hiveweave" / "data.db").write_bytes(b"\x00\x01")  # 系统文件
    (ws / "main.py").write_text("print('main tree')\n", encoding="utf-8")
    return ws


class TestWalkFilesIncludeIgnored:
    def test_default_hides_hiveweave(self, workspace: Path):
        files = _walk_files(workspace, None)
        assert all(".hiveweave" not in f.parts for f in files)
        assert any(f.name == "main.py" for f in files)

    def test_include_ignored_finds_worktree_code(self, workspace: Path):
        files = _walk_files(workspace, None, include_ignored=True)
        names = [f.name for f in files]
        assert "tictactoe.py" in names

    def test_include_ignored_still_skills_git_and_node_modules(
        self, workspace: Path
    ):
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text("x", encoding="utf-8")
        files = _walk_files(workspace, None, include_ignored=True)
        assert all(".git" not in f.parts for f in files)


class TestGrepAutoEnable:
    @pytest.mark.asyncio
    async def test_grep_inside_worktrees_auto_includes_ignored(
        self, workspace: Path
    ):
        """井字棋事故形状：直接对 worktree 路径 grep，无需显式 flag。"""
        result = await execute_grep(
            pattern="minimax",
            path=".hiveweave/worktrees/A004",
            include=None,
            workspace_path=str(workspace),
        )
        assert result["success"] is True
        assert "minimax" in result["output"]

    @pytest.mark.asyncio
    async def test_grep_root_default_still_hides_worktree(
        self, workspace: Path
    ):
        result = await execute_grep(
            pattern="minimax",
            path=".",
            include=None,
            workspace_path=str(workspace),
        )
        assert result["success"] is True
        assert "minimax" not in result["output"]


class TestListFilesIncludeIgnored:
    @pytest.mark.asyncio
    async def test_default_hides_hiveweave_dir(self, workspace: Path):
        result = await list_files(path="", workspace_path=str(workspace))
        assert result["success"] is True
        assert ".hiveweave" not in result["output"]

    @pytest.mark.asyncio
    async def test_include_ignored_lists_hiveweave(self, workspace: Path):
        result = await list_files(
            path="", workspace_path=str(workspace), include_ignored=True
        )
        assert ".hiveweave" in result["output"]

    @pytest.mark.asyncio
    async def test_worktrees_path_auto_enabled(self, workspace: Path):
        result = await list_files(
            path=".hiveweave/worktrees/A004",
            workspace_path=str(workspace),
        )
        assert result["success"] is True
        assert "tictactoe.py" in result["output"]
