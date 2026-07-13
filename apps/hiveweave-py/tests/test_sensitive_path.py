"""敏感文件路径拦截测试 — is_sensitive_path + execute_bash/execute_run_command.

覆盖范围:
1. is_sensitive_path 函数 — .env / *.pem / id_rsa / credentials 等敏感模式匹配
2. execute_bash 拦截 `cat .env`（统一通过 _validate_command_safety 调用 is_sensitive_path）
3. execute_run_command 拦截 `cat .env`（A3 旁路修复点 — 之前 run_command 缺这个检查）

契约 02: 工具执行器 — 敏感路径检查由 security.py 的 is_sensitive_path 提供，
bash.py 的 _validate_command_safety 统一调用。execute_bash 与 execute_run_command
两个 shell 执行入口都必须经过该检查，防止 agent 通过 bash 读取 .env 等敏感文件。
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

from hiveweave.tools.bash import execute_bash, execute_run_command
from hiveweave.tools.security import is_sensitive_path


# ── is_sensitive_path 单元测试 ──────────────────────────────


class TestIsSensitivePath:
    """is_sensitive_path — 敏感文件模式匹配 + 正常路径放行."""

    def test_env_file_blocked(self):
        r""".env 必须被识别为敏感文件（\.env$ 模式）."""
        assert is_sensitive_path(".env") is True

    def test_cat_env_command_blocked(self):
        """`cat .env` 命令字符串以 .env 结尾，应被识别."""
        assert is_sensitive_path("cat .env") is True

    def test_env_local_blocked(self):
        r""".env.local 必须被识别（\.env\. 模式）."""
        assert is_sensitive_path(".env.local") is True

    def test_pem_file_blocked(self):
        """*.pem 必须被识别."""
        assert is_sensitive_path("server.pem") is True

    def test_id_rsa_blocked(self):
        """id_rsa 必须被识别."""
        assert is_sensitive_path("~/.ssh/id_rsa") is True

    def test_credentials_blocked(self):
        """credentials 必须被识别."""
        assert is_sensitive_path("config/credentials.yaml") is True

    def test_api_key_blocked(self):
        """api_key / api-key 必须被识别（api[_-]?key 模式）."""
        assert is_sensitive_path("api_key.txt") is True
        assert is_sensitive_path("api-key.txt") is True

    def test_case_insensitive_env(self):
        """大写 .ENV 也应被识别（re.IGNORECASE）."""
        assert is_sensitive_path("cat .ENV") is True

    def test_normal_readme_not_blocked(self):
        """README.md 是正常文件，不应被识别为敏感."""
        assert is_sensitive_path("README.md") is False

    def test_normal_source_not_blocked(self):
        """源代码文件不是敏感文件."""
        assert is_sensitive_path("src/main.py") is False
        assert is_sensitive_path("app.tsx") is False

    def test_normal_cat_command_not_blocked(self):
        """`cat README.md` 命令不涉及敏感文件."""
        assert is_sensitive_path("cat README.md") is False


# ── execute_bash / execute_run_command 集成测试 ──────────────


class TestBashSensitivePathBlock:
    """execute_bash 与 execute_run_command 应拦截 `cat .env`.

    重点：execute_run_command 通过 _validate_command_safety 统一调用
    is_sensitive_path（A3 旁路修复），本测试防止该回归 —— 之前 run_command
    缺少敏感路径检查，agent 可通过 escape hatch 读取 .env。
    """

    def setup_method(self):
        """每个测试创建临时工作区目录."""
        self.workspace = tempfile.mkdtemp(prefix="hiveweave_sensitive_test_")

    def teardown_method(self):
        """测试结束清理临时目录."""
        shutil.rmtree(self.workspace, ignore_errors=True)

    async def test_execute_bash_blocks_cat_env(self):
        """execute_bash 必须拦截 `cat .env`."""
        result = await execute_bash(
            command="cat .env",
            workdir="",
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()
        assert result["output"] == ""

    async def test_run_command_blocks_cat_env(self):
        """execute_run_command 必须拦截 `cat .env`（A3 旁路修复点）."""
        result = await execute_run_command(
            command="cat .env",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()
        assert result["output"] == ""

    async def test_execute_bash_blocks_cat_env_local(self):
        """execute_bash 必须拦截 `cat .env.local`."""
        result = await execute_bash(
            command="cat .env.local",
            workdir="",
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()

    async def test_run_command_blocks_cat_env_local(self):
        """execute_run_command 必须拦截 `cat .env.local`."""
        result = await execute_run_command(
            command="cat .env.local",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "sensitive" in result["error"].lower()

    async def test_blocked_env_output_is_empty(self):
        """被拦截的 `cat .env` 不应实际执行（output 必须为空）."""
        result = await execute_bash(
            command="cat .env",
            workdir="",
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        # 被拦截时命令根本没跑，output 必须为空
        assert result["output"] == ""

    async def test_run_command_blocked_env_output_is_empty(self):
        """被拦截的 `cat .env` 在 run_command 中也不应实际执行."""
        result = await execute_run_command(
            command="cat .env",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert result["output"] == ""

    async def test_normal_cat_readme_not_blocked(self):
        """`cat README.md` 不应被敏感路径检查拦截.

        可能因命令不存在（Windows 无 cat）而失败，但只要 error 不含
        "sensitive" 即可证明敏感路径检查未误伤正常命令。
        """
        result = await execute_bash(
            command="cat README.md",
            workdir="",
            workspace_path=self.workspace,
        )
        assert "sensitive" not in (result.get("error") or "").lower()

    async def test_run_command_normal_echo_not_blocked(self):
        """execute_run_command 对正常命令（echo hello）不应触发敏感路径拦截."""
        result = await execute_run_command(
            command="echo hello",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert "sensitive" not in (result.get("error") or "").lower()
