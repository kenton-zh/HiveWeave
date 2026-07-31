"""P0-3 回归测试 — approve 拒绝信息精确化。

TEST18 死锁放大器：approve 因 attestation gate 失败被拒时，错误信息笼统
提示 waiver 规则，导致 reviewer（A007）误判自己是 waived_by 而拒绝尝试
approve（实际只有 A005 是 waived_by）。

修复后 attestation gate 失败分支（无 active waiver）明确告知：
- "Current state: NO active waiver on this task"
- "you are NOT blocked by waived_by third-party rule"
- "The blocker is missing/stale attestation evidence"
- 提示用 get_tasks 查 waiver 状态

覆盖：
- 正向：gate 失败 + 无 waiver → 信息含 "NO active waiver" + "NOT blocked"
- 正向：信息含 "get_tasks" 引导 agent 自查
- 边界：信息仍含原有的 options（waive_attestation / rework）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ceo(**extra):
    return {"id": "ceo1", "role": "ceo", "status": "active", **extra}


def _coord(**extra):
    return {
        "id": "coord1",
        "role": "frontend tech lead",
        "permission_type": "coordinator",
        "status": "active",
        **extra,
    }


def _exec(**extra):
    return {
        "id": "exec1",
        "role": "board engineer",
        "permission_type": "executor",
        "status": "active",
        **extra,
    }


@pytest.mark.asyncio
async def test_approve_reject_attestation_gate_says_no_active_waiver():
    """gate 失败 + 无 waiver → 信息明确说 'NO active waiver' + 'NOT blocked'。

    TEST18 根因：A007 看到 attestation gate 失败，误以为自己是 waived_by。
    修复后信息明确区分「缺 attestation」vs「waived_by 隔离」。

    触发 P0-3 分支：evidence 含 attestation_ids 但 verify_ids 失败
    （走 line 247 的 `if needed and not waived` 分支，而非 capability check）。
    """
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "Optimize game",
        "tags": ["ui_browser_e2e"],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "status": "submitted",
        # 含 attestation_ids 但会是 stale → verify_ids 失败 → 走 P0-3 分支
        "evidence": {"attestation_ids": ["stale-att-id"]},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        # submitter attestation verify 失败（stale id）
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            AsyncMock(return_value=(False, "attestation not found or expired")),
        ),
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.list_reviewer_attestations_diag",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.get_agent = AsyncMock(return_value=_ceo())
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="gate failed"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    # P0-3 核心：明确告知无 active waiver，不是 waived_by 隔离
    assert "NO active waiver" in out, (
        f"should say 'NO active waiver' to prevent waived_by misjudgment:\n{out}"
    )
    assert "NOT blocked by waived_by" in out, (
        f"should say 'NOT blocked by waived_by':\n{out}"
    )
    assert "missing/stale attestation" in out, (
        f"should point to attestation as the real blocker:\n{out}"
    )


@pytest.mark.asyncio
async def test_approve_reject_attestation_gate_points_to_get_tasks():
    """gate 失败信息应引导 agent 用 get_tasks 自查 waiver 状态。"""
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "T",
        "tags": ["ui_browser_e2e"],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"attestation_ids": ["stale-att-id"]},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            AsyncMock(return_value=(False, "attestation not found or expired")),
        ),
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.list_reviewer_attestations_diag",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.get_agent = AsyncMock(return_value=_ceo())
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="self check"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    assert "get_tasks" in out, (
        f"should point to get_tasks for waiver self-check:\n{out}"
    )


@pytest.mark.asyncio
async def test_approve_reject_still_lists_waive_and_rework_options():
    """精确化信息不应丢失原有的 waive/rework options。"""
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "T",
        "tags": ["ui_browser_e2e"],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"attestation_ids": ["stale-att-id"]},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.get_valid_waiver",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            AsyncMock(return_value=(False, "attestation not found or expired")),
        ),
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.list_reviewer_attestations_diag",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
    ):
        Org.return_value.get_agent = AsyncMock(return_value=_ceo())
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="options check"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    out = result.output or result.error or ""
    # 仍含 waive_attestation 选项
    assert "waive_attestation" in out
    # 仍含 rework 选项
    assert "rework" in out.lower()
    # 仍含 taskId 提示
    assert "t-parent" in out
