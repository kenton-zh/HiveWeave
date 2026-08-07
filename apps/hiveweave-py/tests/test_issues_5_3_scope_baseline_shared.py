"""Regression tests for platform issues #5 and #3.

#5 — scope-aware VERIFY baseline: attestation staleness must be judged by
     whether the att→main-tip diff touches the verification scope (the parent
     task's changed files), not by raw tip distance. An unrelated merge (e.g.
     frontend landing while this VERIFY targets backend) must NOT force rework.
#3 — contract sharing: .hiveweave/shared/ is gitignored, so a fresh worktree
     has no shared contracts. Creating a worktree must mirror the project's
     shared dir so cross-end agents can read contracts locally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── #5: scope-aware VERIFY baseline ──────────────────────────────────────


def _mock_conn(rows: list[dict]) -> MagicMock:
    class _Row(dict):
        pass

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall = AsyncMock(return_value=[_Row(r) for r in rows])
    cur.close = AsyncMock()
    conn.execute = AsyncMock(return_value=cur)
    return conn


@pytest.mark.asyncio
async def test_verify_baseline_accepts_behind_but_unrelated_scope():
    """Attestation behind main tip but diff only touches unrelated files → OK."""
    from hiveweave.services.attestation import check_verify_baseline

    tip = "8992c22abcdef0123456789"
    att = "797ff6dbeef0123456789ab"  # ancestor of tip, ran the verified backend
    task = {
        "id": "verify-1",
        "title": "VERIFY: backend",
        "parent_task_id": "parent-1",
        "evidence": {"target_merge_commit": "62164ddc111122223333444"},
    }
    rows = [
        {
            "id": "att1",
            "kind": "test_run",
            "commit_hash": att,
            "exit_code": 0,
        }
    ]
    conn = _mock_conn(rows)

    parent = {
        "id": "parent-1",
        "evidence": {"files_changed": ["backend/main.py", "backend/api.py"]},
    }

    # _git side_effect order in check_verify_baseline:
    #   1. rev-parse HEAD → tip
    #   2. merge-base --is-ancestor <att> <main_tip> → True (ancestor)
    #   3. merge-base --is-ancestor <target> <att> → True (target in att)
    #   4. rev-list --count <att>..<main_tip> → "7" (behind > max_behind=5)
    #   5. diff --name-only <att> <main_tip> → only frontend files
    async def fake_git(args, cwd, timeout=30.0):
        if args[0:2] == ["rev-parse", "HEAD"]:
            return (True, tip + "\n")
        if args[0:2] == ["merge-base", "--is-ancestor"]:
            return (True, "")
        if args[0] == "rev-list":
            return (True, "7\n")
        if args[0] == "ls-files":
            return (True, "backend/main.py\nbackend/api.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\nfrontend/styles.css\n")
        return (False, "")

    with (
        patch(
            "hiveweave.services.attestation.attestation_service.ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.services.attestation._conn",
            AsyncMock(return_value=conn),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            side_effect=fake_git,
        ),
        patch(
            "hiveweave.services.task.TaskService",
        ) as TS,
    ):
        TS.return_value.get_task = AsyncMock(return_value=parent)
        err = await check_verify_baseline("p1", task, max_behind=5)

    assert err is None, f"unrelated diff should not force rework: {err}"


@pytest.mark.asyncio
async def test_verify_baseline_rejects_behind_touching_scope():
    """Attestation behind main tip AND diff touches the verify scope → reject."""
    from hiveweave.services.attestation import check_verify_baseline

    tip = "8992c22abcdef0123456789"
    att = "797ff6dbeef0123456789ab"
    task = {
        "id": "verify-1",
        "title": "VERIFY: backend",
        "parent_task_id": "parent-1",
        "evidence": {"target_merge_commit": "62164ddc111122223333444"},
    }
    rows = [
        {
            "id": "att1",
            "kind": "test_run",
            "commit_hash": att,
            "exit_code": 0,
        }
    ]
    conn = _mock_conn(rows)
    parent = {
        "id": "parent-1",
        "evidence": {"files_changed": ["backend/main.py"]},
    }

    async def fake_git(args, cwd, timeout=30.0):
        if args[0:2] == ["rev-parse", "HEAD"]:
            return (True, tip + "\n")
        if args[0:2] == ["merge-base", "--is-ancestor"]:
            return (True, "")
        if args[0] == "rev-list":
            return (True, "7\n")
        if args[0] == "ls-files":
            return (True, "backend/main.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "backend/main.py\n")
        return (False, "")

    with (
        patch(
            "hiveweave.services.attestation.attestation_service.ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.services.attestation._conn",
            AsyncMock(return_value=conn),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            side_effect=fake_git,
        ),
        patch(
            "hiveweave.services.task.TaskService",
        ) as TS,
    ):
        TS.return_value.get_task = AsyncMock(return_value=parent)
        err = await check_verify_baseline("p1", task, max_behind=5)

    assert err is not None
    assert "stale" in err.lower() or "baseline" in err.lower()


# ── #5 edge cases (audit fixes) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_baseline_rejects_directory_scope_touched():
    """Directory-level scope entry must match files under it (prefix match)."""
    from hiveweave.services.attestation import check_verify_baseline

    tip = "8992c22abcdef0123456789"
    att = "797ff6dbeef0123456789ab"
    task = {
        "id": "verify-1",
        "title": "VERIFY: backend",
        "parent_task_id": "parent-1",
        "evidence": {"target_merge_commit": "62164ddc111122223333444"},
    }
    rows = [
        {
            "id": "att1",
            "kind": "test_run",
            "commit_hash": att,
            "exit_code": 0,
        }
    ]
    conn = _mock_conn(rows)
    # Scope is a directory prefix; diff touches a file under it.
    parent = {
        "id": "parent-1",
        "evidence": {"files_changed": ["backend/"]},
    }

    async def fake_git(args, cwd, timeout=30.0):
        if args[0:2] == ["rev-parse", "HEAD"]:
            return (True, tip + "\n")
        if args[0:2] == ["merge-base", "--is-ancestor"]:
            return (True, "")
        if args[0] == "rev-list":
            return (True, "7\n")
        if args[0] == "ls-files":
            return (True, "backend/main.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "backend/main.py\n")
        return (False, "")

    with (
        patch(
            "hiveweave.services.attestation.attestation_service.ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.services.attestation._conn",
            AsyncMock(return_value=conn),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            side_effect=fake_git,
        ),
        patch(
            "hiveweave.services.task.TaskService",
        ) as TS,
    ):
        TS.return_value.get_task = AsyncMock(return_value=parent)
        err = await check_verify_baseline("p1", task, max_behind=5)

    assert err is not None, "directory scope + file under it must be a scope hit"


@pytest.mark.asyncio
async def test_verify_baseline_rejects_hiveweave_scope_undecidable():
    """Scope under untracked .hiveweave/ → undecidable → keep stale (reject)."""
    from hiveweave.services.attestation import check_verify_baseline

    tip = "8992c22abcdef0123456789"
    att = "797ff6dbeef0123456789ab"
    task = {
        "id": "verify-1",
        "title": "VERIFY: backend",
        "parent_task_id": "parent-1",
        "evidence": {"target_merge_commit": "62164ddc111122223333444"},
    }
    rows = [
        {
            "id": "att1",
            "kind": "test_run",
            "commit_hash": att,
            "exit_code": 0,
        }
    ]
    conn = _mock_conn(rows)
    parent = {
        "id": "parent-1",
        "evidence": {"files_changed": [".hiveweave/reports/evidence.md"]},
    }

    async def fake_git(args, cwd, timeout=30.0):
        if args[0:2] == ["rev-parse", "HEAD"]:
            return (True, tip + "\n")
        if args[0:2] == ["merge-base", "--is-ancestor"]:
            return (True, "")
        if args[0] == "rev-list":
            return (True, "7\n")
        # .hiveweave is gitignored → never tracked; ls-files returns only code.
        if args[0] == "ls-files":
            return (True, "backend/main.py\nfrontend/app.js\n")
        # diff never returns .hiveweave paths (gitignored) — but we must not
        # treat that as "untouched"; the guard returns None → reject.
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with (
        patch(
            "hiveweave.services.attestation.attestation_service.ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.services.attestation._conn",
            AsyncMock(return_value=conn),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            side_effect=fake_git,
        ),
        patch(
            "hiveweave.services.task.TaskService",
        ) as TS,
    ):
        TS.return_value.get_task = AsyncMock(return_value=parent)
        err = await check_verify_baseline("p1", task, max_behind=5)

    assert err is not None, ".hiveweave scope is undecidable → must stay stale"


@pytest.mark.asyncio
async def test_diff_touches_scope_none_on_fail_closed():
    """git diff failure → None (fail-closed); caller must not approve."""
    from hiveweave.services.attestation import _diff_touches_scope

    with patch(
        "hiveweave.services.git_worktree._git",
        AsyncMock(return_value=(False, "git error")),
    ):
        r = await _diff_touches_scope("/proj", "a", "b", {"backend/main.py"})
    assert r is None


@pytest.mark.asyncio
async def test_diff_touches_scope_casefold_and_prefix():
    """casefold + directory-prefix matching works."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        return (True, "Frontend/Button.TSX\nFrontend/Styles.css\n")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        # scope lowercase "frontend/" must match casefold'd "frontend/...".
        r = await _diff_touches_scope("/proj", "a", "b", {"frontend/"})
    assert r is True


