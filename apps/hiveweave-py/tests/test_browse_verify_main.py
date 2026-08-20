"""browse stays on the agent workspace; browse_main is explicit project root."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import hiveweave.tools.browse_tools as bt
from hiveweave.tools.browse_tools import BrowseParams, browse_main_tool, browse_tool


@pytest.mark.asyncio
async def test_browse_stays_on_worktree_even_for_verify(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "main" / ".hiveweave" / "worktrees" / "A093"
    main.mkdir()
    wt.mkdir(parents=True)
    seen: list[str] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        seen.append(workspace)
        return 0, "ok", ""

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
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
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert seen == [str(wt), str(wt)]
    text = (result.output or "") + (result.error or "")
    assert "browse_e2e REJECTED" in text
    assert "browse_main" in text
    assert result.success is False


@pytest.mark.asyncio
async def test_browse_main_runs_at_project_root(tmp_path):
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
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
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
        result = await browse_main_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is True
    assert seen == [str(main), str(main)]
    assert stamped == [str(main)]
    assert "cwd=project root" in (result.output or "")
    assert "attestation_id=att-1" in (result.output or "")


@pytest.mark.asyncio
async def test_browse_main_refuses_when_main_missing(tmp_path):
    wt = tmp_path / "worktrees" / "A093"
    wt.mkdir(parents=True)
    ran = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        ran.append(workspace)
        return 0, "ok", ""

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value=None),
        ),
    ):
        result = await browse_main_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is False
    assert ran == []
    text = (result.output or "") + (result.error or "")
    assert "cannot resolve project root" in text


@pytest.mark.asyncio
async def test_leaf_browse_stays_on_worktree():
    from hiveweave.tools.bash import resolve_project_main_cwd

    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        AsyncMock(return_value="D:/proj/main"),
    ):
        cwd, err = await resolve_project_main_cwd("p1")

    assert err == ""
    assert cwd.replace("\\", "/") == "D:/proj/main"


@pytest.mark.asyncio
async def test_browse_verify_fail_closed_when_task_missing(tmp_path):
    wt = tmp_path / "main" / ".hiveweave" / "worktrees" / "A093"
    wt.mkdir(parents=True)
    stamped: list[str] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        return 0, "ok", ""

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-should-not"

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            fake_create,
        ),
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is False
    assert stamped == []
    text = (result.output or "") + (result.error or "")
    assert "browse_e2e REJECTED" in text
    assert "cannot load bound task" in text


@pytest.mark.asyncio
async def test_browse_verify_fail_closed_when_task_is_none(tmp_path):
    wt = tmp_path / "main" / ".hiveweave" / "worktrees" / "A093"
    wt.mkdir(parents=True)
    stamped: list[str] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        return 0, "ok", ""

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-should-not"

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            fake_create,
        ),
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:5199"], task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is False
    assert stamped == []
    text = (result.output or "") + (result.error or "")
    assert "browse_e2e REJECTED" in text
    assert "bound task not found" in text
