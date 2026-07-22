"""reconcile must not rmtree active executor worktrees (TEST3 A004)."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import (
    WORKTREE_DIR,
    GitWorktreeService,
    reconcile_worktrees,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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


def _seed_project_db(repo: Path, short_id: str = "A004") -> None:
    hw = repo / ".hiveweave"
    hw.mkdir(parents=True, exist_ok=True)
    db = hw / "data.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE agents ("
        "id TEXT PRIMARY KEY, short_id TEXT, status TEXT, "
        "permission_type TEXT, workspace_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT, is_archived INT)"
    )
    aid = "agent-a004"
    conn.execute(
        "INSERT INTO agents VALUES (?,?,?,?,?)",
        (aid, short_id, "active", "executor", str(hw / "worktrees" / short_id)),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?)",
        ("task-1", aid, "submitted", 0),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_reconcile_skips_active_executor_orphan_dir(git_repo: Path) -> None:
    """Disk dir not in git registry but agent still active → do not rmtree."""
    gwt = GitWorktreeService()
    created = await gwt.create(str(git_repo), "A004", "联调A工程师")
    assert created["success"] is True
    wt = Path(created["path"])
    (wt / "module_a.py").write_text("x=1\n", encoding="utf-8")
    _seed_project_db(git_repo, "A004")

    # Desync: prune registration but leave directory on disk
    _git(git_repo, "worktree", "remove", "--force", str(wt).replace("\\", "/"))
    # git worktree remove deletes the dir — recreate orphan dir content
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "module_a.py").write_text("keep-me\n", encoding="utf-8")
    (wt / ".git").write_text("gitdir: ../../.git\n", encoding="utf-8")

    with patch(
        "hiveweave.services.git_worktree._try_reattach_worktree",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "hiveweave.services.git_worktree._project_db_if_exists",
        new_callable=AsyncMock,
        return_value=None,
    ):
        report = await reconcile_worktrees(str(git_repo))

    assert wt.exists(), "active executor dir must survive reconcile"
    assert "A004" in (report.get("skipped_active_dirs") or [])
    assert report.get("removed_dirs", 0) == 0
    assert (wt / "module_a.py").read_text(encoding="utf-8") == "keep-me\n"


@pytest.mark.asyncio
async def test_reconcile_removes_true_orphan(git_repo: Path) -> None:
    """Dir with no agent / no open tasks may still be removed."""
    orphan = git_repo / WORKTREE_DIR / "ORPHAN99"
    orphan.mkdir(parents=True)
    (orphan / "junk.txt").write_text("gone\n", encoding="utf-8")
    _seed_project_db(git_repo, "A004")  # protects A004 only

    with patch(
        "hiveweave.services.git_worktree._project_db_if_exists",
        new_callable=AsyncMock,
        return_value=None,
    ):
        report = await reconcile_worktrees(str(git_repo))
    assert not orphan.exists()
    assert report.get("removed_dirs", 0) >= 1


@pytest.mark.asyncio
async def test_reconcile_removes_active_executor_when_all_closed(
    git_repo: Path,
) -> None:
    """Active executor with only closed tasks may have orphan dir cleaned."""
    gwt = GitWorktreeService()
    created = await gwt.create(str(git_repo), "A004", "联调A工程师")
    assert created["success"] is True
    wt = Path(created["path"])
    (wt / "module_a.py").write_text("x=1\n", encoding="utf-8")

    hw = git_repo / ".hiveweave"
    hw.mkdir(parents=True, exist_ok=True)
    db = hw / "data.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE agents ("
        "id TEXT PRIMARY KEY, short_id TEXT, status TEXT, "
        "permission_type TEXT, workspace_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT, is_archived INT)"
    )
    aid = "agent-a004"
    conn.execute(
        "INSERT INTO agents VALUES (?,?,?,?,?)",
        (aid, "A004", "active", "executor", str(wt)),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?)",
        ("task-1", aid, "closed", 0),
    )
    conn.commit()
    conn.close()

    # Desync: prune registration but leave directory
    _git(git_repo, "worktree", "remove", "--force", str(wt).replace("\\", "/"))
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "module_a.py").write_text("stale\n", encoding="utf-8")
    (wt / ".git").write_text("gitdir: ../../.git\n", encoding="utf-8")

    with patch(
        "hiveweave.services.git_worktree._try_reattach_worktree",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "hiveweave.services.git_worktree._project_db_if_exists",
        new_callable=AsyncMock,
        return_value=None,
    ):
        report = await reconcile_worktrees(str(git_repo))

    assert not wt.exists(), "closed-only active executor dir should be cleaned"
    assert report.get("removed_dirs", 0) >= 1
    assert "A004" not in (report.get("skipped_active_dirs") or [])


@pytest.mark.asyncio
async def test_merge_cleanup_not_blocked_by_approved_only(git_repo: Path) -> None:
    """approved-only tasks must not permanently skip post-merge delete."""
    from hiveweave.services import git_worktree as gwt_mod

    hw = git_repo / ".hiveweave"
    hw.mkdir(parents=True, exist_ok=True)
    db = hw / "data.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE agents ("
        "id TEXT PRIMARY KEY, short_id TEXT, status TEXT, "
        "permission_type TEXT, workspace_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT, is_archived INT)"
    )
    aid = "agent-a004"
    conn.execute(
        "INSERT INTO agents VALUES (?,?,?,?,?)",
        (aid, "A004", "active", "executor", str(hw / "worktrees" / "A004")),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?)",
        ("task-approved", aid, "approved", 0),
    )
    conn.commit()
    conn.close()

    # Avoid ensure_project_db migrating the minimal schema mid-test
    with patch.object(
        gwt_mod, "_project_db_if_exists", new_callable=AsyncMock, return_value=None
    ):
        assert await gwt_mod._assignee_has_open_tasks(str(git_repo), "A004") is False

        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?)",
            ("task-rework", aid, "rework", 0),
        )
        conn.commit()
        conn.close()
        assert await gwt_mod._assignee_has_open_tasks(str(git_repo), "A004") is True
