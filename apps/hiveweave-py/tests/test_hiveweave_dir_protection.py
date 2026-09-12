"""Tests for `.hiveweave` system directory protection across tools.

确保所有工具（bash/patch/file/grep/review）都不能操作 .hiveweave 内的系统文件
（data.db, tool_outputs/）。agent 的工作文件（reports/, drafts/）允许访问。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from textwrap import dedent as _dedent

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
    """list_files 允许列出 .hiveweave 根目录，但过滤敏感文件（data.db 等）。"""

    @pytest.mark.asyncio
    async def test_list_files_hiveweave_filters_sensitive(self, tmp_path: Path):
        from hiveweave.tools.file import list_files
        hw_dir = tmp_path / ".hiveweave"
        hw_dir.mkdir()
        (hw_dir / "data.db").write_text("fake")
        (hw_dir / "shared").mkdir()
        result = await list_files(
            path=".hiveweave",
            workspace_path=str(tmp_path),
        )
        # 列出成功（允许访问 .hiveweave 根目录）
        assert result["success"] is True
        # 敏感文件 data.db 被过滤
        assert "data.db" not in result.get("output", "")
        # shared 子目录可见
        assert "shared" in result.get("output", "")

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

    def test_allows_handoffs(self, tmp_path: Path):
        """handoffs/ 是解散交接文档目录，上级须能 read_file 读取（P0 回归守卫）。"""
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "handoffs" / "a001-dismissal.md"),
            str(tmp_path),
        ) is False

    def test_allows_outside_hiveweave(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / "README.md"), str(tmp_path)
        ) is False

    def test_allows_shared(self, tmp_path: Path):
        """shared/ 是团队共享空间，应放行。"""
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "shared" / "plan.md"), str(tmp_path)
        ) is False

    def test_allows_shared_nested(self, tmp_path: Path):
        """shared/ 下的嵌套子目录也应放行。"""
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "shared" / "docs" / "notes.md"),
            str(tmp_path),
        ) is False


# ── shared/ 共享空间放行测试 ─────────────────────────────────


class TestSharedDirAccess:
    """团队共享空间 .hiveweave/shared/ 应允许所有工具读写。"""

    def test_bash_allows_shared_write(self):
        """bash 应放行指向 .hiveweave/shared/ 的文件操作。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "echo notes > .hiveweave/shared/notes.md"
        ) is False

    def test_bash_allows_shared_dir_without_trailing_slash(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command("cat .hiveweave/shared") is False
        assert _check_hiveweave_command("ls .hiveweave/shared") is False

    def test_bash_allows_shared_cat(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "cat .hiveweave/shared/plan.md"
        ) is False

    def test_bash_allows_shared_windows_path(self):
        """Windows 反斜杠路径也应放行。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            r"type .hiveweave\shared\notes.md"
        ) is False

    def test_bash_still_blocks_data_db(self):
        """放行 shared/ 后，data.db 仍应被拦截。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "cat .hiveweave/data.db"
        ) is True

    def test_bash_still_blocks_tool_outputs(self):
        """放行 shared/ 后，tool_outputs/ 仍应被拦截。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "cat .hiveweave/tool_outputs/log.txt"
        ) is True

    def test_pytest_injected_ignore_not_blocked(self):
        from hiveweave.services.process_registry import prepare_spawn_command
        from hiveweave.tools.bash import _check_hiveweave_command

        cmd, _, err, _inj = prepare_spawn_command("pytest", project_id="t")
        assert err is None
        assert "--ignore=.hiveweave" in cmd
        assert "--ignore-glob=" in cmd
        assert _check_hiveweave_command(cmd) is False

        cmd_m, _, err_m, _inj_m = prepare_spawn_command(
            "python -m pytest", project_id="t"
        )
        assert err_m is None
        assert _check_hiveweave_command(cmd_m) is False

    def test_pytest_injected_ignore_before_pipe(self):
        """Piped pytest must receive the excludes before the pipe, not after.

        Tail-appending put them behind ``| Select-Object`` — PowerShell
        then rejected them as positional args and the (green) test run
        died (2026-08-30 DSH T1 evidence grind).
        """
        from hiveweave.services.process_registry import prepare_spawn_command

        raw = "uv --project sidecar run python -m pytest tests/ -q 2>&1 | Select-Object -First 80"
        cmd, _, err, _inj = prepare_spawn_command(raw, project_id="t")
        assert err is None
        assert "--ignore=.hiveweave" in cmd
        pipe_pos = cmd.index("|")
        assert cmd.index("--ignore=.hiveweave") < pipe_pos
        assert "Select-Object -First 80" in cmd

    def test_vitest_exclude_before_pipe(self):
        """Same splice rule for vitest behind a pipe."""
        from hiveweave.services.process_registry import prepare_spawn_command

        raw = "npx vitest run 2>&1 | Out-String -Width 200"
        cmd, _, err, _inj = prepare_spawn_command(raw, project_id="t")
        assert err is None
        pipe_pos = cmd.index("|")
        assert cmd.index("--exclude") < pipe_pos

    def test_pytest_flags_not_injected_into_earlier_segment(self):
        """`pip install pytest-cov && pytest` must not be polluted (audit P2).

        The runner token only counts at a segment boundary; a bare name
        match inside an earlier segment (package name, file name) is
        ignored, so the last true invocation gets the excludes.
        """
        from hiveweave.services.process_registry import prepare_spawn_command

        raw = "pip install pytest-cov && python -m pytest tests/ -q"
        cmd, _, err, _inj = prepare_spawn_command(raw, project_id="t")
        assert err is None
        seg2 = cmd.split("&&", 1)[1]
        assert "--ignore=.hiveweave" in seg2
        assert "--ignore" not in cmd.split("&&", 1)[0]

    def test_python_c_import_pytest_injected_ignore_not_blocked(self):
        from hiveweave.services.process_registry import prepare_spawn_command
        from hiveweave.tools.bash import (
            _check_hiveweave_command,
            _validate_command_safety,
        )

        raw = 'python -c "import pytest; pytest.main()"'
        cmd, _, err, _inj = prepare_spawn_command(raw, project_id="t")
        assert err is None
        assert ".hiveweave" in cmd
        assert _check_hiveweave_command(cmd) is False
        blocked, reason = _validate_command_safety(cmd)
        assert blocked is False, reason

    def test_vitest_jest_exclude_flags_stripped(self):
        from hiveweave.tools.bash import _check_hiveweave_command

        # import is a file-op token — strip must drop the exclude or this blocks
        assert _check_hiveweave_command(
            'python -c "import os" --exclude **/.hiveweave/**'
        ) is False
        assert _check_hiveweave_command(
            r'python -c "import os" --testPathIgnorePatterns=\.hiveweave'
        ) is False
        assert _check_hiveweave_command(
            "vitest --exclude **/.hiveweave/**"
        ) is False
        # Prefix of a real path must not be stripped as an exclude flag.
        assert _check_hiveweave_command(
            "cat --ignore=.hiveweave/data.db"
        ) is True

    @pytest.mark.asyncio
    async def test_execute_path_injected_pytest_not_hiveweave_blocked(
        self, tmp_path: Path
    ):
        from hiveweave.services.process_registry import prepare_spawn_command
        from hiveweave.tools.bash import (
            _validate_command_safety,
            execute_bash,
        )

        cmd, _, err, _inj = prepare_spawn_command("pytest", project_id="t")
        assert err is None
        blocked, reason = _validate_command_safety(cmd)
        assert blocked is False, reason
        cmd_c, _, err_c, _inj_c = prepare_spawn_command(
            'python -c "import pytest; pytest.main()"', project_id="t"
        )
        assert err_c is None
        blocked_c, reason_c = _validate_command_safety(cmd_c)
        assert blocked_c is False, reason_c
        result = await execute_bash(
            command="cat .hiveweave/data.db",
            workdir="",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert ".hiveweave" in (result.get("error") or "")

    @pytest.mark.asyncio
    async def test_file_write_shared_allowed(self, tmp_path: Path):
        """write_file 应允许写入 .hiveweave/shared/。"""
        from hiveweave.tools.file import write_file
        hw_shared = tmp_path / ".hiveweave" / "shared"
        hw_shared.mkdir(parents=True)
        result = await write_file(
            file_path=".hiveweave/shared/plan.md",
            content="# Team Plan",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is True
        assert (hw_shared / "plan.md").read_text() == "# Team Plan"

    @pytest.mark.asyncio
    async def test_file_read_shared_allowed(self, tmp_path: Path):
        """read_file 应允许读取 .hiveweave/shared/。"""
        from hiveweave.tools.file import read_file
        hw_shared = tmp_path / ".hiveweave" / "shared"
        hw_shared.mkdir(parents=True)
        (hw_shared / "notes.md").write_text("team notes")
        result = await read_file(
            file_path=".hiveweave/shared/notes.md",
            offset=0,
            limit=100,
            workspace_path=str(tmp_path),
        )
        assert result["success"] is True
        assert "team notes" in result["output"]

    @pytest.mark.asyncio
    async def test_patch_shared_allowed(self, tmp_path: Path):
        """patch 应允许修改 .hiveweave/shared/ 内文件。"""
        from hiveweave.tools.patch import apply_patch
        hw_shared = tmp_path / ".hiveweave" / "shared"
        hw_shared.mkdir(parents=True)
        result = await apply_patch(
            patches=[{"op": "add", "filePath": ".hiveweave/shared/doc.md",
                      "content": "# Shared Doc"}],
            workspace_path=str(tmp_path),
        )
        assert (hw_shared / "doc.md").exists()


# ── merge-quarantine：只读放行、写仍保护（report TEST_DSH_54 #5 v2 收窄版）──
#
# 判据来源（我们自己）：`services/platform_state.py` T2.5 把 merge-quarantine
# 当**只读诊断源**接进平台状态（统计"待处理 quarantine"并回报给 Agent），
# 却因该目录未入白名单而不让 Agent 读 —— 实测 18/18 次拒绝全部指向它。
# 反向：隔离区由平台自管（git_worktree 把阻塞 merge 的 untracked 文件搬进去），
# agent 不得改写 ⇒ 读放行 / 写保护。


class TestMergeQuarantineReadOnly:
    def test_check_dir_allows_read(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "merge-quarantine" / "20260912-141336"
                / "docs" / "SPEC.md"),
            str(tmp_path),
        ) is False

    def test_check_dir_blocks_write(self, tmp_path: Path):
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "merge-quarantine" / "20260912-141336"
                / "docs" / "SPEC.md"),
            str(tmp_path),
            write=True,
        ) is True

    def test_check_dir_allows_listing(self, tmp_path: Path):
        """agent 要能 list 该目录才知道里面有什么。"""
        from hiveweave.tools.file import _check_hiveweave_dir
        assert _check_hiveweave_dir(
            str(tmp_path / ".hiveweave" / "merge-quarantine"), str(tmp_path)
        ) is False

    @pytest.mark.asyncio
    async def test_read_file_allowed(self, tmp_path: Path):
        from hiveweave.tools.file import read_file
        q = tmp_path / ".hiveweave" / "merge-quarantine" / "20260912-141336"
        q.mkdir(parents=True)
        (q / "NOTE.md").write_text("quarantined conflict")
        result = await read_file(
            file_path=".hiveweave/merge-quarantine/20260912-141336/NOTE.md",
            offset=0,
            limit=100,
            workspace_path=str(tmp_path),
        )
        assert result["success"] is True, result.get("error")
        assert "quarantined conflict" in result["output"]

    @pytest.mark.asyncio
    async def test_list_files_allowed(self, tmp_path: Path):
        from hiveweave.tools.file import list_files
        q = tmp_path / ".hiveweave" / "merge-quarantine" / "20260912-141336"
        q.mkdir(parents=True)
        (q / "NOTE.md").write_text("x")
        result = await list_files(
            path=".hiveweave/merge-quarantine/20260912-141336",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is True, result.get("error")
        assert "NOTE.md" in result["output"]

    @pytest.mark.asyncio
    async def test_write_file_still_blocked(self, tmp_path: Path):
        """写明：读放行不等于写放行（隔离区是平台自管区）。"""
        from hiveweave.tools.file import write_file
        q = tmp_path / ".hiveweave" / "merge-quarantine"
        q.mkdir(parents=True)
        result = await write_file(
            file_path=".hiveweave/merge-quarantine/evil.md",
            content="hack",
            workspace_path=str(tmp_path),
        )
        assert result["success"] is False
        assert not (q / "evil.md").exists()

    def test_bash_allows_read(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "cat .hiveweave/merge-quarantine/20260912-141336/NOTE.md"
        ) is False
        assert _check_hiveweave_command(
            "ls .hiveweave/merge-quarantine"
        ) is False

    def test_bash_blocks_write(self):
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "rm -rf .hiveweave/merge-quarantine/20260912-141336"
        ) is True
        assert _check_hiveweave_command(
            "echo hi > .hiveweave/merge-quarantine/x.md"
        ) is True

    def test_bash_blocks_less_obvious_writers(self):
        """审计 2026-09-12：只读例外靠**写入词表**证明"这是读"，
        词表漏一个动词 = 该目录的写也漏了。`dd`/`ln`/`sqlite3` 在
        `_HIVEWEAVE_FILE_OPS` 里却曾不在写入词表 ⇒ 可绕过只读门。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        assert _check_hiveweave_command(
            "dd of=.hiveweave/merge-quarantine/x.bin"
        ) is True
        assert _check_hiveweave_command(
            "ln -s /etc/passwd .hiveweave/merge-quarantine/link"
        ) is True
        assert _check_hiveweave_command(
            "sqlite3 .hiveweave/merge-quarantine/q.db 'drop table t'"
        ) is True
        # 同一词表也保护 logs 只读例外（既有孔隙一并收口）
        assert _check_hiveweave_command("dd of=.hiveweave/logs/x.bin") is True


