"""H3 blocked 生产者回归——tool 层护栏拒绝必须带 blocked 标记。

H3 分流链的源头是各工具在「平台拒绝」路径打 blocked 标记；stall 分流测试
（test_stall_blocked_diversion.py）mock 掉了数据源，源头若回归（不打标记）
分流测试仍然全绿——本文件钉住生产者端。审计 P1 覆盖缺口清单 #1-5。

覆盖：
1. ToolExecutor legacy 硬门 deny → blocked=True
2. pipeline 权限 deny / 审批超时 / 审批拒绝 → blocked=True
3. bash 自毁拦截（execute_bash dict + bash_tool 序列化）→ blocked=True
4. get_tools_for_agent 全 family：可见列表 ∩ 硬门拒绝 = ∅
5. build_child_env(bash_markers) 分叉 + SDK 根目录白名单
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import infer_role_family, tool_hard_deny


def _agent(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "墨白",
        "role": "签到工程师",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "allowed_tools": "[]",
        "denied_tools": "[]",
        "ask_tools": "[]",
    }
    base.update(kwargs)
    return base


# ── 1. ToolExecutor legacy 硬门 deny ──────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_executor_deny_marks_blocked():
    """未注册工具走 legacy 权限路径，deny 结果必须带 blocked=True。

    工具名必须选真实可达的 legacy 分发工具（run_tests 属评审套件：未进
    @tool 注册表，由 _dispatch 兜住）—— 用不存在的名字会被未知工具
    fast-fail 拦在权限评估之前（DSH_33），根本走不到 deny 分支。
    """
    from hiveweave.tools.executor import ToolExecutor

    class _Perm:
        async def evaluate_detailed(self, agent_id, tool_name, args):
            return ("deny", "hard-gate: tool not in table")

    with (
        patch(
            "hiveweave.tools.executor.meta_db.get_agent_by_id",
            new=AsyncMock(return_value=_agent(role="ceo", permission_type="coordinator")),
        ),
        tempfile.TemporaryDirectory() as tmp,
    ):
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        executor = ToolExecutor(_Perm(), object())
        result = await executor.execute(
            "a1", "run_tests", {"filePaths": ["a.py"]}, ws
        )

    assert result.get("success") is False
    assert result.get("blocked") is True


# ── 1b. 未知工具 fast-fail：早于权限评估，且不标 blocked ──────────────


@pytest.mark.asyncio
async def test_unknown_tool_fast_fails_before_permission():
    """DSH_33：幻觉工具名必须在权限评估之前失败，且归因工具层（非 blocked）。

    修复前：权限层对未注册名落 mode 兜底 "ask" → 120s 审批超时才失败，
    legacy 的 Unknown tool 分支一次都没执行到。
    """
    from hiveweave.tools.executor import ToolExecutor

    evaluated: list[str] = []

    class _Perm:
        async def evaluate_detailed(self, agent_id, tool_name, args):
            evaluated.append(tool_name)
            return ("ask", None)

    class _Approval:
        async def request_permission(self, **kwargs):
            raise AssertionError("未知工具不得进入审批流程")

    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        executor = ToolExecutor(_Perm(), _Approval())
        result = await executor.execute("a1", "self.bash", {"command": "ls"}, ws)

    # 权限评估从未被调用 —— 可达性判定先于授权判定
    assert evaluated == []
    assert result["success"] is False
    # blocked 语义专指护栏拒绝；未知工具是工具层查找失败
    assert result.get("blocked") is not True
    # 错误消息必须带确切纠正路径（DSH reachableFrom）
    assert "self.bash" in result["error"]
    assert "'bash'" in result["error"]


def test_unknown_tool_error_carries_reachable_path():
    """DSH_33 实测的 5 个幻觉名都必须给出确切纠正建议，真实工具不误伤。"""
    from hiveweave.tools.executor import ToolExecutor

    for hallucinated, real in (
        ("self.bash", "bash"),
        ("self.get_tasks", "get_tasks"),
        ("self.commit_turn", "commit_turn"),
        ("self.browse", "browse"),
        ("self.get_platform_state", "get_platform_state"),
    ):
        msg = ToolExecutor._unknown_tool_error(hallucinated)
        assert msg is not None
        assert f"Did you mean '{real}'?" in msg
        assert "self." in msg  # 明确指出前缀是问题

    # 拼写错误 → 近似匹配，但话术不提前缀
    typo = ToolExecutor._unknown_tool_error("bahs")
    assert typo is not None and "Did you mean 'bash'?" in typo

    # 完全查无此名 → 不瞎猜，改指引查能力范围
    nonsense = ToolExecutor._unknown_tool_error("totally_made_up_tool")
    assert nonsense is not None
    assert "Did you mean" not in nonsense
    assert "get_platform_state" in nonsense

    # 可达工具不得被拦：注册表 + legacy _dispatch 两条路径
    assert ToolExecutor._unknown_tool_error("bash") is None
    assert ToolExecutor._unknown_tool_error("run_tests") is None
    assert ToolExecutor._unknown_tool_error("review") is None


def test_legacy_dispatch_tools_match_actual_dispatch_branches():
    """LEGACY_DISPATCH_TOOLS 必须与 _dispatch 的实际分支一致。

    漏登记 → 真实工具被 fast-fail 误拦；多登记 → 未知工具漏过 fast-fail
    再落到 _dispatch 的 Unknown tool。两种都必须在测试期显形。
    """
    import inspect

    from hiveweave.tools.executor import ToolExecutor
    from hiveweave.tools.pipeline import LEGACY_DISPATCH_TOOLS

    src = inspect.getsource(ToolExecutor._dispatch)
    for name in LEGACY_DISPATCH_TOOLS:
        assert f'"{name}"' in src, f"{name} 已登记但 _dispatch 无对应分支"


# ── 2. pipeline 权限三分支 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_deny_marks_blocked():
    from hiveweave.tools.pipeline import execute_registered_tool

    class _Deny:
        async def evaluate_detailed(self, agent_id, tool_name, args):
            return ("deny", "hard-gate")

    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        result = await execute_registered_tool(
            "bash", {"command": "ls"}, "agent-1", ws, _Deny(), None
        )

    assert result is not None
    assert result["success"] is False
    assert result.get("blocked") is True


@pytest.mark.asyncio
async def test_pipeline_permission_timeout_marks_blocked():
    from hiveweave.services.approval import PermissionTimeout
    from hiveweave.tools.pipeline import execute_registered_tool

    class _Ask:
        async def evaluate_detailed(self, agent_id, tool_name, args):
            return ("ask", None)

    class _Approval:
        async def request_permission(self, **kwargs):
            raise PermissionTimeout("away")

    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        result = await execute_registered_tool(
            "bash", {"command": "ls"}, "agent-1", ws, _Ask(), _Approval()
        )

    assert result["success"] is False
    assert result.get("blocked") is True


# ── 3. bash 自毁拦截 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_bash_self_destruct_marks_blocked():
    from hiveweave.tools.bash import execute_bash

    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        result = await execute_bash("rm -rf /", ws, ws)

    assert result["success"] is False
    assert result.get("blocked") is True


@pytest.mark.asyncio
async def test_bash_tool_blocked_serialization():
    from hiveweave.tools.bash import BashParams, bash_tool

    with tempfile.TemporaryDirectory() as tmp:
        ws = str(Path(tmp) / "ws")
        Path(ws).mkdir()
        result = await bash_tool(BashParams(command="rm -rf /"), "a1", ws)

    d = result.to_dict()
    assert d["success"] is False
    assert d["blocked"] is True


# ── 4. get_tools_for_agent 可见性与硬门对齐 ───────────────────────────


@pytest.mark.parametrize(
    "agent",
    [
        _agent(role="ceo", permission_type="coordinator"),
        _agent(role="hr", permission_type="coordinator"),
        _agent(role="前端架构师", permission_type="coordinator"),
        _agent(role="测试工程师", permission_type="executor"),
        _agent(role="签到工程师", permission_type="executor"),
        # infer_role_family 对未知工种兜底 executor（复审 P2-6：不存在可达的
        # unknown family 分支，此例钉住的是兜底行为本身）
        _agent(role="神秘未知工种", permission_type="readonly"),
    ],
)
def test_no_listed_but_hard_denied(agent):
    svc = PermissionService()
    tools = svc.get_tools_for_agent(agent)
    assert tools, "每个 family 都应有可见工具表"
    hard_denied = {n for n in tools if tool_hard_deny(agent, n) is not None}
    assert hard_denied == set(), (
        f"{infer_role_family(agent)} 可见列表含硬门拒绝工具: {hard_denied}"
    )


def test_executor_qa_keep_start_dev_server():
    """CEO 丢 start_dev_server（BASH_SHELL 门），executor/QA 必须保留。"""
    svc = PermissionService()
    ceo = svc.get_tools_for_agent(_agent(role="ceo", permission_type="coordinator"))
    assert "start_dev_server" not in ceo
    assert "lookup_dev_server" in ceo  # SOURCE_READ，可查不可 spawn
    for a in (
        _agent(role="测试工程师", permission_type="executor"),
        _agent(role="签到工程师", permission_type="executor"),
    ):
        assert "start_dev_server" in svc.get_tools_for_agent(a)


# ── 5. build_child_env 分叉 + SDK 白名单 ──────────────────────────────


def test_build_child_env_bash_markers_and_sdk(monkeypatch):
    from hiveweave.util.safe_env import build_child_env

    monkeypatch.setenv("JAVA_HOME", "C:\\jdk")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "C:\\android")
    monkeypatch.setenv("HIVEWEAVE_ARK_API_KEY", "sk-secret")

    bash_env = build_child_env("C:\\ws", bash_markers=True)
    assert bash_env["HIVEWEAVE_BASH"] == "1"
    assert bash_env["LANG"] == "en_US.UTF-8"
    assert bash_env["LC_ALL"] == "en_US.UTF-8"
    assert bash_env["JAVA_HOME"] == "C:\\jdk"
    assert bash_env["ANDROID_SDK_ROOT"] == "C:\\android"

    spawn_env = build_child_env("C:\\ws", bash_markers=False)
    assert "HIVEWEAVE_BASH" not in spawn_env
    assert spawn_env["JAVA_HOME"] == "C:\\jdk"

    # 密钥永不透传
    for env in (bash_env, spawn_env):
        assert "HIVEWEAVE_ARK_API_KEY" not in env
