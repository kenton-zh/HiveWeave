"""BUG-ORGWT 疏通 + 审计 P1 回归：submit 自动 no_code_change 旗标。

7dede94 引入：attestation 背书但零代码变更（verification-only 交付）时
submit 自动补 evidence.no_code_change，避免 approve 被 worktree gate 硬拒。

2026-08-05 审计 P1：软策略（generic_tests/coordinator_review）下
attestation_ids 是 agent 自述、strict 门不校验——自动补旗标前必须先
verify_ids 验真，否则伪造 ID + 空交付即可借旗标绕过 review/close 双侧
merge gate。本文件锁死正反两路：
- 验真通过 → 补旗标（疏通信道保留）
- 验真失败（伪造/过期/不归属）→ 不补旗标（旁路封死）
- 无 attestation → 不补旗标
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_PROJ = "proj"
_AGENT = "agent-1"


def _params(attestation_ids):
    return SimpleNamespace(
        task_id="task-1",
        summary="verification report delivered",
        commit=None,
        files_changed=None,
        test_output=None,
        tests_passed=True,
        attestation_ids=attestation_ids,
    )


def _task():
    return {
        "id": "task-1",
        "status": "running",
        "tags": [],
        "policy_id": "generic_tests",  # 软策略：strict attestation 门跳过
        "title": "platform feature verification",
        "description": "",
        "assignee_id": _AGENT,
        "creator_id": None,  # 无 creator → 跳过 reviewer 通知段
    }


async def _run(params, verify_result):
    """驱动 submit_task_tool（软策略 + 无 worktree → diff 挖不到文件），
    返回 (result, captured_evidence, verify_mock)。"""
    captured: dict = {}

    async def _capture(project_id, task_id, evidence):
        captured["evidence"] = evidence

    stack = ExitStack()
    TS = stack.enter_context(patch("hiveweave.services.task.TaskService"))
    stack.enter_context(
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value=_PROJ,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    verify_mock = stack.enter_context(
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new_callable=AsyncMock,
            return_value=verify_result,
        )
    )
    with stack:
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=_task())
        ts.submit_task = AsyncMock(side_effect=_capture)

        from hiveweave.tools.tasks.submit import submit_task_tool

        result = await submit_task_tool(params, _AGENT, "/tmp/ws")
    return result, captured.get("evidence") or {}, verify_mock


@pytest.mark.asyncio
async def test_auto_flag_set_when_attestations_verified():
    """验真通过 + 零代码变更 → 自动补 no_code_change（疏通信道）。"""
    result, ev, verify_mock = await _run(_params(["att-real-1"]), (True, ""))
    assert result.success is True
    verify_mock.assert_awaited_once()
    assert ev.get("no_code_change") is True
    assert ev.get("_auto_no_code_change") == "attestation_only_delivery"


@pytest.mark.asyncio
async def test_auto_flag_rejected_when_attestations_forged():
    """P1 回归：伪造 attestation ID（验真失败）→ 不得补旗标。"""
    result, ev, _ = await _run(
        _params(["att-forged"]), (False, "Attestation not found: att-forged")
    )
    assert result.success is True
    assert ev.get("no_code_change") is not True
    assert "_auto_no_code_change" not in ev


@pytest.mark.asyncio
async def test_no_auto_flag_without_attestations():
    """无 attestation + 零代码变更 → 不补旗标，verify_ids 不被调用。"""
    result, ev, verify_mock = await _run(_params(None), (True, ""))
    assert result.success is True
    verify_mock.assert_not_awaited()
    assert ev.get("no_code_change") is not True