# ── 跨层一致性：file.py 与 bash.py 的两份清单不得漂移 ─────────────
#
# report TEST_DSH_54 #5 点名的既有债务：加一个 .hiveweave 子目录要在
# file.py / bash.py / policy.py 三处手工同步，漏一处就出上面那类
# "平台让你读、工具层拒你读" 的分裂。注释哨兵挡不住漂移，**行为化断言**可以：
# 对 file.py 放行的每个子目录，bash.py 必须同样放行；反向亦然（用探针目录）。


class TestHiveweaveAllowlistConsistency:
    """file.py::allowed_subdirs ⇄ bash.py::_ALLOWED_HW_SUBDIRS 行为一致。"""

    def _file_allowed_subdirs(self) -> set[str]:
        """从 file.py 源码里取 allowed_subdirs 字面量（改一处即被本测试发现）。"""
        import ast
        import inspect

        from hiveweave.tools import file as file_mod

        src = inspect.getsource(file_mod._check_hiveweave_dir)
        tree = ast.parse(_dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [getattr(t, "id", None) for t in node.targets]
                if "allowed_subdirs" in targets and isinstance(
                    node.value, ast.Set
                ):
                    return {
                        e.value
                        for e in node.value.elts
                        if isinstance(e, ast.Constant)
                        and isinstance(e.value, str)
                    }
        raise AssertionError(
            "未能在 _check_hiveweave_dir 中找到 allowed_subdirs 集合字面量 —— "
            "改结构时必须同步本守卫"
        )

    def test_bash_allows_every_file_allowed_subdir(self):
        from hiveweave.tools.bash import _check_hiveweave_command

        subs = self._file_allowed_subdirs()
        assert subs, "allowed_subdirs 解析为空 ⇒ 守卫失效（先修守卫）"
        missing = [
            s for s in sorted(subs)
            if _check_hiveweave_command(f"cat .hiveweave/{s}/probe.md") is not False
        ]
        assert not missing, (
            f"这些子目录在 file.py 放行、bash.py 却拦截：{missing} —— "
            "两份清单已漂移（加目录时漏同步 bash.py）"
        )

    def test_merge_quarantine_readonly_in_both_layers(self):
        """只读子目录也不能单边漂移：两层都必须"读放行、写拦截"。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        from hiveweave.tools.file import _check_hiveweave_dir

        probe = ".hiveweave/merge-quarantine/probe/NOTE.md"
        # file.py：读放行、写保护
        assert _check_hiveweave_dir(str(Path("/w") / probe), "/w") is False
        assert _check_hiveweave_dir(str(Path("/w") / probe), "/w", write=True) is True
        # bash.py：读放行、写拦截
        assert _check_hiveweave_command(f"cat {probe}") is False
        assert _check_hiveweave_command(f"rm -rf {probe}") is True

    def test_file_allows_every_bash_allowed_subdir(self):
        """**反向**：bash 放行的子目录，file 层也必须放行。

        审计 2026-09-12 指出上面的正向断言只覆盖一个方向 —— 只把目录加进
        bash.py（漏改 file.py）时正向不转红。本测试补上反向。
        子目录名从 bash 正则里**抠**出来（不手抄第二份清单，避免守卫自身漂移）。
        """
        import re as _re

        from hiveweave.tools.bash import _ALLOWED_HW_SUBDIRS
        from hiveweave.tools.file import _check_hiveweave_dir

        m = _re.search(r"\(\?:([^)]+)\)", _ALLOWED_HW_SUBDIRS.pattern)
        assert m, (
            "无法从 _ALLOWED_HW_SUBDIRS 解析子目录清单 —— 正则形态变了，"
            "请同步本守卫（守卫失效比守卫缺失更危险）"
        )
        subs = [s for s in m.group(1).split("|") if s]
        assert len(subs) >= 6, f"解析出的子目录过少，守卫可能已失效：{subs}"

        missing = [
            s for s in subs
            if _check_hiveweave_dir(str(Path("/w") / f".hiveweave/{s}/probe.md"), "/w")
        ]
        assert not missing, (
            f"这些子目录在 bash.py 放行、file.py 却拦截：{missing} —— "
            "两份清单已漂移（加目录时漏同步 file.py）"
        )

    def test_bash_still_blocks_non_allowlisted_dirs(self):
        """反向对照：不一致不能靠"全都放行"达成。"""
        from hiveweave.tools.bash import _check_hiveweave_command
        from hiveweave.tools.file import _check_hiveweave_dir

        for probe in (".hiveweave/tool_outputs/x.txt", ".hiveweave/data.db"):
            assert _check_hiveweave_dir(str(Path("/w") / probe), "/w") is True
            assert _check_hiveweave_command(f"cat {probe}") is True

