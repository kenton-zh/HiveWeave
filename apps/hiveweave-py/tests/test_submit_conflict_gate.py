"""冲突左移门 — submit_task_tool 拒绝与 main 冲突的提交。

混合模式: 真实 git repo/worktree(冲突布局) + mock 外围(TaskService/
get_project_id/attestation 验真), 照抄 test_submit_auto_no_code_change
的 ExitStack 驱动模式。软策略 policy(coordinator_review)绕开 strict
attestation 门, 保证冲突门是被测的**唯一**新 blocker。
"""

from __future__ import annotations

import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_PROJ = "proj"
_AGENT = "agent-1"


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


def _git_version_ok() -> bool:
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True,
        ).stdout
    except OSError:
        return False
    m = re.search(r"(\d+)\.(\d+)", out or "")
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (2, 38)


pytestmark = pytest.mark.skipif(
    not _git_version_ok(), reason="git merge-tree --write-tree requires >= 2.38",
)


@pytest.fixture(autouse=True)
def _reset_supported_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程级 merge-tree 支持缓存必须在测试间复位(防跨测试污染)。"""
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.conflict_predict._merge_tree_supported",
        None,
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


async def _make_worktree(repo: Path, sid: str = "A005") -> Path:
    from hiveweave.services.git_worktree import GitWorktreeService

    gwt = GitWorktreeService()
    result = await gwt.create(str(repo), sid, "冲突门测试")
    assert result["success"] is True, result
    return Path(result["path"])


def _params(dry_run: bool = False):
    return SimpleNamespace(
        task_id="task-1",
        summary="conflict gate test",
        commit=None,
        files_changed=None,
        test_output=None,
        tests_passed=True,
        attestation_ids=["att-real-1"],
        dry_run=dry_run,
    )


def _task():
    return {
        "id": "task-1",
        "status": "running",
        "tags": [],
        "policy_id": "coordinator_review",  # 软策略: strict attestation 门跳过
        "title": "platform feature implementation",
        "description": "",
        "assignee_id": _AGENT,
        "creator_id": None,
    }


async def _run(params, worktree: str | None):
    """驱动 submit_task_tool(软策略 + 指定 worktree), 返回 (result, submitted)。"""
    submitted: dict = {}

    async def _capture(project_id, task_id, evidence):
        submitted["evidence"] = evidence

    stack = ExitStack()
    TS = stack.enter_context(patch("hiveweave.services.task.TaskService"))
    stack.enter_context(
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value=_PROJ,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=worktree,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new_callable=AsyncMock,
            return_value=(True, ""),
        )
    )
    with stack:
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=_task())
        ts.submit_task = AsyncMock(side_effect=_capture)

        from hiveweave.tools.tasks.submit import submit_task_tool

        result = await submit_task_tool(params, _AGENT, "/tmp/ws")
    return result, submitted


async def _make_conflict(repo: Path, wt: Path) -> None:
    """双方改同一文件 — 分叉且 merge-tree 必报冲突。"""
    (wt / "file.txt").write_text("branch version\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch change")
    (repo / "file.txt").write_text("main version\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "main change")


@pytest.mark.asyncio
async def test_submit_rejected_on_conflict(git_repo: Path) -> None:
    """冲突布局 — submit 被拒, submit_task 服务层未被调用(硬拦在 transition 前)。"""
    wt = await _make_worktree(git_repo)
    await _make_conflict(git_repo, wt)

    result, submitted = await _run(_params(), str(wt))

    assert result.success is False
    assert "合并冲突" in (result.error or "")
    assert "file.txt" in (result.error or "")
    assert "rebase" in (result.error or "").lower()
    assert submitted.get("evidence") is None  # 服务层 submit 未被调用


@pytest.mark.asyncio
async def test_submit_passes_when_clean(git_repo: Path) -> None:
    """分叉但不同文件 — 无冲突, 正常提交。"""
    wt = await _make_worktree(git_repo)
    (wt / "branch-only.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "branch-only.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "main-only.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "main-only.txt")
    _git(git_repo, "commit", "-m", "main side")

    result, submitted = await _run(_params(), str(wt))

    assert result.success is True, result
    assert submitted.get("evidence") is not None


@pytest.mark.asyncio
async def test_dry_run_lists_conflict_blocker(git_repo: Path) -> None:
    """dry_run 模式 — missing 列表含 merge_conflict_with_main。"""
    wt = await _make_worktree(git_repo)
    await _make_conflict(git_repo, wt)

    result, submitted = await _run(_params(dry_run=True), str(wt))

    assert result.success is True  # dry-run 恒 ok
    missing = result.extra.get("missing") or []
    assert any(m.get("code") == "merge_conflict_with_main" for m in missing)
    assert submitted.get("evidence") is None


@pytest.mark.asyncio
async def test_gate_skips_without_worktree(git_repo: Path) -> None:
    """无 worktree(CEO/HR/纯文档) — 门自然跳过, 不产生该 blocker。"""
    result, submitted = await _run(_params(), None)

    assert result.success is True, result
    assert submitted.get("evidence") is not None


@pytest.mark.asyncio
async def test_gate_fail_open_when_predict_raises(git_repo: Path) -> None:
    """predict 抛异常 — 门 fail-open, submit 正常(基础设施失败不误拦)。"""
    wt = await _make_worktree(git_repo)
    (wt / "wip.txt").write_text("w\n", encoding="utf-8")
    _git(wt, "add", "wip.txt")
    _git(wt, "commit", "-m", "wip")

    with patch(
        "hiveweave.services.git_worktree.conflict_predict"
        ".predict_merge_conflicts",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result, submitted = await _run(_params(), str(wt))

    assert result.success is True, result
    assert submitted.get("evidence") is not None