@pytest.mark.asyncio
async def test_diff_touches_scope_rejects_case_mismatch_hiveweave():
    """Case-mismatched .Hiveweave/ scope must stay fail-closed (was a P1
    fail-open): the old guard was case-sensitive, so .Hiveweave/... bypassed
    it and got silently approved. ls-files finds no tracked path for it →
    unverifiable → None."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\nfrontend/app.js\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope(
            "/proj", "a", "b", {".Hiveweave/reports/evidence.md"}
        )
    assert r is None, "case-mismatched gitignored scope must be undecidable"


@pytest.mark.asyncio
async def test_diff_touches_scope_accepts_untouched_code_scope():
    """Pure tracked code scope untouched by the diff → False (accept)."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\nbackend/api.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope("/proj", "a", "b", {"backend/"})
    assert r is False


@pytest.mark.asyncio
async def test_diff_touches_scope_empty_diff_returns_false():
    """No changed files between commits → nothing to re-run → False."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope("/proj", "a", "b", {"backend/"})
    assert r is False


@pytest.mark.asyncio
async def test_diff_touches_scope_empty_diff_unverifiable_fail_closed():
    """Empty tracked diff with an unverifiable scope entry → None (fail-closed),
    not False: we cannot prove the gitignored entry was unaffected."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope(
            "/proj", "a", "b", {".hiveweave/reports/evidence.md"}
        )
    assert r is None, "unverifiable scope must stay undecidable on empty diff"


