"""dispatch_task 只读协调角色提醒测试 (Workstream B)。

行为契约:
- 派发给 permission_type='coordinator' 的 assignee：派发照常成功，
  agent 可见的返回文本追加中性提醒（只读协调角色不能改代码）。
- 派发给 executor / 查无此人 / 查询失败：不追加提醒，派发不受影响。
- 提醒只追加在 tool 返回文本上，不进入 inbox / 任务负载（description 原样传递）。

测试策略:
  - 参照 test_loop_hardening.py 的 @tool 直调模式：
    mock get_project_id / resolve_agent_id / DispatchService.dispatch_task /
    OrgService.get_agent，直接 await dispatch_task_tool(params, agent_id, workspace)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hiveweave.services.dispatch import DispatchService
from hiveweave.services.org import OrgService
from hiveweave.tools.task_tools import DispatchTaskParams, dispatch_task_tool

PROJECT_ID = "test-project"
COORDINATOR_ID = "boss-agent"
ASSIGNEE_ID = "assignee-agent"

_DISPATCH_OK: dict = {
    "success": True,
    "task_id": "task-123",
    "handoff_id": "handoff-1",
    "from_agent_id": COORDINATOR_ID,
    "to_agent_id": ASSIGNEE_ID,
    "description": "实现登录模块",
    "worktree_path": None,
    "worktree_short_id": None,
}


def _params() -> DispatchTaskParams:
    return DispatchTaskParams(target="A009", task="实现登录模块")


def _run_patches(assignee: dict | None = None, lookup_error: bool = False):
    """Assemble the standard patch stack for dispatch_task_tool."""
    if lookup_error:
        get_agent = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        get_agent = AsyncMock(return_value=assignee)
    return (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.tools.task_tools.resolve_agent_id",
            AsyncMock(return_value=ASSIGNEE_ID),
        ),
        patch.object(
            DispatchService, "dispatch_task",
            AsyncMock(return_value=dict(_DISPATCH_OK)),
        ),
        patch.object(OrgService, "get_agent", get_agent),
    )


class TestDispatchReadonlyReminder:
    """Workstream B — coordinator 派发提醒：追加但不阻断。"""

    async def test_dispatch_to_coordinator_appends_reminder(self):
        """派发给 coordinator：成功 + 返回文本包含只读提醒。"""
        p1, p2, p3, p4 = _run_patches(
            {"id": ASSIGNEE_ID, "permission_type": "coordinator"})
        with p1, p2, p3, p4:
            result = await dispatch_task_tool(
                _params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "Task dispatched to" in result.output
        assert "只读协调角色" in result.output
        assert "executor" in result.output

    async def test_dispatch_to_executor_has_no_reminder(self):
        """派发给 executor：成功 + 返回文本不含提醒。"""
        p1, p2, p3, p4 = _run_patches(
            {"id": ASSIGNEE_ID, "permission_type": "executor"})
        with p1, p2, p3, p4:
            result = await dispatch_task_tool(
                _params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "Task dispatched to" in result.output
        assert "只读协调角色" not in result.output

    async def test_dispatch_when_assignee_not_found_has_no_reminder(self):
        """查无此人（per-project DB 无记录）：不追加提醒，派发仍成功。"""
        p1, p2, p3, p4 = _run_patches(None)
        with p1, p2, p3, p4:
            result = await dispatch_task_tool(
                _params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "只读协调角色" not in result.output

    async def test_dispatch_when_lookup_fails_has_no_reminder(self):
        """角色查询抛异常：只记日志、按无提醒处理，绝不阻断派发。"""
        p1, p2, p3, p4 = _run_patches(lookup_error=True)
        with p1, p2, p3, p4:
            result = await dispatch_task_tool(
                _params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "只读协调角色" not in result.output

    async def test_dispatch_payload_description_unchanged(self):
        """提醒不进入任务负载：dispatch_task 收到的 description 原样传递。"""
        dispatch_mock = AsyncMock(return_value=dict(_DISPATCH_OK))
        with (
            patch(
                "hiveweave.tools.task_tools.get_project_id",
                AsyncMock(return_value=PROJECT_ID),
            ),
            patch(
                "hiveweave.tools.task_tools.resolve_agent_id",
                AsyncMock(return_value=ASSIGNEE_ID),
            ),
            patch.object(DispatchService, "dispatch_task", dispatch_mock),
            patch.object(
                OrgService, "get_agent",
                AsyncMock(return_value={
                    "id": ASSIGNEE_ID, "permission_type": "coordinator"}),
            ),
        ):
            result = await dispatch_task_tool(
                _params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert dispatch_mock.call_args.kwargs["description"] == "实现登录模块"
