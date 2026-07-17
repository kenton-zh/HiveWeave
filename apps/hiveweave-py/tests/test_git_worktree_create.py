"""Git worktree create must survive stale registrations (HiveWeave product bug)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import GitWorktreeService, WORKTREE_DIR


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        # git 输出是 UTF-8；中文 Windows 默认 GBK 会让 reader 线程
        # UnicodeDecodeError 崩溃（进程退出时挂死的根因）
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


@pytest.mark.asyncio
async def test_create_recovers_missing_but_registered_worktree(git_repo: Path) -> None:
    """Dir deleted while git still lists the worktree — create must prune + succeed."""
    gwt = GitWorktreeService()
    first = await gwt.create(str(git_repo), "A004", "卡槽系统工程师")
    assert first["success"] is True
    wt_path = Path(first["path"])
    assert wt_path.is_dir()

    # Simulate sandbox / manual cleanup: remove dir, leave git metadata stale
    shutil.rmtree(wt_path)
    assert not wt_path.exists()

    # Without prune-before-add this used to soft-fail and leave agents.workspace_path NULL
    second = await gwt.create(str(git_repo), "A004", "卡槽系统工程师")
    assert second["success"] is True, second
    assert Path(second["path"]).is_dir()
    assert (Path(second["path"]) / ".git").exists()


@pytest.mark.asyncio
async def test_create_reattaches_existing_branch_without_wiping_commits(
    git_repo: Path,
) -> None:
    """If branch survives but worktree dir is gone, reattach — do not -B wipe."""
    gwt = GitWorktreeService()
    first = await gwt.create(str(git_repo), "A005", "道具系统工程师")
    assert first["success"] is True
    wt = Path(first["path"])
    (wt / "feature.txt").write_text("keep-me\n", encoding="utf-8")
    _git(wt, "add", "feature.txt")
    _git(wt, "commit", "-m", "executor work")

    shutil.rmtree(wt)

    second = await gwt.create(str(git_repo), "A005", "道具系统工程师")
    assert second["success"] is True, second
    restored = Path(second["path"]) / "feature.txt"
    assert restored.exists()
    assert restored.read_text(encoding="utf-8") == "keep-me\n"


@pytest.mark.asyncio
async def test_create_idempotent_when_valid(git_repo: Path) -> None:
    gwt = GitWorktreeService()
    a = await gwt.create(str(git_repo), "A006", "UI界面工程师")
    b = await gwt.create(str(git_repo), "A006", "UI界面工程师")
    assert a["success"] and b["success"]
    assert a["path"] == b["path"]
    assert (Path(git_repo) / WORKTREE_DIR / "A006").is_dir()
