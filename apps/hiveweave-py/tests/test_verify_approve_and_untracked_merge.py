"""review_worktree_gate + merge untracked classification — v3 卡点回归.

1. 纯验证任务 (VERIFY / 0 commits ahead / no_code_change) 不得因
   evidence.files_changed 为空被 approve 硬拦。
2. main 上 untracked 挡住 merge 时不得伪装成 merge conflict、不得 auto-rework。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import (
    GitWorktreeService,
    parse_untracked_overwrite,
    quarantine_untracked_on_target,
)
from hiveweave.services.worktree_review import (
    compare_worktree_to_main,
    review_worktree_gate,
)


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
    return (r.stdout or "").strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")
    _git(root, "branch", "-M", "main")


# ── parse untracked ──────────────────────────────────────


def test_parse_untracked_overwrite_extracts_paths():
    out = (
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "\tscripts/verify_ai.py\n"
        "\tdata/out.json\n"
        "Please move or remove them before you merge.\n"
        "Aborting\n"
    )
    assert parse_untracked_overwrite(out) == [
        "scripts/verify_ai.py",
        "data/out.json",
    ]


def test_parse_untracked_empty_on_real_conflict():
    out = "CONFLICT (content): Merge conflict in src/a.py\nAutomatic merge failed"
    assert parse_untracked_overwrite(out) == []


# ── compare / gate ───────────────────────────────────────


def test_compare_allows_empty_when_flagged():
    deny, meta = compare_worktree_to_main(
        main_ws="/m",
        worktree_ws="/w",
        files_changed=[],
        allow_empty_files=True,
    )
    assert deny is None
    assert meta.get("skipped") == "empty_files_changed_allowed"


def test_compare_still_blocks_empty_by_default():
    deny, _ = compare_worktree_to_main(
        main_ws="/m",
        worktree_ws="/w",
        files_changed=[],
        allow_empty_files=False,
    )
    assert deny is not None
    assert "files_changed is empty" in deny


@pytest.mark.asyncio
async def test_gate_skips_verify_task():
    task = {
        "id": "t1",
        "title": "VERIFY: Phase 1",
        "tags": ["verify"],
        "assignee_id": "a1",
        "evidence": {},
    }
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        AsyncMock(return_value="/proj"),
    ):
        deny, meta = await review_worktree_gate("p1", task, {})
    assert deny is None
    assert meta.get("skipped") == "verify_task"


@pytest.mark.asyncio
async def test_gate_skips_no_code_change_flag():
    task = {
        "id": "t1",
        "title": "运行 verify_ai.py 确认 PASS",
        "assignee_id": "a1",
    }
    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        AsyncMock(return_value="/proj"),
    ):
        deny, meta = await review_worktree_gate(
            "p1", task, {"no_code_change": True}
        )
    assert deny is None
    assert meta.get("skipped") == "no_code_change_flag"


@pytest.mark.asyncio
async def test_gate_allows_empty_files_when_zero_ahead(tmp_path: Path):
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    wt = tmp_path / "wt"
    # same commit worktree via git worktree
    _git(main, "worktree", "add", str(wt), "-b", "hw/A004/work")

    task = {
        "id": "t1",
        "title": "运行 verify_ai.py 确认 PASS",
        "assignee_id": "a1",
        "tags": [],
    }
    evidence = {"attestation_ids": ["att-1"]}

    with (
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value=str(main)),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=str(wt)),
        ),
    ):
        deny, meta = await review_worktree_gate("p1", task, evidence)

    assert deny is None, deny
    assert meta.get("commitsAhead") == 0
    assert meta.get("skipped") == "zero_commits_ahead"


@pytest.mark.asyncio
async def test_gate_blocks_empty_files_when_ahead(tmp_path: Path):
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "hw/A004/work")
    (wt / "new.py").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "new.py")
    _git(wt, "commit", "-m", "feat")

    task = {
        "id": "t1",
        "title": "实现功能",
        "assignee_id": "a1",
        "tags": [],
    }
    with (
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value=str(main)),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            AsyncMock(return_value=str(wt)),
        ),
    ):
        deny, meta = await review_worktree_gate("p1", task, {})

    assert deny is not None
    assert "files_changed is empty" in deny
    assert meta.get("commitsAhead") == 1


# ── merge untracked quarantine ───────────────────────────


@pytest.mark.asyncio
async def test_merge_quarantines_untracked_and_succeeds(tmp_path: Path):
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    # branch with a new tracked file
    _git(main, "checkout", "-b", "hw/A004/work")
    (main / "feature.py").write_text("feat\n", encoding="utf-8")
    _git(main, "add", "feature.py")
    _git(main, "commit", "-m", "feat")
    _git(main, "checkout", "main")

    # untracked file on main that would be overwritten
    (main / "feature.py").write_text("feat\n", encoding="utf-8")

    gwt = GitWorktreeService()
    # create a dummy worktree dir so delete doesn't blow up oddly —
    # merge() calls delete; for merge_by_branch with short_id A004
    result = await gwt.merge_by_branch(
        str(main), "hw/A004/work", target_branch="main"
    )

    assert result.get("success") is True, result
    # Dirty main is cleared either by pre-merge auto-checkpoint (commits
    # untracked onto target) or by merge-quarantine fallback. Both must
    # leave a successful merge with feature.py on main.
    assert (main / "feature.py").is_file()
    assert "feat" in (main / "feature.py").read_text(encoding="utf-8")
    q = main / ".hiveweave" / "merge-quarantine"
    tracked = bool(_git(main, "ls-files", "--", "feature.py"))
    assert q.is_dir() or tracked, (
        "expected merge-quarantine dir or tracked feature.py after checkpoint"
    )


@pytest.mark.asyncio
async def test_merge_same_commit_with_unrelated_untracked_ok(tmp_path: Path):
    """Same tip on main+branch: already up to date even if main has junk untracked."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    tip = _git(main, "rev-parse", "HEAD")
    _git(main, "branch", "hw/A004/work")
    assert _git(main, "rev-parse", "hw/A004/work") == tip

    (main / "junk.tmp").write_text("noise\n", encoding="utf-8")

    gwt = GitWorktreeService()
    result = await gwt.merge_by_branch(
        str(main), "hw/A004/work", target_branch="main"
    )
    assert result.get("success") is True, result
    assert result.get("already_up_to_date") is True


@pytest.mark.asyncio
async def test_quarantine_moves_only_untracked(tmp_path: Path):
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    (main / "orphan.txt").write_text("x\n", encoding="utf-8")
    moved = await quarantine_untracked_on_target(
        str(main), ["orphan.txt", "README.md"]
    )
    assert moved == ["orphan.txt"]
    assert not (main / "orphan.txt").exists()
    assert (main / "README.md").exists()
