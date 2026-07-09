"""Tests for `.hiveweave` system directory protection across tools.

确保所有工具（bash/patch/file/grep/review）都不能操作 .hiveweave 内的系统文件
（data.db, tool_outputs/）。agent 的工作文件（reports/, drafts/）允许访问。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

# ── bash.py: .hiveweave 命令拦截 ─────────────────────────────


class TestBashHiveweaveBlock:
    """bash 工具应拦截针对 .hiveweave 的文件操作命令。"""

    def test_check_hiveweave_command_blocks_rm(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("rm -rf .hiveweave") is True

    def test_check_hiveweave_command_blocks_cat(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("cat .hiveweave/data.db") is True

    def test_check_hiveweave_command_blocks_del(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("del .hiveweave\\data.db") is True

    def test_check_hiveweave_command_blocks_copy(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("copy .hiveweave\\data.db C:\\temp") is True

    def test_check_hiveweave_command_blocks_strings(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("strings .hiveweave/data.db | grep pass") is True

    def test_check_hiveweave_command_allows_ls(self):
        """ls .hiveweave 无害，不拦（实际 list_files 会拦，但 bash 的 ls 不拦）。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("ls -la .hiveweave") is False

    def test_check_hiveweave_command_allows_cd(self):
        """cd .hiveweave 无害，不拦。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("cd .hiveweave") is False

    def test_check_hiveweave_command_allows_unrelated(self):
        """不涉及 .hiveweave 的命令不拦。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("npm install") is False
        assert _check_hiveweave_command("cat README.md") is False
        assert _check_hiveweave_command("rm -rf node_modules") is False

    @pytest.mark.asyncio
    async def test_bash_execute_blocks_hiveweave_rm(self, tmp_path: Path):
        """execute_bash 应拒绝 rm -rf .hiveweave。"""
        from hiveweave.tools.bash import execute_bash
        result = await execute_bash(
            command="rm -rf .hiveweave",
            workdir="",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert ".hiveweave" in result["error"]

    @pytest.mark.asyncio
    async def test_bash_execute_blocks_cat_data_db(self, tmp_path: Path):
        """execute_bash 应拒绝 cat .hiveweave/data.db。"""
        from hiveweave.tools.bash import execute_bash
        result = await execute_bash(
            command="cat .hiveweave/data.db",
            workdir="",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert ".hiveweave" in result["error"]


# ── patch.py: _check_hiveweave_dir ───────────────────────────


class TestPatchHiveweaveBlock:
    """patch 工具应拒绝修改/删除 .hiveweave 内系统文件。"""

    @pytest.mark.asyncio
    async def test_patch_delete_data_db_blocked(self, tmp_path: Path):
        from hiveweave.tools.patch import apply_patch
        # 先创建假的 data.db
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("fake")
        result = await apply_patch(
            patches=[{"op": "delete", "filePath": ".hiveweave/data.db"}],
            workspace_path=str(tmp_path),
        )
        # data.db 应仍然存在
        assert (hw_dir / "data.db").exists()

    @pytest.mark.asyncio
    async def test_patch_add_to_hiveweave_blocked(self, tmp_path: Path):
        from hiveweave.tools.patch import apply_patch
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        result = await apply_patch(
            patches=[{"op": "add", "filePath": ".hiveweave/malicious.txt",
                      "content": "hack"}],
            workspace_path=str(tmp_path),
        )
        assert not (hw_dir / "malicious.txt").exists()

    @pytest.mark.asyncio
    async def test_patch_add_to_reports_allowed(self, tmp_path: Path):
        """reports/ 是工作文件目录，应允许 patch。"""
        from hiveweave.tools.patch import apply_patch
        hw_dir = tmp_path / ".hiveweave" / "reports"
        hw_dir.mkdir(parents=True)
        result = await apply_patch(
            patches=[{"op": "add", "filePath": ".hiveweave/reports/draft.md",
                      "content": "# Draft"}],
            workspace_path=str(tmp_path),
        )
        assert (hw_dir / "draft.md").exists()


# ── file.py: list_files .hiveweave 保护 ─────────────────────


class TestListFilesHiveweaveBlock:
    """list_files 应拒绝显式列出 .hiveweave 目录。"""

    @pytest.mark.asyncio
    async def test_list_files_explicit_hiveweave_blocked(self, tmp_path: Path):
        from hiveweave.tools.file import list_files
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("fake")
        result = await list_files(
            path=".hiveweave",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert ".hiveweave" in result["error"]

    @pytest.mark.asyncio
    async def test_list_files_skips_hiveweave_in_recursive(self, tmp_path: Path):
        """递归列出 workspace 时应跳过 .hiveweave 目录。"""
        from hiveweave.tools.file import list_files
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("fake")
        (tmp_path / "README.md").write_text("hello")
        result = await list_files(
            path="",
            workspace_path=str(tmp_path),
            recursive=True,
        )
        assert result["success"] is True
        # .hiveweave 内容不应出现在输出中
        assert "data.db" not in result.get("output", "")
        assert "README.md" in result.get("output", "")


# ── grep.py: .hiveweave 路径拦截 ─────────────────────────────


class TestGrepHiveweaveBlock:
    """grep 应拒绝显式搜索 .hiveweave 目录。"""

    @pytest.mark.asyncio
    async def test_grep_hiveweave_blocked(self, tmp_path: Path):
        from hiveweave.tools.grep import execute_grep
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("password=hunter2")
        result = await execute_grep(
            pattern="password",
            path=".hiveweave",
            include=None,
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert ".hiveweave" in result["error"]

    @pytest.mark.asyncio
    async def test_grep_workspace_skips_hiveweave(self, tmp_path: Path):
        """workspace 级搜索不应匹配 .hiveweave 内文件。"""
        from hiveweave.tools.grep import execute_grep
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("SECRET_TOKEN=leaked")
        (tmp_path / "app.py").write_text("# no secrets here")
        result = await execute_grep(
            pattern="SECRET_TOKEN",
            path="",
            include=None,
            workspace_path=str(tmp_path),
        )
        # 不应匹配到 .hiveweave 内的内容
        assert "leaked" not in result.get("output", "")


# ── file.py: _check_hiveweave_dir 单元测试 ─────────────────


class TestCheckHiveweaveDir:
    """_check_hiveweave_dir 应精确保护系统文件，放行工作文件。"""

    def test_blocks_data_db(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "data.db"), str(tmp_path)
        ) is True

    def test_blocks_data_db_wal(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "data.db-wal"), str(tmp_path)
        ) is True

    def test_blocks_tool_outputs(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "tool_outputs" / "log.txt"), str(tmp_path)
        ) is True

    def test_allows_reports(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "reports" / "draft.md"), str(tmp_path)
        ) is False

    def test_allows_drafts(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "drafts" / "plan.md"), str(tmp_path)
        ) is False

    def test_allows_outside_hiveweave(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / "README.md"), str(tmp_path)
        ) is False
