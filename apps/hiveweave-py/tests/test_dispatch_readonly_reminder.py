"""dispatch_task hard gates: assignee capability + direct-report span.

行为契约（2026-07 CEO 抽离 + 中层 builder 改造）:
- 派发给 builder coordinator（family=coordinator，有 SOURCE_WRITE）：放行
- 派发给 CEO（family=ceo，无 SOURCE_WRITE）：硬拒绝，不调用 DispatchService
- 派发给非直属下属：硬拒绝跨级
- 派发给直属 executor：成功
- 角色查询失败：fail-open（不因 infra 砖死派活）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hiveweave.services.dispatch import DispatchService
from hiveweave.services.org import OrgService
from hiveweave.services.task import TaskService
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


def _get_agent_side_effect(assignee: dict | None, lookup_error: bool = False):
    if lookup_error:
        return AsyncMock(side_effect=RuntimeError("db down"))

    async def _ga(aid: str):
        if aid == ASSIGNEE_ID:
            return assignee
        if aid == COORDINATOR_ID:
            return {
                "id": COORDINATOR_ID,
                "permission_type": "coordinator",
                "parent_id": None,
                "name": "Boss",
            }
        return None

    return AsyncMock(side_effect=_ga)


def _run_patches(assignee: dict | None = None, lookup_error: bool = False):
    get_agent = _get_agent_side_effect(assignee, lookup_error=lookup_error)
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
            DispatchService,
            "dispatch_task",
            AsyncMock(return_value=dict(_DISPATCH_OK)),
        ),
        patch.object(OrgService, "get_agent", get_agent),
        patch.object(
            TaskService,
            "find_similar_open_task",
            AsyncMock(return_value=None),
        ),
    )


class TestDispatchHardGates:
    """Coordinator assignee + span hard gates."""

    async def test_dispatch_to_builder_coordinator_allowed(self):
        """派发给 builder coordinator（有 SOURCE_WRITE 的中层）：放行。"""
        p = _run_patches(
            {
                "id": ASSIGNEE_ID,
                "permission_type": "coordinator",
                "role": "前端架构师",
                "parent_id": COORDINATOR_ID,
                "name": "Lead",
            }
        )
        with p[0], p[1], p[2], p[3], p[4]:
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "Task dispatched to" in result.output

    async def test_dispatch_to_ceo_hard_blocked(self):
        """派发给 CEO（family=ceo，无 SOURCE_WRITE）：硬拒绝，不调用底层 dispatch。"""
        dispatch_mock = AsyncMock(return_value=dict(_DISPATCH_OK))
        patches = _run_patches(
            {
                "id": ASSIGNEE_ID,
                "permission_type": "coordinator",
                "role": "ceo",
                "parent_id": COORDINATOR_ID,
                "name": "归零",
            }
        )
        # replace dispatch mock
        patches = (
            patches[0],
            patches[1],
            patch.object(DispatchService, "dispatch_task", dispatch_mock),
            patches[3],
            patches[4],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is False
        text = (result.output or "") + (getattr(result, "error", None) or "")
        assert "拒绝派活" in text or "CEO" in text
        dispatch_mock.assert_not_awaited()

    async def test_dispatch_to_executor_direct_report_ok(self):
        """直属 executor：成功。"""
        p = _run_patches(
            {
                "id": ASSIGNEE_ID,
                "permission_type": "executor",
                "parent_id": COORDINATOR_ID,
                "name": "Eng",
            }
        )
        with p[0], p[1], p[2], p[3], p[4]:
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
        assert "Task dispatched to" in result.output

    async def test_dispatch_cross_level_blocked(self):
        """非直属：跨级拒绝。"""
        dispatch_mock = AsyncMock(return_value=dict(_DISPATCH_OK))
        get_agent = _get_agent_side_effect(
            {
                "id": ASSIGNEE_ID,
                "permission_type": "executor",
                "parent_id": "other-lead",
                "name": "Leaf",
            }
        )
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
            patch.object(OrgService, "get_agent", get_agent),
            patch.object(
                TaskService, "find_similar_open_task", AsyncMock(return_value=None)
            ),
        ):
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is False
        text = (result.output or "") + (getattr(result, "error", None) or "")
        assert "跨级" in text
        dispatch_mock.assert_not_awaited()

    async def test_dispatch_dedup_similar_open_task(self):
        """相似未完成任务 → 拒绝新建，提示复用 taskId。"""
        with (
            patch(
                "hiveweave.tools.task_tools.get_project_id",
                AsyncMock(return_value=PROJECT_ID),
            ),
            patch(
                "hiveweave.tools.task_tools.resolve_agent_id",
                AsyncMock(return_value=ASSIGNEE_ID),
            ),
            patch.object(DispatchService, "dispatch_task", AsyncMock()),
            patch.object(
                OrgService,
                "get_agent",
                _get_agent_side_effect(
                    {
                        "id": ASSIGNEE_ID,
                        "permission_type": "executor",
                        "parent_id": COORDINATOR_ID,
                        "name": "Eng",
                    }
                ),
            ),
            patch.object(
                TaskService,
                "find_similar_open_task",
                AsyncMock(
                    return_value={
                        "id": "dup-task",
                        "status": "running",
                        "title": "实现登录模块",
                    }
                ),
            ),
        ):
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is False
        text = (result.output or "") + (getattr(result, "error", None) or "")
        assert "dup-task" in text
        assert "相似" in text

    async def test_lookup_failure_fail_open_allows_dispatch(self):
        """角色查询抛异常：fail-open，不阻断。"""
        p = _run_patches(lookup_error=True)
        with p[0], p[1], p[2], p[3], p[4]:
            result = await dispatch_task_tool(_params(), COORDINATOR_ID, "/tmp")
        assert result.success is True
