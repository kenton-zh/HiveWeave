"""Hard gates: effective_delivery, merge-on-close, quota reset parsing."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.llm.retry import parse_quota_reset
from hiveweave.services.task import MergeRequiredError, TaskService
from hiveweave.services.worktree_review import effective_delivery


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
def dual_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Main repo + linked worktree with one commit ahead."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "t@hiveweave.local")
    _git(main, "config", "user.name", "Test")
    (main / "README.md").write_text("base\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(main, "commit", "-m", "init")

    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-b", "hw/A001/work", str(wt))
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(wt, "add", "feature.py")
    _git(wt, "commit", "-m", "feature")
    return main, wt


@pytest.mark.asyncio
async def test_effective_delivery_dirty_counts(dual_repos: tuple[Path, Path]) -> None:
    main, wt = dual_repos
    delivery = await effective_delivery(str(main), str(wt))
    assert delivery["commits_ahead"] == 1
    assert delivery["dirty_count"] == 0
    assert delivery["has_effective_output"] is True

    (wt / "dirty.py").write_text("untracked\n", encoding="utf-8")
    dirty = await effective_delivery(str(main), str(wt))
    assert dirty["dirty_count"] >= 1
    assert dirty["untracked_count"] >= 1
    assert dirty["has_effective_output"] is True


@pytest.mark.asyncio
async def test_enforce_merge_on_close_raises_merge_required() -> None:
    ts = TaskService()
    task = {
        "id": "task-abc12345-0000-0000-0000-000000000001",
        "status": "approved",
        "assignee_id": "agent-1",
        "tags": [],
        "evidence": {},
    }
    delivery = {
        "commits_ahead": 2,
        "dirty_count": 0,
        "untracked_count": 0,
        "modified_count": 0,
        "has_effective_output": True,
    }

    with (
        patch.object(ts, "_task_skips_merge_gate", return_value=False),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new=AsyncMock(return_value="/proj/.hiveweave/worktrees/A001"),
        ),
        patch(
            "hiveweave.services.worktree_review.effective_delivery",
            new=AsyncMock(return_value=delivery),
        ),
        patch.object(ts, "_rollback_close_to_approved", new=AsyncMock()),
    ):
        with pytest.raises(MergeRequiredError):
            await ts._enforce_merge_on_close("proj", task)


def test_parse_quota_reset_daily_vs_soft() -> None:
    now = time.time()
    daily = parse_quota_reset({"X-RateLimit-Reset": str(int(now + 7200))})
    assert daily["is_daily_quota"] is True
    assert daily["retry_after_s"] is not None
    assert daily["retry_after_s"] > 600

    soft = parse_quota_reset({"Retry-After": "90"})
    assert soft["is_daily_quota"] is False
    assert soft["retry_after_s"] == pytest.approx(90.0, abs=1.0)

    empty = parse_quota_reset(None)
    assert empty["retry_after_s"] is None
    assert empty["is_daily_quota"] is False
