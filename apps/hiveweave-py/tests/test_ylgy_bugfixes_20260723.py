"""Regression tests for TEST_YLGY agent-behavior diagnosis (2026-07-23).

Covers program bugs BUG-1/3/9 (unit-level, no live LLM).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.agent_router import agent_router
from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.org import OrgService
from hiveweave.services.task import TaskService
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    get_pending_turn_result,
)
from hiveweave.services.worktree_review import compare_worktree_to_main
from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool


# ── BUG-9: approve when content already on main ─────────────


def test_compare_worktree_allows_already_on_main(tmp_path: Path) -> None:
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "docs").mkdir()
    (wt / "docs").mkdir()
    (main / "docs" / "ui-spec.md").write_text("spec-v1\n", encoding="utf-8")
    (wt / "docs" / "ui-spec.md").write_text("spec-v1\n", encoding="utf-8")

    deny, meta = compare_worktree_to_main(
        main_ws=str(main),
        worktree_ws=str(wt),
        files_changed=["docs/ui-spec.md"],
    )
    assert deny is None
    assert meta.get("alreadyOnMain") is True
    assert meta.get("identicalToMain") == ["docs/ui-spec.md"]


def test_compare_worktree_still_blocks_partial_identical(tmp_path: Path) -> None:
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    (main / "a.txt").write_text("same\n", encoding="utf-8")
    (wt / "a.txt").write_text("same\n", encoding="utf-8")
    (main / "b.txt").write_text("old\n", encoding="utf-8")
    (wt / "b.txt").write_text("new\n", encoding="utf-8")

    deny, meta = compare_worktree_to_main(
        main_ws=str(main),
        worktree_ws=str(wt),
        files_changed=["a.txt", "b.txt"],
    )
    assert deny is not None
    assert "identical to MAIN" in deny
    assert "a.txt" in meta["identicalToMain"]
    assert "b.txt" in meta["divergedFiles"]


# ── BUG-3: commit_turn end_turn + structured gates ──────────


@pytest.mark.asyncio
async def test_commit_turn_done_slice_sets_end_turn_and_empty_gates() -> None:
    agent_id = "test-agent-commit-end-turn"
    clear_pending_turn_result(agent_id)
    try:
        params = CommitTurnParams(
            phase="done_slice",
            summary="slice complete",
        )
        result = await commit_turn_tool(params, agent_id, workspace=".")
        assert result.success is True
        assert result.extra.get("end_turn") is True
        assert result.extra.get("gates") == []
        assert "STOP:" in result.output
        assert "gates: []" in result.output
        assert get_pending_turn_result(agent_id) is not None
    finally:
        clear_pending_turn_result(agent_id)


@pytest.mark.asyncio
async def test_commit_turn_in_progress_does_not_end_turn() -> None:
    agent_id = "test-agent-commit-in-progress"
    clear_pending_turn_result(agent_id)
    try:
        params = CommitTurnParams(
            phase="in_progress",
            summary="still working",
        )
        result = await commit_turn_tool(params, agent_id, workspace=".")
        assert result.success is True
        assert result.extra.get("end_turn") is False
        assert "Will continue working" in result.output
    finally:
        clear_pending_turn_result(agent_id)


@pytest.mark.asyncio
async def test_commit_turn_duplicate_also_ends_turn() -> None:
    agent_id = "test-agent-commit-dup"
    clear_pending_turn_result(agent_id)
    try:
        params = CommitTurnParams(
            phase="waiting",
            summary="parked",
            waiting_on=[{"kind": "timer", "ref": "t1"}],
        )
        first = await commit_turn_tool(params, agent_id, workspace=".")
        assert first.success is True
        # Soft-pass or hard accept both set end_turn; duplicate path also does.
        second = await commit_turn_tool(params, agent_id, workspace=".")
        assert second.success is True
        assert second.extra.get("end_turn") is True
        assert second.extra.get("duplicate") is True or "ALREADY" in second.output
    finally:
        clear_pending_turn_result(agent_id)


# ── BUG-1 residual: reassign_keep_status self-review deadlock ─


async def _seed_org(
    pid: str,
    *,
    ceo_id: str,
    coord_id: str,
    exec_id: str,
) -> tuple[dict, dict, dict]:
    org = OrgService()
    ceo = await org.create_agent(
        {
            "id": ceo_id,
            "project_id": pid,
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "status": "active",
            "parent_id": None,
        },
        bootstrap=True,
    )
    coord = await org.create_agent(
        {
            "id": coord_id,
            "project_id": pid,
            "name": "知远",
            "role": "frontend-architect",
            "permission_type": "coordinator",
            "status": "active",
            "parent_id": ceo["id"],
        },
        bootstrap=True,
    )
    exec_ = await org.create_agent(
        {
            "id": exec_id,
            "project_id": pid,
            "name": "墨白",
            "role": "签到排行榜工程师",
            "permission_type": "executor",
            "status": "active",
            "parent_id": coord["id"],
        },
        bootstrap=True,
    )
    return ceo, coord, exec_


async def _dismiss_quiet(org: OrgService, pid: str, agent_id: str) -> dict:
    with (
        patch.object(
            GitWorktreeService,
            "quarantine_for_review",
            new=AsyncMock(return_value={"quarantined": True}),
        ),
        patch.object(
            GitWorktreeService,
            "delete",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "hiveweave.services.roster.RosterService.update",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.game_time.GameTimeService.cancel_alarms_for_agent",
            new=AsyncMock(return_value=0),
        ),
    ):
        return await org.dismiss_agent(pid, agent_id)


@pytest.mark.asyncio
async def test_dismiss_submitted_escalates_reviewer_past_parent(
    tmp_path: Path,
) -> None:
    """Common accident: parent already pinned as reviewer at submit.

    Old BUG-1 fix set assignee=parent while leaving reviewer=parent →
    self-review gate deadlock (11ad19cd scene). Must escalate reviewer
    to grandparent so assignee != reviewer.
    """
    ws = str(tmp_path.resolve())
    pid = "ylgy-bug1-escalate"
    ids = ("ceo-ylgy-e", "coord-ylgy-e", "exec-ylgy-e")

    async def fake_ws(p: str):
        return ws if p == pid else None

    task_module._migrated.discard(pid)
    try:
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            org = OrgService()
            ceo, coord, exec_ = await _seed_org(
                pid, ceo_id=ids[0], coord_id=ids[1], exec_id=ids[2]
            )
            ts = TaskService()
            tid = await ts.create_task(
                pid,
                "UI slice",
                "d",
                creator_id=coord["id"],
                assignee_id=exec_["id"],
            )
            await ts.start_task(pid, tid)
            await ts.submit_task(
                pid,
                tid,
                evidence={"tests_passed": True, "test_output": "ok"},
            )
            before = await ts.get_task(pid, tid)
            assert before["status"] == "submitted"
            assert before["reviewer_id"] == coord["id"]

            result = await _dismiss_quiet(org, pid, exec_["id"])
            assert result["success"] is True

            after = await ts.get_task(pid, tid)
            assert after["status"] == "submitted"
            assert after["assignee_id"] == coord["id"]
            assert after["reviewer_id"] == ceo["id"]
            assert after["assignee_id"] != after["reviewer_id"]
    finally:
        agent_router.clear_project(pid)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_dismiss_submitted_keeps_external_reviewer(
    tmp_path: Path,
) -> None:
    """If reviewer is already above parent, do not rewrite them."""
    ws = str(tmp_path.resolve())
    pid = "ylgy-bug1-keep-rev"
    ids = ("ceo-ylgy-k", "coord-ylgy-k", "exec-ylgy-k")

    async def fake_ws(p: str):
        return ws if p == pid else None

    task_module._migrated.discard(pid)
    try:
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            org = OrgService()
            ceo, coord, exec_ = await _seed_org(
                pid, ceo_id=ids[0], coord_id=ids[1], exec_id=ids[2]
            )
            ts = TaskService()
            tid = await ts.create_task(
                pid,
                "API slice",
                "d",
                creator_id=coord["id"],
                assignee_id=exec_["id"],
            )
            await ts.start_task(pid, tid)
            await ts.submit_task(
                pid,
                tid,
                evidence={"tests_passed": True, "test_output": "ok"},
            )
            # Pin reviewer to CEO (above parent) before dismiss
            conn = await project_db.get_project_db_by_project_id(pid)
            await conn.execute(
                "UPDATE tasks SET reviewer_id = ? WHERE id = ?",
                [ceo["id"], tid],
            )
            await conn.commit()

            result = await _dismiss_quiet(org, pid, exec_["id"])
            assert result["success"] is True

            after = await ts.get_task(pid, tid)
            assert after["status"] == "submitted"
            assert after["assignee_id"] == coord["id"]
            assert after["reviewer_id"] == ceo["id"]
            assert after["assignee_id"] != after["reviewer_id"]
    finally:
        agent_router.clear_project(pid)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_dismiss_submitted_no_grandparent_keeps_assignee(
    tmp_path: Path,
) -> None:
    """Root parent already reviewer: keep dismissed as assignee (no deadlock)."""
    ws = str(tmp_path.resolve())
    pid = "ylgy-bug1-root"
    ceo_id = "ceo-ylgy-r"
    exec_id = "exec-ylgy-r"

    async def fake_ws(p: str):
        return ws if p == pid else None

    task_module._migrated.discard(pid)
    try:
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            org = OrgService()
            ceo = await org.create_agent(
                {
                    "id": ceo_id,
                    "project_id": pid,
                    "name": "归零",
                    "role": "ceo",
                    "permission_type": "coordinator",
                    "status": "active",
                    "parent_id": None,
                },
                bootstrap=True,
            )
            # bootstrap skips "executor under CEO" invariant
            exec_ = await org.create_agent(
                {
                    "id": exec_id,
                    "project_id": pid,
                    "name": "墨白",
                    "role": "签到排行榜工程师",
                    "permission_type": "executor",
                    "status": "active",
                    "parent_id": ceo["id"],
                },
                bootstrap=True,
            )
            ts = TaskService()
            tid = await ts.create_task(
                pid,
                "Root slice",
                "d",
                creator_id=ceo["id"],
                assignee_id=exec_["id"],
            )
            await ts.start_task(pid, tid)
            await ts.submit_task(
                pid,
                tid,
                evidence={"tests_passed": True, "test_output": "ok"},
            )
            before = await ts.get_task(pid, tid)
            assert before["reviewer_id"] == ceo["id"]

            result = await _dismiss_quiet(org, pid, exec_["id"])
            assert result["success"] is True

            after = await ts.get_task(pid, tid)
            assert after["status"] == "submitted"
            # No grandparent to escalate to → keep dismissed as assignee
            assert after["assignee_id"] == exec_["id"]
            assert after["reviewer_id"] == ceo["id"]
            assert after["assignee_id"] != after["reviewer_id"]
    finally:
        agent_router.clear_project(pid)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
