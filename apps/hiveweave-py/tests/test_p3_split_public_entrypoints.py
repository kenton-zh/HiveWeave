"""P3 package-split acceptance: public entrypoint happy / fail / recover paths.

Import smoke alone is not enough after the streamer / git_worktree / tasks
splits. These tests exercise the **package public APIs** agents still import:

- ``hiveweave.llm.streamer``
- ``hiveweave.services.git_worktree``
- ``hiveweave.services.task`` (shim → ``services.tasks``)

They intentionally stay small and do not replace the broader suites.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.llm.streamer import (
    CircuitBreakerOpenError,
    doom_loop_limit,
    parse_sse,
    round_made_progress,
    round_was_readonly_only,
    sse_to_chunks,
)
from hiveweave.services.git_worktree import GitWorktreeService, compute_branch_name
from hiveweave.services.task import TaskEventService, TaskService


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


# ── streamer package ─────────────────────────────────────────


class TestStreamerPublicEntrypoints:
    def test_sse_pipeline_happy_path_emits_text_chunk(self) -> None:
        buf = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        events, leftover = parse_sse(buf)
        assert leftover == ""
        assert len(events) == 1
        chunks = sse_to_chunks(events[0])
        assert any(c.get("type") == "text" and c.get("content") == "ok" for c in chunks)

    def test_doom_loop_limits_distinguish_readonly_and_side_effect(self) -> None:
        assert doom_loop_limit("get_tasks") == 6
        assert doom_loop_limit("read_file") == 15
        assert doom_loop_limit("bash") == 3
        assert doom_loop_limit("commit_turn") == 8

    def test_stall_progress_recovers_after_mutating_success(self) -> None:
        """Readonly-only rounds are no progress; a later mutating success recovers."""
        readonly = [{"id": "1", "name": "get_tasks"}]
        assert round_was_readonly_only(readonly) is True
        assert round_made_progress(readonly) is False

        failed_write = [{"id": "2", "name": "write_file"}]
        assert round_made_progress(failed_write, error_ids={"2"}) is False

        recovered = [{"id": "3", "name": "write_file"}]
        assert round_made_progress(recovered) is True
        assert round_was_readonly_only(recovered) is False

    def test_circuit_breaker_open_is_public_exception(self) -> None:
        with pytest.raises(CircuitBreakerOpenError):
            raise CircuitBreakerOpenError("open")


# ── git_worktree package ─────────────────────────────────────


class TestGitWorktreePublicEntrypoints:
    def test_branch_name_stable_for_task(self) -> None:
        assert compute_branch_name("A004", "deadbeef-1111") == "hw/A004/t-deadbeef"

    @pytest.mark.asyncio
    async def test_create_list_happy_path(self, git_repo: Path) -> None:
        gwt = GitWorktreeService()
        created = await gwt.create(str(git_repo), "B001", task_id="abcdef12-0000")
        assert created["success"] is True, created
        assert created["branch"] == "hw/B001/t-abcdef12"
        assert Path(created["path"]).is_dir()

        listed = await gwt.list(str(git_repo))
        assert listed["success"] is True
        paths = [row.get("path") for row in listed.get("entries", [])]
        assert any(
            Path(p).resolve() == Path(created["path"]).resolve()
            for p in paths
            if p
        )

    @pytest.mark.asyncio
    async def test_create_fails_when_ensure_git_repo_fails(self, tmp_path: Path) -> None:
        gwt = GitWorktreeService()
        missing = tmp_path / "no-such-workspace"
        with patch.object(
            GitWorktreeService,
            "ensure_git_repo",
            return_value={"success": False, "message": "Git is not installed or not on PATH."},
        ):
            out = await gwt.create(str(missing), "B002", task_id="11111111-2222")
        assert out["success"] is False
        assert "Git" in out.get("message", "")

    @pytest.mark.asyncio
    async def test_delete_unmerged_preserves_branch_then_discard_recovers(
        self, git_repo: Path
    ) -> None:
        gwt = GitWorktreeService()
        created = await gwt.create(str(git_repo), "B003", task_id="33333333-4444")
        assert created["success"] is True, created
        wt = Path(created["path"])
        (wt / "work.txt").write_text("unmerged\n", encoding="utf-8")
        _git(wt, "add", "work.txt")
        _git(wt, "commit", "-m", "unmerged work")

        soft = await gwt.delete(str(git_repo), "B003")
        assert soft.get("success") is True and soft.get("removed") is True, soft
        assert soft.get("preserved_branch") is not None, soft
        assert soft["preserved_branch"]["branch"] == created["branch"]

        hard = await gwt.delete(
            str(git_repo), "B003", discard=True, branch=created["branch"]
        )
        assert hard.get("success") is True, hard
        assert hard.get("preserved_branch") is None


# ── tasks package via task.py shim ───────────────────────────


PROJECT_ID = "test-p3-entry-project"
COORDINATOR_ID = "test-p3-coord"
EXECUTOR_ID = "test-p3-exec"


@pytest.fixture
async def task_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        agents = {
            COORDINATOR_ID: {
                "id": COORDINATOR_ID,
                "name": "Coord",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXECUTOR_ID: {
                "id": EXECUTOR_ID,
                "name": "Exec",
                "parent_id": COORDINATOR_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return agents.get(aid)

        from hiveweave.services import task as task_module

        task_module._migrated.discard(PROJECT_ID)
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id", fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORDINATOR_ID,
                "executor_id": EXECUTOR_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)


class TestTasksPublicEntrypoints:
    @pytest.mark.asyncio
    async def test_create_claim_start_happy_path(self, task_env) -> None:
        svc = TaskService()
        tid = await svc.create_task(
            project_id=task_env["project_id"],
            title="P3 entry",
            description="public API",
            creator_id=task_env["coordinator_id"],
        )
        await svc.claim_task(task_env["project_id"], tid, task_env["executor_id"])
        await svc.start_task(task_env["project_id"], tid)
        row = await svc.get_task(task_env["project_id"], tid)
        assert row is not None
        assert row["status"] == "running"

    @pytest.mark.asyncio
    async def test_illegal_transition_fails_closed(self, task_env) -> None:
        svc = TaskService()
        tid = await svc.create_task(
            project_id=task_env["project_id"],
            title="closed path",
            description="d",
            creator_id=task_env["coordinator_id"],
        )
        with pytest.raises(ValueError, match="Illegal transition"):
            await svc._transition(task_env["project_id"], tid, "approved")

    @pytest.mark.asyncio
    async def test_task_events_recover_empty_on_query_error(self) -> None:
        """Failure path stays soft: undelivered query errors return []."""
        from unittest.mock import AsyncMock

        with patch(
            "hiveweave.services.tasks.events._query",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            rows = await TaskEventService().get_undelivered("proj-p3-entry")
        assert rows == []
