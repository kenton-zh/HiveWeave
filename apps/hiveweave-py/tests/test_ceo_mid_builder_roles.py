"""CEO 抽离 + 中层 builder 角色改造 — 专项回归测试。

覆盖计划 docs/plans/ceo-mid-builder-roles.md 的 G 切片补写项：
- ensure_executor_worktree refuse-ceo / refuse-hr / allow-builder（此前
  refuse 分支几乎无直接单测覆盖）
- hire permission_mode 选择（builder coordinator 不再固定 readonly）
- review_task 内 ensure 调用点对 builder 不静默失败
- submit 自交（creator==assignee）wake org parent 而非自己
- git_worktree_merge 自有分支门（须 approved 且批准人≠调用者）
- _find_independent_qa 排除 merger
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from hiveweave.services.git_worktree import (
    agent_gets_write_worktree,
    ensure_executor_worktree,
)
from hiveweave.services.org import OrgService
from hiveweave.tools.misc_tools import _check_self_merge_gate
from hiveweave.tools.org_tools import _hire_permission_mode
from hiveweave.tools.task_tools import (
    ReviewTaskParams,
    SubmitTaskParams,
    _find_independent_qa,
    review_task_tool,
    submit_task_tool,
)


def _agent_row(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "墨白",
        "role": "签到工程师",
        "short_id": "A009",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "workspace_path": "",
    }
    base.update(kwargs)
    return base


# ── agent_gets_write_worktree / ensure refuse & allow ──────────


def test_agent_gets_write_worktree_matrix():
    assert agent_gets_write_worktree(_agent_row()) is True
    assert agent_gets_write_worktree(
        _agent_row(role="前端架构师", permission_type="coordinator")
    ) is True
    assert agent_gets_write_worktree(
        _agent_row(role="ceo", permission_type="coordinator")
    ) is False
    assert agent_gets_write_worktree(
        _agent_row(role="hr", permission_type="coordinator")
    ) is False


@pytest.mark.asyncio
async def test_ensure_worktree_refuses_ceo():
    ceo = _agent_row(
        id="ceo-1", role="ceo", permission_type="coordinator", short_id="A001"
    )
    with patch.object(
        OrgService, "resolve_agent", AsyncMock(return_value=ceo)
    ):
        result = await ensure_executor_worktree("proj-1", "ceo-1")
    assert result["success"] is False
    assert "Refusing worktree" in result["message"]


@pytest.mark.asyncio
async def test_ensure_worktree_refuses_hr():
    hr = _agent_row(
        id="hr-1", role="hr", permission_type="coordinator", short_id="A002"
    )
    with patch.object(
        OrgService, "resolve_agent", AsyncMock(return_value=hr)
    ):
        result = await ensure_executor_worktree("proj-1", "hr-1")
    assert result["success"] is False
    assert "Refusing worktree" in result["message"]


@pytest.mark.asyncio
async def test_ensure_worktree_allows_builder_coordinator(tmp_path):
    (tmp_path / ".git").mkdir()
    builder = _agent_row(
        id="mid-1",
        role="前端架构师",
        permission_type="coordinator",
        short_id="A003",
        workspace_path="",
    )
    create_mock = AsyncMock(
        return_value={
            "success": True,
            "path": str(tmp_path / ".hiveweave/worktrees/A003"),
            "branch": "hw/A003/work",
        }
    )
    with (
        patch.object(OrgService, "resolve_agent", AsyncMock(return_value=builder)),
        patch.object(OrgService, "update_agent", AsyncMock()),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(tmp_path)),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            Mock(return_value=Mock(create=create_mock)),
        ),
    ):
        result = await ensure_executor_worktree("proj-1", "mid-1")
    assert result["success"] is True, result.get("message")
    assert result["short_id"] == "A003"
    create_mock.assert_awaited_once()


# ── hire permission_mode ───────────────────────────────────────


def test_hire_permission_mode():
    # executor / qa → readwrite（原行为）
    assert _hire_permission_mode("executor", "签到工程师") == "readwrite"
    # builder coordinator → 可写（勿死锁 readonly）
    assert _hire_permission_mode("coordinator", "前端架构师") == "readwrite"
    # CEO / HR → 偏只读协调 mode（无 SOURCE_WRITE，docs 白名单够用）
    assert _hire_permission_mode("coordinator", "ceo") == "readonly"
    assert _hire_permission_mode("coordinator", "hr") == "readonly"


# ── review_task：ensure 失败不静默（交给 worktree gate 判定） ──


@pytest.mark.asyncio
async def test_review_task_ensure_failure_not_silently_skipped():
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.task import TaskService

    ensure_mock = AsyncMock(
        return_value={"success": False, "message": "simulated ensure failure"}
    )
    task_row = {
        "id": "t-1",
        "assignee_id": "mid-1",  # builder coordinator assignee
        "status": "reviewing",
        "title": "搭骨架",
        "description": "d",
        "evidence": {"tests_passed": True},
        "tags": [],
    }
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="proj-1"),
        ),
        patch.object(
            TaskService, "get_task", AsyncMock(return_value=dict(task_row))
        ),
        patch.object(TaskService, "review_task", AsyncMock()),
        patch(
            "hiveweave.services.attestation.resolve_task_policy",
            Mock(return_value="docs_only"),
        ),
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            Mock(return_value=[]),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            ensure_mock,
        ),
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            AsyncMock(return_value=("worktree gate deny: no worktree", {})),
        ),
        patch.object(InboxService, "send_message", AsyncMock()),
    ):
        result = await review_task_tool(
            ReviewTaskParams(taskId="t-1", decision="approve"),
            "ceo-1",
            "/tmp",
        )

    assert result.success is False
    assert "worktree gate deny" in (result.error or "")
    # ensure 被真正调用（携带稳定命名 task_id），失败结果未静默吞掉
    ensure_mock.assert_awaited_once()
    assert ensure_mock.call_args.kwargs["task_id"] == "t-1"


# ── submit 自交 → wake org parent ──────────────────────────────


@pytest.mark.asyncio
async def test_submit_self_submit_wakes_parent_not_self():
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.task import TaskService

    task_row = {
        "id": "t-9",
        "assignee_id": "mid-1",
        "creator_id": "mid-1",  # 自交：creator == assignee
        "status": "running",
        "title": "骨架任务",
        "description": "d",
        "policy_id": "docs_only",
        "tags": [],
    }
    sent: dict = {}
    triggered: list[str] = []

    async def fake_send(self, from_agent_id, to_agent_id, message, **kw):
        sent["from"] = from_agent_id
        sent["to"] = to_agent_id
        sent["message"] = message

    async def fake_trigger(aid):
        triggered.append(aid)

    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="proj-1"),
        ),
        patch.object(
            TaskService, "get_task", AsyncMock(return_value=dict(task_row))
        ),
        patch.object(TaskService, "submit_task", AsyncMock()),
        patch(
            "hiveweave.services.attestation.resolve_task_policy",
            Mock(return_value="docs_only"),
        ),
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            Mock(return_value=[]),
        ),
        patch(
            "hiveweave.services.handoff.HandoffService.mark_reported",
            AsyncMock(return_value=0),
        ),
        patch.object(InboxService, "send_message", fake_send),
        patch.object(
            OrgService,
            "resolve_agent",
            AsyncMock(return_value={"id": "mid-1", "parent_id": "ceo-1"}),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            fake_trigger,
        ),
    ):
        result = await submit_task_tool(
            SubmitTaskParams(taskId="t-9", summary="骨架完成", testsPassed=True),
            "mid-1",
            "/tmp",
        )

    assert result.success is True, result.error
    # 发给 org parent（CEO），不发自己；无 self-submit 后缀
    assert sent["to"] == "ceo-1"
    assert "self-submit" not in sent["message"]
    assert triggered == ["ceo-1"]


# ── merge 自有分支门 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_merge_gate_requires_approved_task():
    from hiveweave.services.task import TaskService

    with patch.object(
        TaskService, "list_tasks", AsyncMock(return_value=[])
    ), patch.object(TaskService, "get_task", AsyncMock(return_value=None)):
        err = await _check_self_merge_gate("proj-1", "mid-1", None, None)
    assert err is not None
    assert "no corresponding task" in err


@pytest.mark.asyncio
async def test_self_merge_gate_requires_approved_status():
    from hiveweave.services.task import TaskService

    task = {"id": "abcdef01-2345", "status": "submitted", "assignee_id": "mid-1"}
    with patch.object(
        TaskService, "get_task", AsyncMock(return_value=task)
    ):
        err = await _check_self_merge_gate(
            "proj-1", "mid-1", "abcdef01-2345", None
        )
    assert err is not None
    assert "not approved" in err


@pytest.mark.asyncio
async def test_self_merge_gate_rejects_self_approval():
    from hiveweave.services.task import TaskService

    task = {
        "id": "abcdef01-2345",
        "status": "approved",
        "assignee_id": "mid-1",
        "evidence": {"reviewed_by": "mid-1"},
    }
    with patch.object(
        TaskService, "get_task", AsyncMock(return_value=task)
    ):
        err = await _check_self_merge_gate(
            "proj-1", "mid-1", "abcdef01-2345", None
        )
    assert err is not None
    assert "DIFFERENT" in err


@pytest.mark.asyncio
async def test_self_merge_gate_allows_other_approval():
    from hiveweave.services.task import TaskService

    task = {
        "id": "abcdef01-2345",
        "status": "approved",
        "assignee_id": "mid-1",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    with patch.object(
        TaskService, "get_task", AsyncMock(return_value=task)
    ):
        err = await _check_self_merge_gate(
            "proj-1", "mid-1", "abcdef01-2345", None
        )
    assert err is None


# ── VERIFY picker 排除 merger ──────────────────────────────────


@pytest.mark.asyncio
async def test_find_independent_qa_excludes_merger():
    agents = [
        {"id": "eng-1", "role": "签到工程师", "permission_type": "executor",
         "parent_id": "mid-1", "status": "active"},
        # merger 本人 qa-capable —— 必须被 exclude_ids 排除
        {"id": "mid-1", "role": "qa", "permission_type": "executor",
         "parent_id": "ceo-1", "status": "active"},
        {"id": "qa-2", "role": "测试工程师", "permission_type": "executor",
         "parent_id": "mid-1", "status": "active"},
    ]
    with patch.object(
        OrgService, "list_agents", AsyncMock(return_value=agents)
    ):
        picked = await _find_independent_qa(
            "proj-1",
            original_assignee="eng-1",
            exclude_ids={"mid-1"},
        )
    assert picked == "qa-2"
