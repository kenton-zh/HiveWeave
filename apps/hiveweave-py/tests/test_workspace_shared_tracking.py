"""Workspace shared-docs git tracking (PR-A: join .hiveweave-shared into git).

验证 4 个目标:
1. 新模板 gitignore: .hiveweave 私有区忽略, shared/reports/drafts/handoffs 反选入库
2. 存量旧模板 (整目录 .hiveweave/) 迁移: 重写 + 单次维护提交
3. worktree checkout 天然携带 tracked shared 契约 (不再需要 _sync_shared_contracts)
4. merge: 分支新增 shared 契约携带回 main; 冲突 (binary) 可见并 abort;
   main 上仅 workspaces 目录的 dirty 自动提交放行, 其它 dirty 仍拒绝
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import (
    GitWorktreeService,
    scan_conflict_markers,
)
from hiveweave.services.git_worktree.constants import (
    HIVEWEAVE_DIR,
    TRACKED_WS_DIRS,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_ok(cwd: Path, *args: str) -> bool:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.returncode == 0


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


# ── 1. 新模板: 4 目录反选入库, 其余 .hiveweave 忽略 ────────────


@pytest.mark.asyncio
async def test_new_gitignore_tracks_workspace_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    res = await gwt.ensure_git_repo(str(repo))
    assert res["success"] is True, res

    # tracked-workspace dirs are NOT ignored
    for d in TRACKED_WS_DIRS:
        probe = repo / d / ".gitignore-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("x\n", encoding="utf-8")
        assert _git_ok(repo, "check-ignore", "-q", str(probe)) is False, d
    # private dirs stay ignored
    priv = repo / ".hiveweave" / "worktrees" / "A001"
    priv.mkdir(parents=True)
    assert _git_ok(repo, "check-ignore", "-q", str(priv)) is True


# ── 2. 存量迁移: old .hiveweave/ → 维护提交 ──────────────────


@pytest.mark.asyncio
async def test_migrate_legacy_gitignore_commits_maintenance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / ".gitignore").write_text(
        "# HiveWeave 系统目录\n.hiveweave/\n\nnode_modules/\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init with legacy ignore")

    gwt = GitWorktreeService()
    res = await gwt.ensure_git_repo(str(repo))
    assert res["success"] is True, res

    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".hiveweave/*\n" in content
    assert "!.hiveweave/shared/\n" in content
    # single maintenance commit lands on main
    log = _git(repo, "log", "--oneline", "-3")
    assert "maintenance: track hiveweave shared workspace" in log.stdout
    # workspace dir now un-ignored
    shared = repo / ".hiveweave" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "contract.md").write_text("c\n", encoding="utf-8")
    assert _git_ok(repo, "check-ignore", "-q", str(shared / "contract.md")) is False
    # idempotent: second ensure_git_repo does not add another maintenance commit
    before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    res2 = await gwt.ensure_git_repo(str(repo))
    assert res2["success"] is True
    after = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert before == after


@pytest.mark.asyncio
async def test_migrate_skips_when_hiveweave_files_already_tracked(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / ".gitignore").write_text(".hiveweave/\n", encoding="utf-8")
    (repo / ".hiveweave" / "shared").mkdir(parents=True)
    (repo / ".hiveweave" / "shared" / "x.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-f", ".hiveweave/shared/x.md")
    _git(repo, "commit", "-m", "init")

    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))

    # migration aborted — no maintenance commit, tracked file untouched
    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".hiveweave/\n" in content
    assert _git_ok(repo, "ls-files", "--error-unmatch", ".hiveweave/shared/x.md")


# ── 3. worktree 天然携带 shared (不再需要 _sync_shared_contracts) ──


@pytest.mark.asyncio
async def test_new_worktree_checkout_brings_shared(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))
    shared = repo / ".hiveweave" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "taskflow-contract.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".hiveweave/shared/taskflow-contract.md")
    _git(repo, "commit", "-m", "add contract")

    res = await gwt.create(str(repo), "A010", "feat-x")
    assert res["success"] is True, res
    wt = Path(res["path"])
    carried = wt / ".hiveweave" / "shared" / "taskflow-contract.md"
    assert carried.exists()
    assert carried.read_text(encoding="utf-8") == "v1\n"
    assert _git_ok(wt, "ls-files", "--error-unmatch",
                   ".hiveweave/shared/taskflow-contract.md")


# ── 4. merge 携带 shared + 冲突可见 + dirty 放行 ─────────────


async def _branch_with_shared_file(repo: Path, short_id: str, task: str,
                                   rel: str, content: str) -> str:
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, task)
    assert res["success"] is True, res
    wt = Path(res["path"])
    f = wt / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    _git(wt, "add", rel)
    _git(wt, "commit", "-m", f"add {rel}")
    return res["branch"]


@pytest.mark.asyncio
async def test_branch_contract_merges_to_main(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))
    branch = await _branch_with_shared_file(
        repo, "A001", "feat-x", ".hiveweave/shared/api.md", "api v1\n"
    )

    res = await gwt.merge_by_branch(str(repo), branch, "main")
    assert res["success"] is True, res
    assert (repo / ".hiveweave" / "shared" / "api.md").exists()
    staged = _git(repo, "ls-files", ".hiveweave/shared/api.md")
    assert staged.stdout.strip() == ".hiveweave/shared/api.md"


@pytest.mark.asyncio
async def test_conflicting_contracts_abort_with_markers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))
    shared = repo / ".hiveweave" / "shared"
    shared.mkdir(parents=True)
    (shared / "api.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".hiveweave/shared/api.md")
    _git(repo, "commit", "-m", "base contract")

    # branch A edits the contract
    branch_a = await _branch_with_shared_file(
        repo, "A001", "t-a", ".hiveweave/shared/api.md", "api v1\nA-variant\n"
    )
    # branch B edits the same contract divergently
    branch_b = await _branch_with_shared_file(
        repo, "A002", "t-b", ".hiveweave/shared/api.md", "api v1\nB-variant\n"
    )

    # merge B succeeds on its own (non-conflicting diff vs base)
    r1 = await gwt.merge_by_branch(str(repo), branch_b, "main")
    assert r1["success"] is True, r1
    # merge A now conflicts: binary driver → conflicts reported & abort
    r2 = await gwt.merge_by_branch(str(repo), branch_a, "main")
    assert r2["success"] is False, r2
    assert "conflict" in (r2.get("reason") or (r2.get("message") or "")).lower() or \
        r2.get("conflicts"), r2
    # no partial merge landed — main must be clean of marker residue
    assert scan_conflict_markers(str(repo)) == []
    # main still holds B's variant only
    assert (repo / ".hiveweave" / "shared" / "api.md").read_text(
        encoding="utf-8"
    ) == "api v1\nB-variant\n"


@pytest.mark.asyncio
async def test_main_workspace_docs_dirty_autocommits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))
    # baseline contract on main
    branch_base = await _branch_with_shared_file(
        repo, "A009", "t-base", ".hiveweave/shared/contract.md", "v0\n"
    )
    r = await gwt.merge_by_branch(str(repo), branch_base, "main")
    assert r["success"] is True, r

    # main-side edit of the shared doc (uncommitted)
    doc = repo / ".hiveweave" / "shared" / "contract.md"
    doc.write_text("v0\nmain-edit\n", encoding="utf-8")

    # a worktree branch adds code
    branch = await _branch_with_shared_file(
        repo, "A011", "t-code", "src/thing.py", "print('x')\n"
    )

    # dirty gate should auto-commit the workspace doc, not reject
    res = await gwt.merge_by_branch(str(repo), branch, "main")
    assert res["success"] is True, res
    assert doc.read_text(encoding="utf-8") == "v0\nmain-edit\n"


@pytest.mark.asyncio
async def test_main_non_workspace_dirty_still_rejects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    g = GitWorktreeService()
    await g.ensure_git_repo(str(repo))
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("v0\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "app")

    branch = await _branch_with_shared_file(
        repo, "A012", "t-code", "src/other.py", "print('y')\n"
    )
    (repo / "src" / "app.py").write_text("v0\nDIRTY\n", encoding="utf-8")

    res = await g.merge_by_branch(str(repo), branch, "main")
    assert res["success"] is False, res
    assert res.get("reason") == "main_dirty", res


# ── 5. quarantine 回归 (audit P1-1 / P1-2 修复) ─────────────────


def _legacy_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Legacy repo (old .gitignore ignores all of .hiveweave/) + one linked
    worktree containing the old untracked sync copy."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / ".gitignore").write_text(
        "# HiveWeave 系统目录\n.hiveweave/\n\nnode_modules/\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "legacy init")

    wt = repo.parent / "wt-a"
    _git(repo, "worktree", "add", str(wt), "-b", "feat-a")
    wt_shared = wt / ".hiveweave" / "shared"
    wt_shared.mkdir(parents=True)
    (wt_shared / "copy.md").write_text("agent-edit\n", encoding="utf-8")
    # P1-1 precondition: under the legacy ignore the copy is INVISIBLE to
    # status --porcelain (ignored), which is why the old scan never fired.
    assert _git_ok(wt, "check-ignore", "-q", ".hiveweave/shared/copy.md")
    return repo, wt


def _quarantined(qroot: Path) -> list[str]:
    return sorted(
        p.relative_to(qroot).as_posix()
        for p in qroot.rglob("*")
        if p.is_file()
    )


@pytest.mark.asyncio
async def test_quarantine_moves_ignored_legacy_copies(tmp_path: Path) -> None:
    """P1-1: legacy sync copies are IGNORED files — ls-files --others (no
    --exclude-standard) must find them and preserve them under quarantine."""
    repo, wt = _legacy_repo(tmp_path)

    gwt = GitWorktreeService()
    res = await gwt.ensure_git_repo(str(repo))
    assert res["success"] is True, res

    # copy moved out of the merge path, content intact, preserved not deleted
    assert not (wt / ".hiveweave" / "shared" / "copy.md").exists()
    q = wt / ".hiveweave" / "merge-quarantine"
    stamps = [p for p in q.iterdir() if p.is_dir()] if q.is_dir() else []
    assert len(stamps) == 1, stamps
    files = _quarantined(stamps[0])
    assert files == [".hiveweave/shared/copy.md"], files
    assert (stamps[0] / ".hiveweave" / "shared" / "copy.md").read_text(
        encoding="utf-8"
    ) == "agent-edit\n"
    # merge into the worktree now lands without overwriting anything
    _git(wt, "merge", "main", "--no-edit")
    assert (wt / ".hiveweave" / "shared" / "copy.md").exists() is False


@pytest.mark.asyncio
async def test_quarantine_preserves_tracked_branch_edits(tmp_path: Path) -> None:
    """Quarantine must skip files TRACKED in the worktree branch (the agent's
    committed contract edit) — only untracked copies move."""
    repo, wt = _legacy_repo(tmp_path)
    # agent force-commits a contract edit on the branch (tracked, must stay)
    (wt / ".hiveweave" / "shared" / "tracked.md").write_text(
        "committed\n", encoding="utf-8"
    )
    _git(wt, "add", "-f", ".hiveweave/shared/tracked.md")
    _git(wt, "commit", "-m", "agent contract edit")

    gwt = GitWorktreeService()
    res = await gwt.ensure_git_repo(str(repo))
    assert res["success"] is True, res

    assert (wt / ".hiveweave" / "shared" / "tracked.md").exists()
    assert _git_ok(
        wt, "ls-files", "--error-unmatch", ".hiveweave/shared/tracked.md"
    )
    # only the untracked copy was quarantined
    assert not (wt / ".hiveweave" / "shared" / "copy.md").exists()
    q = wt / ".hiveweave" / "merge-quarantine"
    stamps = [p for p in q.iterdir() if p.is_dir()] if q.is_dir() else []
    assert len(stamps) == 1, stamps
    assert _quarantined(stamps[0]) == [".hiveweave/shared/copy.md"]


@pytest.mark.asyncio
async def test_migrate_commit_failure_rolls_back_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-2: commit failure must restore .gitignore + delete the brand-new
    .gitattributes + unstage, leaving main clean and the migration retryable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / ".gitignore").write_text(
        "# HiveWeave 系统目录\n.hiveweave/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "legacy init")

    import hiveweave.services.git_worktree.service_create as sc

    real_git = sc._git

    async def failing_git(args, cwd):
        joined = " ".join(args)
        if "commit" in args and "maintenance" in joined:
            return False, "error: simulated commit failure"
        return await real_git(args, cwd)

    monkeypatch.setattr(sc, "_git", failing_git)
    gwt = GitWorktreeService()
    res = await gwt.ensure_git_repo(str(repo))
    assert res["success"] is True, res

    # rollback: legacy rule back on disk, new .gitattributes gone, repo CLEAN
    content = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".hiveweave/\n" in content
    assert ".hiveweave/*" not in content
    assert not (repo / ".gitattributes").exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # retry (real git) succeeds once the failure clears — no permanent no-op
    monkeypatch.undo()
    res2 = await gwt.ensure_git_repo(str(repo))
    assert res2["success"] is True
    log = _git(repo, "log", "--oneline", "-3")
    assert "maintenance: track hiveweave shared workspace" in log.stdout


@pytest.mark.asyncio
async def test_gitattributes_shared_pattern_matches_nested(tmp_path: Path) -> None:
    """P2: 单 `*` 模式漏掉嵌套子目录 — `**` 必须同时覆盖直接子级与嵌套
    (shared binary / drafts+handoffs union, check-attr 实证)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    gwt = GitWorktreeService()
    await gwt.ensure_git_repo(str(repo))
    attr = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ".hiveweave/shared/**/*.md merge=binary" in attr
    assert ".hiveweave/drafts/**/*.md merge=union" in attr
    assert ".hiveweave/handoffs/**/*.md merge=union" in attr
    probes: list[tuple[Path, str]] = [
        (repo / ".hiveweave" / "shared" / "api.md", "binary"),
        (repo / ".hiveweave" / "shared" / "api" / "rest.md", "binary"),
        (repo / ".hiveweave" / "drafts" / "sub" / "log.md", "union"),
        (repo / ".hiveweave" / "handoffs" / "a" / "b" / "h.md", "union"),
    ]
    paths: list[str] = []
    for p, _v in probes:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
        paths.append(p.relative_to(repo).as_posix())
    out = _git(repo, "check-attr", "merge", "--", *paths).stdout
    for p, want in probes:
        rel = p.relative_to(repo).as_posix()
        line = next(
            (ln for ln in out.splitlines()
             if ln.startswith(f"{rel}:")), ""
        )
        assert f"merge: {want}" in line, (rel, line)