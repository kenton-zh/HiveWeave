"""T2.2：command_guard ask 判定接在线审批通道。

TEST_DSH_35 实测：ask 规则命中（如 Remove-Item -Recurse）在 guard 内被
降级成 deny 并记为「[ask→deny: 平台无在线审批]」—— 平台缺陷被记成 Agent
越权。修复后 ask 判定原样上浮，消费方二选一：
resolve_ask_with_approval（在线审批，PermissionRejected/PermissionTimeout
可区分）/ degrade_ask（非交互路径保持历史降级）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.command_guard import (
    GuardVerdict,
    _ASK_DEGRADE,
    degrade_ask,
    evaluate_command,
    resolve_ask_with_approval,
)
from hiveweave.services.approval import PermissionRejected, PermissionTimeout

ASK_CMD = "Remove-Item -Recurse -Force ./build"


def _ask_verdict() -> GuardVerdict:
    v = evaluate_command(ASK_CMD)
    assert v.action == "ask"  # 前提：规则表里确实是 ask
    return v


def test_ask_verdict_survives_evaluation():
    """guard 不再降级：ask 规则命中返回 ask（plan 验证场景）。"""
    v = _ask_verdict()
    assert v.blocked is True
    assert _ASK_DEGRADE not in v.reason  # 降级说明不再由 guard 拼接
    assert "delete_directory" in v.reason  # 疏通提示保留


async def test_resolve_ask_approved_allows():
    _ask_verdict()
    with patch(
        "hiveweave.services.approval.approval_service.request_permission",
        new=AsyncMock(return_value="req-1"),
    ):
        v = await resolve_ask_with_approval(
            _ask_verdict(), agent_id="a1", tool_name="bash",
            tool_args={"command": ASK_CMD},
        )
    assert v.blocked is False and v.action == "allow"


async def test_resolve_ask_rejected_is_distinguishable():
    with patch(
        "hiveweave.services.approval.approval_service.request_permission",
        new=AsyncMock(side_effect=PermissionRejected("user said no")),
    ):
        v = await resolve_ask_with_approval(
            _ask_verdict(), agent_id="a1", tool_name="bash",
        )
    assert v.blocked is True and v.action == "deny"
    assert "Permission rejected" in v.reason
    assert _ASK_DEGRADE not in v.reason  # 用户拒绝 ≠ 平台降级


async def test_resolve_ask_timeout_is_distinguishable():
    with patch(
        "hiveweave.services.approval.approval_service.request_permission",
        new=AsyncMock(side_effect=PermissionTimeout("req-2 timed out")),
    ):
        v = await resolve_ask_with_approval(
            _ask_verdict(), agent_id="a1", tool_name="bash",
        )
    assert v.blocked is True and v.action == "deny"
    assert "timed out" in v.reason


async def test_resolve_ask_channel_error_fails_closed():
    """审批通道本身故障 → fail-closed 降级（历史 _ASK_DEGRADE 语义）。"""
    with patch(
        "hiveweave.services.approval.approval_service.request_permission",
        new=AsyncMock(side_effect=RuntimeError("db gone")),
    ):
        v = await resolve_ask_with_approval(
            _ask_verdict(), agent_id="a1", tool_name="bash",
        )
    assert v.blocked is True and v.action == "deny"
    assert "ask→deny" in v.reason  # 降级说明回到文案里


def test_degrade_ask_keeps_legacy_shape():
    v = degrade_ask(_ask_verdict())
    assert v.blocked is True and v.action == "deny"
    assert _ASK_DEGRADE in v.reason


def test_degrade_and_resolve_passthrough_non_ask():
    allow = GuardVerdict(False, "allow")
    assert degrade_ask(allow) is allow
    deny = GuardVerdict(True, "deny", "x", "r")
    assert degrade_ask(deny) is deny


async def test_resolved_variant_skips_second_prompt():
    """_bash_background → execute_bash 链：上游已解析的 ask 不二次弹审批。"""
    from hiveweave.tools.bash import _validate_command_safety_resolved

    with patch(
        "hiveweave.services.approval.approval_service.request_permission",
        new=AsyncMock(return_value="req-3"),
    ) as req_mock:
        # ask_already_resolved=True → 不应触发 request_permission
        blocked, reason = await _validate_command_safety_resolved(
            ASK_CMD,
            agent_id="a1", tool_name="bash",
            ask_already_resolved=True,
        )
        assert blocked is False
        req_mock.assert_not_awaited()


async def test_resolved_variant_blocked_message_keeps_marker():
    """无审批通道（agent_id 空 / DB 不可用）→ 降级 deny，带 Command blocked。"""
    from hiveweave.tools.bash import _validate_command_safety_resolved

    blocked, reason = await _validate_command_safety_resolved(
        ASK_CMD, agent_id="", tool_name="bash",
    )
    assert blocked is True
    assert "Command blocked" in reason
    assert "ask→deny" in reason