@pytest.mark.asyncio
async def test_diff_touches_scope_mixed_unverifiable_fail_closed():
    """Mixed scope with an unverifiable entry and untouched code → None
    (cannot prove the gitignored entry was unaffected)."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope(
            "/proj", "a", "b", {".hiveweave/reports/evidence.md", "backend/"}
        )
    assert r is None, "unverifiable entry in play → fail-closed"


@pytest.mark.asyncio
async def test_diff_touches_scope_empty_scope_returns_none():
    """Empty scope → None (caller keeps the distance-only stale rule)."""
    from hiveweave.services.attestation import _diff_touches_scope

    with patch(
        "hiveweave.services.git_worktree._git",
        AsyncMock(return_value=(True, "x\n")),
    ):
        r = await _diff_touches_scope("/proj", "a", "b", set())
    assert r is None


# ── #3: contract sharing into worktrees ──────────────────────────────────


def test_create_syncs_shared_contracts(tmp_path):
    """Worktree creation mirrors .hiveweave/shared/ into the worktree."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = tmp_path / "proj" / ".hiveweave" / "worktrees" / "A001"
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    (src / "taskflow-contract.md").write_text("# contract\n", encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "api.json").write_text("{}", encoding="utf-8")

    svc._sync_shared_contracts(str(ws), str(wt))

    assert (wt / ".hiveweave" / "shared" / "taskflow-contract.md").exists()
    assert (
        wt / ".hiveweave" / "shared" / "nested" / "api.json"
    ).read_text(encoding="utf-8") == "{}"


def test_create_sync_shared_noop_when_missing(tmp_path):
    """No shared dir → no-op, no error, no worktree dir created."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = tmp_path / "proj" / ".hiveweave" / "worktrees" / "A002"

    svc._sync_shared_contracts(str(ws), str(wt))

    assert not (wt / ".hiveweave" / "shared").exists()


# ── #3 edge cases (audit fixes) ─────────────────────────────────────────


def _bump_mtime(p: str, seconds_into_future: float) -> None:
    """Force *p*'s mtime strictly after a prior copy (mtime-granularity-safe)."""
    import os

    old = os.stat(p).st_mtime
    os.utime(p, (old, old + seconds_into_future))


