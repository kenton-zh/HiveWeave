"""Workspace vs project-root tools: model picks; platform does not rewrite cwd."""

from __future__ import annotations

import inspect
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.tools.bash import (
    BashParams,
    _is_project_root_tree,
    bash_main_tool,
    bash_tool,
)


def test_is_project_root_tree_accepts_apps_web_rejects_worktree(tmp_path):
    main = tmp_path / "proj"
    wt = main / ".hiveweave" / "worktrees" / "A093"
    web = main / "apps" / "web"
    main.mkdir()
    wt.mkdir(parents=True)
    web.mkdir(parents=True)

    assert _is_project_root_tree(str(main), str(main)) is True
    assert _is_project_root_tree(str(web), str(main)) is True
    assert _is_project_root_tree(str(wt), str(main)) is False


def test_get_workspace_path_source_does_not_rewrite_verify():
    from hiveweave.agents.agent import Agent

    src = inspect.getsource(Agent._get_workspace_path)
    assert "verify_only" not in src
    assert "bash_main" in src


def _verify_task():
    return {
        "id": "v-1",
        "title": "VERIFY: MAIN QA",
        "status": "running",
        "policy_id": "unit",
    }


def _bash_exec_patches(seen: list[str], execute_result: dict | None = None):
    async def fake_exec(**kwargs):
        seen.append(kwargs.get("workspace_path") or "")
        return execute_result or {
            "success": True,
            "output": "ok\nExit code: 0",
            "exit_code": 0,
        }

    return (
        patch("hiveweave.tools.bash.execute_bash", fake_exec),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value="p1")),
        patch(
            "hiveweave.services.process_registry.prepare_spawn_command",
            return_value=("npm test", {}, None, None),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=_verify_task()),
        ),
        patch(
            "hiveweave.services.task.TaskService.emit_task_event",
            AsyncMock(),
        ),
    )


@pytest.mark.asyncio
async def test_bash_stays_on_worktree_and_rejects_verify_attest(tmp_path):
    main = tmp_path / "main"
    wt = main / ".hiveweave" / "worktrees" / "A093"
    main.mkdir()
    wt.mkdir(parents=True)
    seen: list[str] = []
    stamped: list[str] = []

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-should-not"

    with ExitStack() as stack:
        for p in _bash_exec_patches(seen):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "hiveweave.services.worktree_review.project_main_workspace",
                AsyncMock(return_value=str(main)),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.create",
                fake_create,
            )
        )
        result = await bash_tool(
            BashParams(command="npm test", task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert seen == [str(wt)]
    assert stamped == []
    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "VERIFY ATTEST REJECTED" in text
    assert "bash_main" in text


@pytest.mark.asyncio
async def test_bash_main_runs_at_project_root(tmp_path):
    main = tmp_path / "main"
    wt = main / ".hiveweave" / "worktrees" / "A093"
    main.mkdir()
    wt.mkdir(parents=True)
    seen: list[str] = []
    stamped: list[str] = []

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-1"

    with ExitStack() as stack:
        for p in _bash_exec_patches(seen):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "hiveweave.services.worktree_review.project_main_workspace",
                AsyncMock(return_value=str(main)),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.create",
                fake_create,
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.hash_stdout",
                return_value="h",
            )
        )
        result = await bash_main_tool(
            BashParams(command="npm test", task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is True
    assert seen == [str(main)]
    assert stamped == [str(main)]
    assert "cwd=project root" in (result.output or "")


@pytest.mark.asyncio
async def test_agent_workspace_stays_on_worktree_when_verify_only(tmp_path):
    main = tmp_path / "proj"
    wt = main / ".hiveweave" / "worktrees" / "A001"
    main.mkdir()
    (main / ".git").mkdir()
    wt.mkdir(parents=True)
    (wt / ".git").mkdir()

    from hiveweave.agents.agent import Agent

    with patch.object(Agent, "_ensure_watcher_alive", lambda self: None):
        agent = Agent(
            "aid",
            "pid",
            {"name": "Vera", "role": "测试工程师", "short_id": "A001"},
        )

    org = MagicMock()
    org.get_agent = AsyncMock(
        return_value={
            "id": "aid",
            "short_id": "A001",
            "role": "测试工程师",
            "permission_type": "readwrite",
            "workspace_path": str(wt),
        }
    )
    org.update_agent = AsyncMock()

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(main)),
        ),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch(
            "hiveweave.services.git_worktree.agent_gets_write_worktree",
            return_value=True,
        ),
        patch(
            "hiveweave.services.git_worktree.heal_workspace_binding_from_disk",
            AsyncMock(return_value=str(wt)),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
    ):
        path = await agent._get_workspace_path()

    assert Path(path).resolve() == wt.resolve()
    assert Path(path).resolve() != main.resolve()


@pytest.mark.asyncio
async def test_game_run_case_rejected_is_tool_error(tmp_path):
    wt = tmp_path / "main" / ".hiveweave" / "worktrees" / "A093"
    wt.mkdir(parents=True)

    from hiveweave.tools.game_qa_tools import GameRunCaseParams, game_run_case_tool

    async def fake_js(*args, **kwargs):
        return 0, '{"hw": true, "cases": []}', ""

    with (
        patch(
            "hiveweave.tools.game_qa_tools.resolve_browse_bin",
            return_value="fake-ab",
        ),
        patch("hiveweave.tools.game_qa_tools._js", fake_js),
        patch(
            "hiveweave.tools.game_qa_tools.issue_browse_e2e_attestation",
            AsyncMock(
                return_value=(
                    "\n\n[browse_e2e REJECTED] VERIFY UI evidence must run "
                    "on MAIN. Use browse_main / game_run_case_main."
                )
            ),
        ),
    ):
        result = await game_run_case_tool(
            GameRunCaseParams(action="probe", task_id="v-1"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "browse_e2e REJECTED" in text


@pytest.mark.asyncio
async def test_unbound_verify_bash_does_not_stamp(tmp_path):
    main = tmp_path / "main"
    wt = main / ".hiveweave" / "worktrees" / "A093"
    main.mkdir()
    wt.mkdir(parents=True)
    seen: list[str] = []
    stamped: list[str] = []

    async def fake_create(*args, **kwargs):
        stamped.append(kwargs.get("workspace") or "")
        return "att-should-not"

    with ExitStack() as stack:
        for p in _bash_exec_patches(seen):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "hiveweave.tools.bash._resolve_test_attestation_task_id",
                AsyncMock(
                    return_value=(
                        None,
                        "\n\n[attestation_bind] test_run left UNBOUND: "
                        "multiple open VERIFY tasks assigned to you.",
                    )
                ),
            )
        )
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.create",
                fake_create,
            )
        )
        result = await bash_tool(
            BashParams(command="npm test"),
            agent_id="qa-1",
            workspace=str(wt),
        )

    assert seen == [str(wt)]
    assert stamped == []
    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "VERIFY ATTEST REJECTED" in text
    assert "bash_main" in text
