"""冲突检测左移 — predict_merge_conflicts 预测核心 + checkpoint 预警回归。

布局构造原则: 真实 git repo + GitWorktreeService.create 建 worktree,
双方分叉后用 merge-tree 只读预演断言状态/behind/ahead/conflicts。
Git < 2.38 环境整文件 skip(--write-tree 不可用时产品走降级路径,
降级路径用 monkeypatch 单测覆盖)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree.conflict_predict import (
    predict_merge_conflicts,
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


def _git_version_ok() -> bool:
    """merge-tree --write-tree 需要 Git >= 2.38。"""
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


@pytest.fixture(autouse=True)
def _reset_supported_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程级 merge-tree 支持缓存必须在测试间复位(防跨测试污染)。"""
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.conflict_predict._merge_tree_supported",
        None,
    )


async def _make_worktree(repo: Path, sid: str = "A005") -> Path:
    gwt = GitWorktreeService()
    result = await gwt.create(str(repo), sid, "冲突预演测试")
    assert result["success"] is True, result
    return Path(result["path"])


# ── predict_merge_conflicts ────────────────────────────────


@pytest.mark.asyncio
async def test_predict_up_to_date_when_no_divergence(git_repo: Path) -> None:
    """刚建 worktree 无任何提交 — up_to_date(廉价剪枝, 不跑 merge-tree)。"""
    wt = await _make_worktree(git_repo)
    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "up_to_date"
    assert pred.behind == 0 and pred.ahead == 0
    assert pred.conflicts == []


@pytest.mark.asyncio
async def test_predict_conflict_detected(git_repo: Path) -> None:
    """双方改同一文件 — conflict 且解析出文件清单。"""
    wt = await _make_worktree(git_repo)
    # branch 侧提交
    (wt / "file.txt").write_text("branch version\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch change")
    # main 侧提交(同文件不同内容)
    (git_repo / "file.txt").write_text("main version\n", encoding="utf-8")
    _git(git_repo, "add", "file.txt")
    _git(git_repo, "commit", "-m", "main change")

    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "conflict"
    assert pred.behind == 1 and pred.ahead == 1
    assert pred.conflicts == ["file.txt"]


