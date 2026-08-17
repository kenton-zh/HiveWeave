"""VERIFY / ui_browser_e2e browse is forced onto MAIN (not rejected after the fact)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import hiveweave.tools.browse_tools as bt
from hiveweave.tools.browse_tools import BrowseParams, browse_tool


@pytest.mark.asyncio
async def test_browse_verify_forces_main_cwd(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "main" / ".hiveweave" / "worktrees" / "A093"
    main.mkdir()
    wt.mkdir(parents=True)
    seen: list[str] = []
    stamped: list[str] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        seen.append(workspace)
        return 0, "ok", ""

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-1"

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("v-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(
                return_value={
                    "id": "v-1",
                    "title": "VERIFY: MAIN QA",
                    "status": "running",
                    "policy_id": "ui_browser_e2e",
                }
            ),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value=str(main)),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            fake_create,
        ),
        patch(
            "hiveweave.services.attestation.hash_stdout",
            return_value="h",
        ),
        patch.object(bt, "_maybe_git_commit", AsyncMock(return_value="abc")),
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is True
    assert seen == [str(main)]
    assert stamped == [str(main)]
    assert "REJECTED" not in (result.output or "")
    assert "forced cwd=main" in (result.output or "")
    assert "attestation_id=att-1" in (result.output or "")


@pytest.mark.asyncio
async def test_browse_verify_refuses_when_main_missing(tmp_path):
    wt = tmp_path / "worktrees" / "A093"
    wt.mkdir(parents=True)
    ran = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        ran.append(workspace)
        return 0, "ok", ""

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("v-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(
                return_value={
                    "id": "v-1",
                    "title": "VERIFY: MAIN QA",
                    "status": "running",
                }
            ),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value=None),
        ),
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is False
    assert ran == []
    text = (result.output or "") + (result.error or "")
    assert "cannot resolve project main workspace" in text


@pytest.mark.asyncio
async def test_resolve_ui_policy_forces_main():
    from hiveweave.tools.bash import _resolve_verify_main_workspace

    task = {
        "id": "qa-e2e",
        "title": "实现质量验证面",
        "status": "running",
        "policy_id": "ui_browser_e2e",
        "assignee_id": "qa-1",
    }
    with (
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("qa-e2e", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=task),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=False,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
    ):
        exec_ws, note, tid = await _resolve_verify_main_workspace(
            "p1",
            "qa-1",
            "qa-e2e",
            "D:/proj/.hiveweave/worktrees/A093",
            "browse goto",
            require_test_command=False,
            include_ui_policy=True,
        )

    assert tid == "qa-e2e"
    assert exec_ws.replace("\\", "/") == "D:/proj/main"
    assert "forced cwd=main" in note


@pytest.mark.asyncio
async def test_resolve_unit_leaf_stays_on_worktree():
    from hiveweave.tools.bash import _resolve_verify_main_workspace

    task = {
        "id": "leaf-1",
        "title": "实现互动学习体验面",
        "status": "running",
        "policy_id": "generic_tests",
        "assignee_id": "exec-1",
    }
    wt = "D:/proj/.hiveweave/worktrees/A092"
    with (
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("leaf-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=task),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=False,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
    ):
        exec_ws, note, tid = await _resolve_verify_main_workspace(
            "p1",
            "exec-1",
            "leaf-1",
            wt,
            "browse goto",
            require_test_command=False,
            include_ui_policy=True,
        )

    assert tid == "leaf-1"
    assert exec_ws == wt
    assert "forced cwd=main" not in note
