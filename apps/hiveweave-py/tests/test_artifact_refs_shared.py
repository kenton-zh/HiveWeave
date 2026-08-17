"""dispatch artifact_refs: .hiveweave/shared is visible; other .hiveweave/ is not."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from hiveweave.services.dispatch import DispatchService
from hiveweave.services.org import OrgService
from hiveweave.services.task import TaskService
from hiveweave.tools.task_tools import DispatchTaskParams, dispatch_task_tool
from hiveweave.tools.tasks.dispatch import (
    _is_invisible_hiveweave_ref,
    _is_shared_artifact_ref,
)

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


def test_shared_ref_is_not_invisible():
    assert _is_shared_artifact_ref(".hiveweave/shared/file.md")
    assert _is_shared_artifact_ref(".hiveweave/shared/")
    assert _is_shared_artifact_ref(".hiveweave/shared")
    assert not _is_invisible_hiveweave_ref(".hiveweave/shared/file.md")
    assert not _is_invisible_hiveweave_ref(".hiveweave/shared/")


def test_private_hiveweave_refs_are_invisible():
    assert _is_invisible_hiveweave_ref(".hiveweave/tool_outputs/out.txt")
    assert _is_invisible_hiveweave_ref(".hiveweave/reports/a.md")
    assert _is_invisible_hiveweave_ref(".hiveweave/data.db")
    assert not _is_shared_artifact_ref(".hiveweave/tool_outputs/out.txt")


def _run_patches(tmp_main: Path):
    async def _ga(aid: str):
        if aid == ASSIGNEE_ID:
            return {
                "id": ASSIGNEE_ID,
                "permission_type": "executor",
                "parent_id": COORDINATOR_ID,
                "name": "Eng",
            }
        if aid == COORDINATOR_ID:
            return {
                "id": COORDINATOR_ID,
                "permission_type": "coordinator",
                "parent_id": None,
                "name": "Boss",
            }
        return None

    return (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.tools.helpers.resolve_agent_id",
            AsyncMock(return_value=ASSIGNEE_ID),
        ),
        patch.object(
            DispatchService,
            "dispatch_task",
            AsyncMock(return_value=dict(_DISPATCH_OK)),
        ),
        patch.object(OrgService, "get_agent", AsyncMock(side_effect=_ga)),
        patch.object(
            TaskService,
            "find_similar_open_task",
            AsyncMock(return_value=None),
        ),
        patch.object(
            TaskService,
            "find_structured_open_dup",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(tmp_main)),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            AsyncMock(),
        ),
    )


async def test_shared_artifact_ref_no_path_invisible(tmp_path: Path):
    shared = tmp_path / ".hiveweave" / "shared"
    shared.mkdir(parents=True)
    (shared / "file.md").write_text("draft\n", encoding="utf-8")
    params = DispatchTaskParams(
        target="A009",
        task="实现登录模块",
        submitGate="unit",
        artifact_refs=[".hiveweave/shared/file.md"],
    )
    patches = _run_patches(tmp_path)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
            patches[6], patches[7], patches[8]:
        result = await dispatch_task_tool(params, COORDINATOR_ID, "/tmp")
    assert result.success is True
    text = result.output or ""
    assert "PATH INVISIBLE" not in text
    assert "canonical contracts live in docs/" in text


async def test_tool_outputs_artifact_ref_still_warns(tmp_path: Path):
    out_dir = tmp_path / ".hiveweave" / "tool_outputs"
    out_dir.mkdir(parents=True)
    (out_dir / "out.txt").write_text("x\n", encoding="utf-8")
    params = DispatchTaskParams(
        target="A009",
        task="实现登录模块",
        submitGate="unit",
        artifact_refs=[".hiveweave/tool_outputs/out.txt"],
    )
    patches = _run_patches(tmp_path)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
            patches[6], patches[7], patches[8]:
        result = await dispatch_task_tool(params, COORDINATOR_ID, "/tmp")
    assert result.success is True
    text = result.output or ""
    assert "PATH INVISIBLE" in text
    assert "Move shared specs to docs/" in text
    assert ".hiveweave/tool_outputs/out.txt" in text