@pytest.mark.asyncio
async def test_predict_clean_when_changes_touch_different_files(
    git_repo: Path,
) -> None:
    """双方分叉但改不同文件 — clean。"""
    wt = await _make_worktree(git_repo)
    (wt / "branch-only.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "branch-only.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "main-only.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "main-only.txt")
    _git(git_repo, "commit", "-m", "main side")

    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "clean"
    assert pred.behind == 1 and pred.ahead == 1


@pytest.mark.asyncio
async def test_predict_noop_when_branch_not_ahead(git_repo: Path) -> None:
    """仅 main 前移(分支无新提交) — 不可能冲突, up_to_date。"""
    wt = await _make_worktree(git_repo)
    (git_repo / "main-only.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "main-only.txt")
    _git(git_repo, "commit", "-m", "main side")

    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "up_to_date"
    assert pred.behind == 1 and pred.ahead == 0


@pytest.mark.asyncio
async def test_predict_unknown_when_degraded(git_repo: Path) -> None:
    """merge-tree 不可用(进程级缓存 False) — unknown + degraded。"""
    wt = await _make_worktree(git_repo)
    (wt / "file.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "other.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "other.txt")
    _git(git_repo, "commit", "-m", "main side")

    import hiveweave.services.git_worktree.conflict_predict as cp

    cp._merge_tree_supported = False
    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "unknown"
    assert pred.degraded is True
    assert pred.behind == 1 and pred.ahead == 1


@pytest.mark.asyncio
async def test_predict_unknown_on_fatal_merge_tree(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """merge-tree rc=128(fatal) — unknown 放行(fail-open), 不触发降级。"""
    wt = await _make_worktree(git_repo)
    (wt / "file.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "other.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "other.txt")
    _git(git_repo, "commit", "-m", "main side")

    async def _fatal(base: str, branch: str, cwd: str) -> tuple[int, str]:
        return 128, "fatal: repository corruption"

    monkeypatch.setattr(
        "hiveweave.services.git_worktree.conflict_predict._merge_tree", _fatal
    )
    pred = await predict_merge_conflicts(str(wt))
    assert pred.status == "unknown"
    assert pred.degraded is False  # fatal ≠ 降级, 不永久禁用
    import hiveweave.services.git_worktree.conflict_predict as cp

    assert cp._merge_tree_supported is None  # 降级旗未被误置


# ── checkpoint 冲突预警 ────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_warns_on_conflict(git_repo: Path) -> None:
    """冲突布局下 checkpoint 回执带 WARNING + 冲突文件名(有新变更路径)。"""
    wt = await _make_worktree(git_repo)
    (wt / "file.txt").write_text("branch version\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch change")
    (git_repo / "file.txt").write_text("main version\n", encoding="utf-8")
    _git(git_repo, "add", "file.txt")
    _git(git_repo, "commit", "-m", "main change")
    # 未存档新文件 → checkpoint 走正常 commit 路径
    (wt / "wip.txt").write_text("wip\n", encoding="utf-8")

    gwt = GitWorktreeService()
    result = await gwt.checkpoint(str(git_repo), "A005", "存档")
    assert result["success"] is True, result
    msg = result.get("message") or ""
    assert "WARNING" in msg
    assert "file.txt" in msg
    assert "rebase" in msg.lower() or "rebase" in msg


@pytest.mark.asyncio
async def test_checkpoint_warns_on_newly_introduced_conflict(
    git_repo: Path,
) -> None:
    """本次存档新引入的冲突, 同一张回执就预警(审计 P2-1: 预测须在 commit 后)。"""
    wt = await _make_worktree(git_repo)
    # 先制造"分叉但无冲突": 双方改不同文件
    (wt / "branch-side.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "branch-side.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "shared.txt").write_text("main version\n", encoding="utf-8")
    _git(git_repo, "add", "shared.txt")
    _git(git_repo, "commit", "-m", "main side")
    # 未存档的 wip 改了 main 也改过的 shared.txt — 存档后即刻形成冲突
    (wt / "shared.txt").write_text("branch version\n", encoding="utf-8")

    gwt = GitWorktreeService()
    result = await gwt.checkpoint(str(git_repo), "A005", "存档")
    assert result["success"] is True, result
    msg = result.get("message") or ""
    assert "WARNING" in msg, "本次 checkpoint 引入的冲突应在同一回执预警"
    assert "shared.txt" in msg


@pytest.mark.asyncio
async def test_checkpoint_warning_survives_no_changes_path(
    git_repo: Path,
) -> None:
    """冲突布局 + 无新变更(空 checkpoint) — 早退路径同样带 WARNING。"""
    wt = await _make_worktree(git_repo)
    (wt / "file.txt").write_text("branch version\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch change")
    (git_repo / "file.txt").write_text("main version\n", encoding="utf-8")
    _git(git_repo, "add", "file.txt")
    _git(git_repo, "commit", "-m", "main change")
    # 不加新文件 → 工作区干净, 走 no-changes 早退

    gwt = GitWorktreeService()
    result = await gwt.checkpoint(str(git_repo), "A005", "空存档")
    assert result["success"] is True, result
    msg = result.get("message") or ""
    assert "no changes to commit" in msg
    assert "WARNING" in msg
    assert "file.txt" in msg


@pytest.mark.asyncio
async def test_checkpoint_no_warning_when_clean(git_repo: Path) -> None:
    """无冲突布局 — 回执不含 WARNING(防误报)。"""
    wt = await _make_worktree(git_repo)
    (wt / "wip.txt").write_text("wip\n", encoding="utf-8")

    gwt = GitWorktreeService()
    result = await gwt.checkpoint(str(git_repo), "A005", "存档")
    assert result["success"] is True, result
    msg = result.get("message") or ""
    assert "WARNING: main 已领先" not in msg


@pytest.mark.asyncio
async def test_checkpoint_note_when_degraded_and_behind(
    git_repo: Path,
) -> None:
    """git 过旧降级 + behind>0 且 ahead>0 — 回执给 NOTE(不拦)。"""
    wt = await _make_worktree(git_repo)
    (wt / "file.txt").write_text("b\n", encoding="utf-8")
    _git(wt, "add", "file.txt")
    _git(wt, "commit", "-m", "branch side")
    (git_repo / "other.txt").write_text("m\n", encoding="utf-8")
    _git(git_repo, "add", "other.txt")
    _git(git_repo, "commit", "-m", "main side")

    import hiveweave.services.git_worktree.conflict_predict as cp

    cp._merge_tree_supported = False
    (wt / "wip.txt").write_text("wip\n", encoding="utf-8")

    gwt = GitWorktreeService()
    result = await gwt.checkpoint(str(git_repo), "A005", "存档")
    assert result["success"] is True, result
    msg = result.get("message") or ""
    assert "NOTE" in msg
    assert "rebase" in msg
