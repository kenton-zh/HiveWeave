"""GitWorktreeService.info() porcelain hints + git-error distinction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree.service import GitWorktreeService
from hiveweave.services.git_worktree.service_lifecycle import (
    _INFO_UNCOMMITTED_FILES_LIMIT,
    _porcelain_uncommitted_paths,
)


def test_porcelain_uncommitted_paths_restores_stripped_unstaged():
    """``_git`` strips stdout, so ``" M x"`` arrives as ``"M x"``."""
    raw = " M src/app.py\n?? scratch.txt\nA  docs/note.md"
    st = raw.strip()
    assert _porcelain_uncommitted_paths(st) == [
        "src/app.py",
        "scratch.txt",
        "docs/note.md",
    ]


def test_porcelain_uncommitted_paths_rename_and_cap():
    st = "R  old.py -> new.py\n" + "\n".join(
        f"?? f{i:02d}.txt" for i in range(_INFO_UNCOMMITTED_FILES_LIMIT + 3)
    )
    paths = _porcelain_uncommitted_paths(st)
    assert paths[0] == "new.py"
    assert len(paths) == _INFO_UNCOMMITTED_FILES_LIMIT


async def _info_with_git(tmp_path: Path, git_side_effect):
    wt = tmp_path / "wt"
    wt.mkdir()
    svc = GitWorktreeService()
    with (
        patch.object(
            svc,
            "_resolve_effective_worktree_path",
            AsyncMock(return_value=str(wt)),
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._git",
            side_effect=git_side_effect,
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._resolve_base_branch",
            AsyncMock(return_value=None),
        ),
        patch.object(svc, "_checkpoint_list", AsyncMock(return_value=[])),
    ):
        return await svc.info(str(tmp_path), "A004")


@pytest.mark.asyncio
async def test_info_lists_porcelain_files(tmp_path: Path):
    raw = " M src/app.py\n?? scratch.txt\nR  old.py -> new.py"

    async def fake_git(args, cwd, timeout=30.0):
        if args[:2] == ["rev-parse", "--short"]:
            return True, "abc1234"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return True, "hw/A004/work"
        if args == ["status", "--porcelain"]:
            return True, raw.strip()
        return True, ""

    result = await _info_with_git(tmp_path, fake_git)
    status = result["status"]
    assert status["has_uncommitted"] is True
    assert status["git_status_error"] is False
    assert status["uncommitted_files"] == [
        "src/app.py",
        "scratch.txt",
        "new.py",
    ]
    assert status["path"] == str(tmp_path / "wt")


@pytest.mark.asyncio
async def test_info_git_status_failure_sets_git_status_error(tmp_path: Path):
    async def fake_git(args, cwd, timeout=30.0):
        if args[:2] == ["rev-parse", "--short"]:
            return True, "abc1234"
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return True, "hw/A004/work"
        if args == ["status", "--porcelain"]:
            return False, "fatal: not a git repository"
        return True, ""

    result = await _info_with_git(tmp_path, fake_git)
    status = result["status"]
    assert status["has_uncommitted"] is True
    assert status["git_status_error"] is True
    assert status["uncommitted_files"] == []
