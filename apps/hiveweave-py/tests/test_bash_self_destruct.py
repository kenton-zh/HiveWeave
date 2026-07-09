"""A3 回归测试 — execute_run_command 的自毁命令检查.

A3 bug: run_command 之前跳过了自毁检查，导致 rm -rf /、format、shutdown 等
系统级破坏命令能通过 escape hatch 直接执行。现已修复为统一调用
check_self_destructive（与 execute_bash 一致）。

本测试防止该回归，覆盖两个层面：
1. check_self_destructive 函数 — 7 个正则模式的匹配 / 放行逻辑
2. execute_run_command — 确保实际调用 check_self_destructive（A3 修复点）
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

from hiveweave.tools.bash import (
    SELF_DESTRUCTIVE_PATTERNS,
    check_self_destructive,
    execute_run_command,
)


# ── check_self_destructive 单元测试 ──────────────────────────


class TestCheckSelfDestructive:
    """check_self_destructive 函数 — 7 个自毁模式匹配 + 正常命令放行."""

    def test_rm_rf_root_blocked(self):
        """rm -rf / 必须被拦截（pattern 1：根目录递归删除）."""
        blocked, reason = check_self_destructive("rm -rf /")
        assert blocked is True
        assert reason == "system-level destructive command"

    def test_format_c_blocked(self):
        """format c: 必须被拦截（pattern 2：Windows 格式化磁盘）."""
        blocked, reason = check_self_destructive("format c:")
        assert blocked is True
        assert "destructive" in reason

    def test_format_uppercase_blocked(self):
        """FORMAT D: 大小写不敏感也应被拦截（re.I 标志）."""
        blocked, _ = check_self_destructive("FORMAT D:")
        assert blocked is True

    def test_shutdown_blocked(self):
        """shutdown /s 必须被拦截（pattern 4：substring 匹配）."""
        blocked, _ = check_self_destructive("shutdown /s")
        assert blocked is True

    def test_normal_ls_not_blocked(self):
        """ls 是正常命令，不应被拦截."""
        blocked, reason = check_self_destructive("ls")
        assert blocked is False
        assert reason == ""

    def test_normal_echo_not_blocked(self):
        """echo hello 是正常命令，不应被拦截."""
        blocked, reason = check_self_destructive("echo hello")
        assert blocked is False
        assert reason == ""

    def test_normal_git_status_not_blocked(self):
        """git status 是正常命令，不应被拦截."""
        blocked, reason = check_self_destructive("git status")
        assert blocked is False
        assert reason == ""

    def test_empty_command_not_blocked(self):
        """空命令不应被自毁检查拦截（空检查由上层 execute_* 处理）."""
        blocked, reason = check_self_destructive("")
        assert blocked is False
        assert reason == ""

    def test_diskpart_blocked(self):
        """diskpart 必须被拦截（pattern 3：Windows 磁盘分区工具）."""
        blocked, _ = check_self_destructive("diskpart /list")
        assert blocked is True

    def test_reboot_blocked(self):
        """reboot 必须被拦截（pattern 5）."""
        blocked, _ = check_self_destructive("reboot now")
        assert blocked is True

    def test_poweroff_blocked(self):
        """poweroff 必须被拦截（pattern 6）."""
        blocked, _ = check_self_destructive("poweroff")
        assert blocked is True

    def test_halt_blocked(self):
        """halt 必须被拦截（pattern 7，word boundary）."""
        blocked, _ = check_self_destructive("halt")
        assert blocked is True

    def test_halt_substring_not_blocked(self):
        """halt 的子串（如 'halted'）不应被拦截（\\bhalt\\b word boundary 保护）."""
        blocked, _ = check_self_destructive("echo halted")
        assert blocked is False

    def test_patterns_count(self):
        """确认自毁模式数量为 7（契约 02）— 防止模式被意外删除."""
        assert len(SELF_DESTRUCTIVE_PATTERNS) == 7


# ── execute_run_command 自毁检查集成测试（A3 修复点）──────────


class TestRunCommandSelfDestructive:
    """execute_run_command 必须调用 check_self_destructive（A3 修复）.

    A3 bug: 之前 run_command 跳过了自毁检查，导致 rm -rf / 等命令
    能直接执行。修复后统一调用 check_self_destructive。
    """

    def setup_method(self):
        """每个测试创建临时工作区目录（execute_run_command 需要 workspace_path）."""
        self.workspace = tempfile.mkdtemp(prefix="hiveweave_a3_test_")

    def teardown_method(self):
        """测试结束清理临时目录."""
        shutil.rmtree(self.workspace, ignore_errors=True)

    async def test_rm_rf_root_blocked_in_run_command(self):
        """rm -rf / 在 execute_run_command 中必须返回 Command blocked."""
        result = await execute_run_command(
            command="rm -rf /",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "Command blocked" in result["error"]
        assert "destructive" in result["error"]

    async def test_format_c_blocked_in_run_command(self):
        """format c: 在 execute_run_command 中必须返回 Command blocked."""
        result = await execute_run_command(
            command="format c:",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "Command blocked" in result["error"]

    async def test_shutdown_blocked_in_run_command(self):
        """shutdown /s 在 execute_run_command 中必须返回 Command blocked."""
        result = await execute_run_command(
            command="shutdown /s",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "Command blocked" in result["error"]

    async def test_diskpart_blocked_in_run_command(self):
        """diskpart 在 execute_run_command 中必须返回 Command blocked."""
        result = await execute_run_command(
            command="diskpart /list",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "Command blocked" in result["error"]

    async def test_blocked_command_output_is_empty(self):
        """被拦截的自毁命令不应实际执行（output 必须为空）."""
        result = await execute_run_command(
            command="rm -rf /",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        # 被拦截时命令根本没跑，output 必须为空
        assert result["output"] == ""
        assert result["success"] is False

    async def test_normal_command_echo_not_blocked(self):
        """正常命令（echo hello）不应被自毁检查拦截.

        可能因其他原因失败（cwd、退出码等），但只要 error 不是
        "Command blocked" 即可证明 A3 修复未误伤正常命令。
        """
        result = await execute_run_command(
            command="echo hello",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        # 关键断言：error 绝不能是 Command blocked
        assert "Command blocked" not in (result.get("error") or "")

    async def test_normal_command_ls_not_blocked(self):
        """正常命令（ls）不应被自毁检查拦截."""
        result = await execute_run_command(
            command="ls",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert "Command blocked" not in (result.get("error") or "")

    async def test_normal_command_git_status_not_blocked(self):
        """git status 不应被自毁检查拦截."""
        result = await execute_run_command(
            command="git status",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert "Command blocked" not in (result.get("error") or "")

    async def test_empty_command_returns_required_error(self):
        """空命令返回 command is required（不是 Command blocked）."""
        result = await execute_run_command(
            command="",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        # 空命令的报错是 "command is required"，与自毁检查无关
        assert "Command blocked" not in result["error"]
        assert "required" in result["error"]