def test_sync_shared_preserves_newer_local_file(tmp_path):
    """A worktree-local edit (dst newer than src) must NOT be clobbered."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = ws / ".hiveweave" / "worktrees" / "A"
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    root_file = src / "contract.md"
    root_file.write_text("v1", encoding="utf-8")

    svc._sync_shared_contracts(str(ws), str(wt))
    dst = wt / ".hiveweave" / "shared" / "contract.md"
    assert dst.read_text(encoding="utf-8") == "v1"

    # Agent edits the copied file locally, then we re-sync.
    dst.write_text("agent-edit", encoding="utf-8")
    _bump_mtime(str(dst), 10)  # dst strictly newer than src
    svc._sync_shared_contracts(str(ws), str(wt))

    assert (
        dst.read_text(encoding="utf-8") == "agent-edit"
    ), "worktree-local edit must be preserved, not overwritten"


def test_sync_shared_propagates_root_update(tmp_path):
    """A root-side contract update (src newer) still reaches the worktree."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = ws / ".hiveweave" / "worktrees" / "A"
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    root_file = src / "contract.md"
    root_file.write_text("v1", encoding="utf-8")

    svc._sync_shared_contracts(str(ws), str(wt))
    dst = wt / ".hiveweave" / "shared" / "contract.md"
    assert dst.read_text(encoding="utf-8") == "v1"

    root_file.write_text("v2", encoding="utf-8")
    _bump_mtime(str(root_file), 20)  # src strictly newer than dst
    svc._sync_shared_contracts(str(ws), str(wt))

    assert dst.read_text(encoding="utf-8") == "v2"


def test_sync_shared_preserves_local_added_file(tmp_path):
    """Repeated sync keeps a worktree-local file that has no root counterpart."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = ws / ".hiveweave" / "worktrees" / "A"
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    (src / "a.md").write_text("a", encoding="utf-8")

    svc._sync_shared_contracts(str(ws), str(wt))
    local = wt / ".hiveweave" / "shared" / "agent-notes.md"
    local.write_text("agent local", encoding="utf-8")

    svc._sync_shared_contracts(str(ws), str(wt))

    assert local.exists(), "worktree-local added file must survive repeat"
    assert (wt / ".hiveweave" / "shared" / "a.md").exists()


def test_sync_shared_skips_symlink(tmp_path):
    """Symlinks are skipped — external targets are not dereferenced."""
    import os

    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = ws / ".hiveweave" / "worktrees" / "A"
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    os.symlink(str(target), src / "link.md")

    svc._sync_shared_contracts(str(ws), str(wt))

    dst_link = wt / ".hiveweave" / "shared" / "link.md"
    assert not dst_link.exists(), "symlink must not be dereferenced into worktree"


def test_sync_shared_skips_nested_symlink(tmp_path):
    """A symlink nested inside a copied directory is also not dereferenced."""
    import os

    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    ws = tmp_path / "proj"
    wt = ws / ".hiveweave" / "worktrees" / "A"
    src_dir = ws / ".hiveweave" / "shared" / "bundle"
    src_dir.mkdir(parents=True)
    (src_dir / "real.txt").write_text("ok", encoding="utf-8")
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    os.symlink(str(target), src_dir / "leak.md")

    svc._sync_shared_contracts(str(ws), str(wt))

    dst_bundle = wt / ".hiveweave" / "shared" / "bundle"
    assert (dst_bundle / "real.txt").read_text(encoding="utf-8") == "ok"
    assert not (
        dst_bundle / "leak.md"
    ).exists(), "nested symlink must not be dereferenced into worktree"


@pytest.mark.asyncio
async def test_create_syncs_best_effort_and_calls_shared(tmp_path):
    """create() wires _sync_shared_contracts and never fails when it throws."""
    from hiveweave.services.git_worktree.service_create import CreateMixin

    svc = CreateMixin()
    wt_path = str(tmp_path / "proj" / ".hiveweave" / "worktrees" / "A001")
    with (
        patch.object(
            svc,
            "_create_unlocked",
            AsyncMock(
                return_value={
                    "success": True,
                    "path": wt_path,
                    "branch": "hw/a001",
                }
            ),
        ),
        patch.object(
            svc,
            "_sync_shared_contracts",
            side_effect=RuntimeError("boom"),
        ) as sync,
    ):
        result = await svc.create(str(tmp_path / "proj"), "a001")

    assert result["success"] is True, "sync failure must not fail the create"
    sync.assert_called_once()
    # sync was invoked (via asyncio.to_thread) with workspace + worktree paths.
    args = sync.call_args.args
    assert args[1] == wt_path