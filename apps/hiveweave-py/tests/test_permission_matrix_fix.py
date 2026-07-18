"""Permission matrix 止血修复回归 — 三 bug。

1. cancel_task / unclaim_task / waive_attestation 未收录进 COORDINATOR_TOOLS
   → coordinator 合法出口被 permission 层 deny，任务台账只进不出。
2. qa 族 capabilities 缺 SOURCE_WRITE → readwrite executor（实测 Echo，
   role='游戏测试工程师'）写测试文件被 write_file 硬门误拒。
3. coordinator deny 提示谎称 'read-only role'，与 policy 受限写白名单不符；
   真实硬门原因只写日志、不返回模型。
"""

from __future__ import annotations

import pytest

from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import (
    Capability,
    has_capability,
    infer_role_family,
    policy_service,
)
from hiveweave.tools.pipeline import build_deny_hint, execute_registered_tool


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


def _coordinator(**kwargs) -> dict:
    return _agent(
        role="ceo",
        name="归零",
        permission_type="coordinator",
        permission_mode="readonly",
        **kwargs,
    )


def _echo(**kwargs) -> dict:
    """实测事故行：Echo (A005), 游戏测试工程师, permission_type=executor + readwrite."""
    return _agent(role="游戏测试工程师", name="Echo", **kwargs)


@pytest.fixture
def svc() -> PermissionService:
    return PermissionService()


def _patch_agent(monkeypatch: pytest.MonkeyPatch, agent: dict) -> None:
    async def fake_get(_aid):
        return agent

    monkeypatch.setattr(
        "hiveweave.services.permission.meta_db.get_agent_by_id", fake_get
    )


# ── Bug 1: 台账出口三工具对 coordinator 放行、对 executor 拦截 ──

EXIT_TOOLS = ("cancel_task", "unclaim_task", "waive_attestation")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", EXIT_TOOLS)
async def test_coordinator_can_use_exit_tools(svc, monkeypatch, tool):
    _patch_agent(monkeypatch, _coordinator())
    decision = await svc.evaluate("a1", tool, {"taskId": "t1", "reason": "r"})
    assert decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", EXIT_TOOLS)
async def test_executor_cannot_use_exit_tools(svc, monkeypatch, tool):
    # 工具本体（task_tools）无角色守卫 → 必须由 policy 硬能力门拦截
    # （cancel/unclaim 需 DISPATCH、waive 需 REVIEW，executor 族均不具备）
    _patch_agent(monkeypatch, _agent())
    decision = await svc.evaluate("a1", tool, {"taskId": "t1", "reason": "r"})
    assert decision == "deny"


def test_exit_tools_in_coordinator_tool_list(svc):
    tools = svc.get_tools_for_agent(_coordinator())
    for t in EXIT_TOOLS:
        assert t in tools


def test_exit_tools_not_in_executor_tool_list(svc):
    tools = svc.get_tools_for_agent(_agent())
    for t in EXIT_TOOLS:
        assert t not in tools


# ── Bug 2: readwrite 测试工程师（Echo）写文件不被误拒 ──────────


@pytest.mark.asyncio
async def test_echo_write_test_file_allowed(svc, monkeypatch):
    _patch_agent(monkeypatch, _echo())
    decision = await svc.evaluate(
        "a1", "write_file", {"filePath": "tests/test_game.py", "content": "x"}
    )
    assert decision == "allow"


@pytest.mark.asyncio
async def test_echo_write_and_edit_source_allowed(svc, monkeypatch):
    _patch_agent(monkeypatch, _echo())
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "src/game.py", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate("a1", "edit_file", {"filePath": "src/game.py"})
        == "allow"
    )


def test_echo_family_still_qa_for_verify_discovery():
    # VERIFY 独立验收依赖 qa 族识别（_find_independent_qa）—— 分类保持不变，
    # 只补回 SOURCE_WRITE 能力。
    echo = _echo()
    assert infer_role_family(echo) == "qa"
    assert has_capability(echo, Capability.BROWSER_ACCEPTANCE)
    assert has_capability(echo, Capability.SOURCE_WRITE)


@pytest.mark.asyncio
async def test_coordinator_write_scope_unchanged(svc, monkeypatch):
    # 回归保护：coordinator 写白名单内放行、写源码仍硬拒
    _patch_agent(monkeypatch, _coordinator())
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "docs/plan.md", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "src/app.py", "content": "x"}
        )
        == "deny"
    )


# ── Bug 3: deny 提示如实（白名单 + 真实原因，无 'read-only role'） ──


def test_deny_hint_coordinator_write_lists_whitelist():
    hint = build_deny_hint("write_file", "coordinator")
    assert "docs/" in hint
    assert ".hiveweave/shared/" in hint
    assert "dispatch_task" in hint
    assert "read-only" not in hint


def test_deny_hint_includes_real_hard_reason():
    reason = policy_service.hard_check(
        _coordinator(), "write_file", {"filePath": "src/app.py"}
    )
    assert reason
    hint = build_deny_hint("write_file", "coordinator", reason)
    assert reason in hint
    assert "read-only" not in hint


def test_deny_hint_generic_for_executor():
    hint = build_deny_hint("bash", "executor")
    assert hint == "Permission denied: bash is blocked for this agent."


@pytest.mark.asyncio
async def test_pipeline_deny_hint_end_to_end(monkeypatch, tmp_path):
    """coordinator 写源码被拒 → pipeline 返回真实原因 + 白名单指引。"""
    import hiveweave.tools.file  # noqa: F401 — 确保 write_file 完成 @tool 注册

    _patch_agent(monkeypatch, _coordinator())

    class _DenyAll:
        async def evaluate(self, *_a, **_k):
            return "deny"

    result = await execute_registered_tool(
        tool_name="write_file",
        raw_args={"filePath": "src/app.py", "content": "x"},
        agent_id="a1",
        workspace_path=str(tmp_path),
        permission=_DenyAll(),
        approval=None,
    )
    assert result is not None
    assert result["success"] is False
    assert "docs/" in result["error"]
    assert ".hiveweave/shared/" in result["error"]
    assert "dispatch_task" in result["error"]
    assert "read-only" not in result["error"]
