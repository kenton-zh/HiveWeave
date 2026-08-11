"""CODE AUDIT POLICY notice: dispatch/create wiring + helper idempotency.

行为契约:
- append_code_audit_notice 幂等 —— marker 只出现一次
- dispatch_task 给写码 assignee（有 write worktree、非 VERIFY）→ 描述含 marker
- dispatch_task 给 VERIFY 任务 / 非写码 assignee（CEO/HR）→ 不含 marker
- create_task_tool 带写码 assignee → 落库 description 含 marker
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.code_audit import append_code_audit_notice
from hiveweave.services.dispatch import DispatchService
from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService
from hiveweave.services.obligation import ObligationLedger
from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.create import CreateTaskParams, create_task_tool

MARKER = "[CODE AUDIT POLICY]"

PROJECT_ID = "test-project"
COORDINATOR_ID = "boss-agent"
ASSIGNEE_ID = "assignee-agent"


def _enter(patches: list) -> ExitStack:
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack

_WRITER = {
    "id": ASSIGNEE_ID,
    "permission_type": "executor",
    "parent_id": COORDINATOR_ID,
    "name": "Exec",
    "short_id": "A009",
}
_NON_WRITER = {
    "id": COORDINATOR_ID,
    "permission_type": "coordinator",
    "parent_id": None,
    "name": "Boss",
    "short_id": "CEO",
}


# ── helper ───────────────────────────────────────────────


def test_append_code_audit_notice_idempotent():
    once = append_code_audit_notice("实现登录模块")
    assert MARKER in once
    assert once.count(MARKER) == 1
    twice = append_code_audit_notice(once)
    assert twice == once
    assert twice.count(MARKER) == 1


# ── dispatch path ────────────────────────────────────────


def _dispatch_patches(assignee: dict, get_task_returns) -> list:
    return [
        patch("hiveweave.services.dispatch._conn", AsyncMock()),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.git_worktree.agent_gets_write_worktree",
            lambda a: (a or {}).get("permission_type") == "executor",
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={
                "success": True,
                "path": "/tmp/wt",
                "short_id": "A009",
            }),
        ),
        patch(
            "hiveweave.services.git_worktree.pin_dispatch_message_to_worktree",
            lambda desc, short_id="", worktree_path="": (
                f"{desc}\n[WORKTREE PATH] {short_id} → {worktree_path}"
            ),
        ),
        patch(
            "hiveweave.services.git_worktree.worktree_commits_behind_main",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.org.OrgService.resolve_agent",
            AsyncMock(return_value=assignee),
        ),
        patch.object(TaskService, "create_task", AsyncMock(return_value="task-1")),
        patch.object(
            TaskService, "get_task", AsyncMock(side_effect=get_task_returns)
        ),
        patch.object(TaskService, "update_task", AsyncMock()),
        patch.object(TaskService, "require_task_id", AsyncMock(return_value="task-1")),
        patch.object(TaskService, "ensure_assignee_claimed", AsyncMock()),
        patch.object(InboxService, "send_message", AsyncMock()),
        patch.object(
            HandoffService, "create_handoff", AsyncMock(return_value="h-1")
        ),
        patch.object(ObligationLedger, "create", AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_dispatch_writer_gets_notice_once():
    with _enter(_dispatch_patches(_WRITER, [None, None])):
        svc = DispatchService()
        result = await svc.dispatch_task(
            PROJECT_ID, COORDINATOR_ID, ASSIGNEE_ID, "实现登录模块"
        )
        send_mock = InboxService.send_message
    assert result["success"] is True
    assert result["description"].count(MARKER) == 1
    assert MARKER in send_mock.await_args.args[2]


@pytest.mark.asyncio
async def test_dispatch_verify_task_gets_no_notice():
    verify_row = {"title": "VERIFY: 登录模块", "is_archived": False}
    with _enter(_dispatch_patches(_WRITER, [verify_row, verify_row])):
        svc = DispatchService()
        result = await svc.dispatch_task(
            PROJECT_ID, COORDINATOR_ID, ASSIGNEE_ID, "VERIFY: 登录模块"
        )
        send_mock = InboxService.send_message
    assert result["success"] is True
    assert MARKER not in result["description"]
    assert MARKER not in send_mock.await_args.args[2]


@pytest.mark.asyncio
async def test_dispatch_non_writer_gets_no_notice():
    with _enter(_dispatch_patches(_NON_WRITER, [None, None])):
        svc = DispatchService()
        result = await svc.dispatch_task(
            PROJECT_ID, COORDINATOR_ID, COORDINATOR_ID, "整理文档"
        )
        send_mock = InboxService.send_message
    assert result["success"] is True
    assert MARKER not in result["description"]
    assert MARKER not in send_mock.await_args.args[2]


# ── create path ──────────────────────────────────────────


def _create_patches(assignee: dict) -> list:
    return [
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.tools.helpers.resolve_agent_id",
            AsyncMock(return_value=ASSIGNEE_ID),
        ),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={"success": True, "path": "/tmp/wt"}),
        ),
        patch(
            "hiveweave.services.git_worktree.agent_gets_write_worktree",
            lambda a: (a or {}).get("permission_type") == "executor",
        ),
        patch(
            "hiveweave.services.org.OrgService.resolve_agent",
            AsyncMock(return_value=assignee),
        ),
        patch.object(
            TaskService, "find_structured_open_dup", AsyncMock(return_value=None)
        ),
        patch.object(
            TaskService, "find_similar_open_task", AsyncMock(return_value=None)
        ),
        patch.object(TaskService, "create_task", AsyncMock(return_value="task-1")),
        patch.object(
            TaskService, "get_task", AsyncMock(return_value={"status": "claimed"})
        ),
    ]


@pytest.mark.asyncio
async def test_create_task_writer_description_gets_notice():
    with _enter(_create_patches(_WRITER)):
        result = await create_task_tool(
            CreateTaskParams(
                title="实现登录模块", description="实现登录模块", assigneeId="A009"
            ),
            COORDINATOR_ID,
            "/tmp/ws",
            None,
        )
        create_mock = TaskService.create_task
    assert result.success
    desc = create_mock.await_args.kwargs["description"]
    assert desc.count(MARKER) == 1
    assert desc.startswith("实现登录模块")


@pytest.mark.asyncio
async def test_create_task_non_writer_keeps_description():
    with _enter(_create_patches(_NON_WRITER)):
        result = await create_task_tool(
            CreateTaskParams(
                title="整理文档", description="整理文档", assigneeId=COORDINATOR_ID
            ),
            COORDINATOR_ID,
            "/tmp/ws",
            None,
        )
        create_mock = TaskService.create_task
    assert result.success
    desc = create_mock.await_args.kwargs["description"]
    assert MARKER not in desc
    assert desc == "整理文档"


@pytest.mark.asyncio
async def test_create_task_without_assignee_keeps_description():
    with _enter(_create_patches(_WRITER)):
        result = await create_task_tool(
            CreateTaskParams(title="草稿任务", description="草稿任务"),
            COORDINATOR_ID,
            "/tmp/ws",
            None,
        )
        create_mock = TaskService.create_task
    assert result.success
    desc = create_mock.await_args.kwargs["description"]
    assert MARKER not in desc
