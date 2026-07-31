"""P1 回归测试 — consume_ids 扩展到所有 TEST_RUN capability agent。

TEST18 死锁放大器：coordinator (player-coach) 跑了最新测试，但 CEO 无法
consume 他的 attestation —— consume_ids 只含 assignee + qa family。
小团队里 coordinator 常是跑测最积极的人，这个限制把脱困路径收窄到 0。

修复后 consume_ids = {assignee} ∪ {所有 active TEST_RUN capability agent}，
含 coordinator。task_binding 仍由 find_reviewer_attestation SQL 强制
（task_id 匹配），不会让无关任务 attestation 解锁 gate。

覆盖：
- 正向：CEO consume 非 assignee coordinator 的同任务 test_run 成功
- 负向：task_id 不匹配时 find_reviewer_attestation 返回 False → approve 拒绝
- 边界：archived agent 不进入 consume_ids
- 边界：reviewer 本人（有 TEST_RUN 时）走 reviewer_must_hold=True 路径
        （CEO 无 TEST_RUN → reviewer_must_hold=False，不会自 consume）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers (mirror test_audit_test6_ylgy_deadlock.py) ────────────────────


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


def _exec(assignee_id="exec1", **extra):
    return {
        "id": assignee_id,
        "role": "board engineer",
        "permission_type": "executor",
        "status": "active",
        **extra,
    }


def _qa(**extra):
    return {
        "id": "qa1",
        "role": "test_engineer",
        "permission_type": "executor",
        "status": "active",
        **extra,
    }


# ── 正向：CEO consume 非 assignee coordinator 的同任务 attestation ────────


@pytest.mark.asyncio
async def test_ceo_consume_non_assignee_coordinator_attestation():
    """CEO approve 时 consume_ids 应包含非 assignee 的 coordinator。

    TEST18 场景：assignee=exec1（attestation stale），coordinator coord1
    跑了新鲜测试。修复前 coord1 不在 consume_ids → 死锁；
    修复后 coord1 在 consume_ids → CEO 可 consume。
    """
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "Optimize game",
        "tags": [],
        "assignee_id": "exec1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"tests_passed": True},
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
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=True),
        ) as find_att,
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            AsyncMock(return_value={"id": "m1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            side_effect=lambda aid: _ceo() if aid == "ceo1" else _coord()
        )
        # team: CEO (no TEST_RUN) + coordinator (TEST_RUN) + executor (TEST_RUN, assignee) + QA (TEST_RUN)
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec(), _qa()]
        )
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = MagicMock(return_value=False)
        ts.start_review = AsyncMock()
        ts.review_task = AsyncMock(return_value=task)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="consume coord"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True, result.output or result.error
    # P1 核心：coordinator（非 assignee）在 consume_ids 中
    consume_ids = find_att.await_args.kwargs.get("consume_agent_ids") or []
    assert "coord1" in consume_ids, (
        f"P1 regression: coordinator coord1 should be in consume_ids, "
        f"got {consume_ids}"
    )
    # assignee 也应在
    assert "exec1" in consume_ids
    # qa 也应在（向后兼容）
    assert "qa1" in consume_ids
    # CEO 自己不应在（reviewer 本人排除，避免自 consume）
    assert "ceo1" not in consume_ids


# ── 边界：archived agent 不进入 consume_ids ────────────────────────────────


@pytest.mark.asyncio
async def test_archived_agent_excluded_from_consume_ids():
    """archived coordinator 不应进入 consume_ids（无 active attestation 可 consume）。"""
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "T",
        "tags": [],
        "assignee_id": "exec1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"tests_passed": True},
    }
    archived_coord = _coord(status="archived")
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
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=True),
        ) as find_att,
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            AsyncMock(return_value={"id": "m1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            side_effect=lambda aid: _ceo() if aid == "ceo1" else _coord()
        )
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), archived_coord, _exec()]
        )
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = MagicMock(return_value=False)
        ts.start_review = AsyncMock()
        ts.review_task = AsyncMock(return_value=task)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="archived excluded"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True, result.output or result.error
    consume_ids = find_att.await_args.kwargs.get("consume_agent_ids") or []
    # archived coord1 不应出现
    assert "coord1" not in consume_ids, (
        f"archived coordinator should be excluded, got {consume_ids}"
    )
    # active executor 仍在
    assert "exec1" in consume_ids


# ── 负向：task_id 不匹配时 find_reviewer_attestation 拒绝 ─────────────────


@pytest.mark.asyncio
async def test_task_binding_still_enforced_by_find_reviewer_attestation():
    """P1 安全前提：扩展 consume_ids 不绕过 task_binding。

    find_reviewer_attestation 的 SQL 仍强制 task_id IN (task_id, *ancestor)。
    即使 coord1 在 consume_ids 中，若其 attestation 绑定到别的 task_id，
    find_reviewer_attestation 返回 False → approve 拒绝。
    这是 P1 放宽不破坏安全的关键。
    """
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "T",
        "tags": [],
        "assignee_id": "exec1",
        "creator_id": "ceo1",
        "status": "submitted",
        "evidence": {"tests_passed": True},
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
        # 模拟 task_binding 不匹配 → find 返回 False
        patch(
            "hiveweave.services.attestation.find_reviewer_attestation",
            AsyncMock(return_value=False),
        ) as find_att,
        patch(
            "hiveweave.services.attestation.ancestor_task_ids",
            AsyncMock(return_value=[]),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            AsyncMock(return_value={"id": "m1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(return_value=None),
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            side_effect=lambda aid: _ceo() if aid == "ceo1" else _coord()
        )
        Org.return_value.list_agents = AsyncMock(
            return_value=[_ceo(), _coord(), _exec()]
        )
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = MagicMock(return_value=False)
        ts.start_review = AsyncMock()
        ts.review_task = AsyncMock(return_value=task)
        result = await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="binding mismatch"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    # task_binding 不匹配 → approve 拒绝（P1 放宽不破坏 task_binding 安全门）
    assert result.success is False, (
        "task_binding mismatch should reject approve even with widened consume_ids"
    )
    # coord1 确实被传给了 find_reviewer_attestation（P1 生效），但 SQL 层拒绝
    consume_ids = find_att.await_args.kwargs.get("consume_agent_ids") or []
    assert "coord1" in consume_ids
