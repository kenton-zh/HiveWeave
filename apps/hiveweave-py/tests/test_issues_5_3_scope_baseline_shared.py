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


@pytest.fixture(autouse=True)
def _clear_scope_state_cache():
    """Each test gets a pristine git-scope cache (it is module-level and
    TTL-cached by workspace, so it would otherwise leak mock state across
    tests that share the "/proj" workspace key)."""
    from hiveweave.services import attestation

    attestation._scope_state_cache.clear()
    yield
    attestation._scope_state_cache.clear()


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
        if args[0] == "status":
            return (True, "")  # no untracked/ignored content
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
        if args[0] == "status":
            return (True, "")  # no untracked/ignored content
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
        if args[0] == "status":
            return (True, "")  # no untracked/ignored content
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
        if args[0] == "status":
            return (True, "!! .hiveweave/\n")
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
    """git ls-files failure → None (fail-closed); caller must not approve."""
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
        if args[0] == "ls-files":
            return (True, "Frontend/Button.TSX\nFrontend/Styles.css\n")
        if args[0] == "status":
            return (True, "")
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
        if args[0] == "status":
            return (True, "!! .hiveweave/\n")
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
        if args[0] == "status":
            return (True, "")
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
        if args[0] == "status":
            return (True, "")
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
        if args[0] == "status":
            return (True, "!! .hiveweave/\n")
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
        if args[0] == "status":
            return (True, "!! .hiveweave/\n")
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


# ── #5 P2 (audit): directory heuristic + git-scope cache ─────────────────


def test_is_ignorable_path():
    """Build/cache dirs are ignorable; real paths are not."""
    from hiveweave.services.attestation import _is_ignorable_path

    assert _is_ignorable_path("frontend/node_modules/lodash/index.js")
    assert _is_ignorable_path("backend/__pycache__/x.pyc")
    assert _is_ignorable_path("node_modules")
    assert not _is_ignorable_path("backend/main.py")
    assert not _is_ignorable_path(".hiveweave/reports/evidence.md")
    assert not _is_ignorable_path("backend/.env")


@pytest.mark.asyncio
async def test_diff_touches_scope_dir_with_only_build_ignored_verifiable():
    """A directory scope that only hides build/cache output is still
    verifiable and accepts when the diff is unrelated."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\nbackend/api.py\n")
        if args[0] == "status":
            return (True, "!! backend/node_modules/\n!! backend/__pycache__/\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope("/proj", "a", "b", {"backend/"})
    assert r is False, "build-only ignored content must not force fail-closed"


@pytest.mark.asyncio
async def test_diff_touches_scope_dir_with_substantive_unverifiable():
    """A directory scope with substantive (non-build) untracked/ignored content
    under it → cannot be fully validated → unverifiable → fail-closed."""
    from hiveweave.services.attestation import _diff_touches_scope

    async def fake_git(args, cwd, timeout=30.0):
        if args[0] == "ls-files":
            return (True, "backend/main.py\nbackend/api.py\n")
        if args[0] == "status":
            return (True, "?? backend/.env\n")
        if args[0:2] == ["diff", "--name-only"]:
            return (True, "frontend/app.js\n")
        return (False, "")

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r = await _diff_touches_scope("/proj", "a", "b", {"backend/"})
    assert r is None, "substantive unverifiable content under dir → fail-closed"


@pytest.mark.asyncio
async def test_scope_state_cache_reuses_probe():
    """`_scope_state` result is cached: two _diff_touches_scope calls trigger
    only one ls-files/status probe each."""
    from hiveweave.services.attestation import _diff_touches_scope

    ls_files_calls = 0
    status_calls = 0

    async def fake_git(args, cwd, timeout=30.0):
        nonlocal ls_files_calls, status_calls
        if args[0] == "ls-files":
            ls_files_calls += 1
            return (True, "backend/main.py\n")
        if args[0] == "status":
            status_calls += 1
            return (True, "")
        return (True, "")  # empty diff for both calls

    with patch(
        "hiveweave.services.git_worktree._git",
        side_effect=fake_git,
    ):
        r1 = await _diff_touches_scope("/proj", "a", "b", {"backend/"})
        r2 = await _diff_touches_scope("/proj", "c", "d", {"backend/"})

    assert r1 is False and r2 is False
    assert ls_files_calls == 1, "ls-files must be probed once, then cached"
    assert status_calls == 1, "status must be probed once, then cached"


# ── #3: contract sharing into worktrees (tracked semantics, PR-A) ────────
#
# Issue #3 was orginally solved by `_sync_shared_contracts` (per-create copy
# of .hiveweave/shared into each worktree). Since the audit, shared/reports/
# drafts/handoffs are git-tracked — `git worktree add` carries them natively,
# so the copy-based sync is gone. These tests pin the replacement contract.


def _git(cwd: Path, *args: str) -> None:
    import subprocess as sp
    sp.run(["git", *args], cwd=cwd, check=True, capture_output=True,
           text=True, encoding="utf-8", errors="replace")


def test_create_syncs_shared_contracts(tmp_path):
    """Worktree creation carries tracked .hiveweave/shared/ natively."""
    from hiveweave.services.git_worktree import GitWorktreeService
    import asyncio

    ws = tmp_path / "proj"
    ws.mkdir()
    _git(ws, "init", "-b", "main")
    _git(ws, "config", "user.email", "t@h.l")
    _git(ws, "config", "user.name", "t")
    src = ws / ".hiveweave" / "shared"
    src.mkdir(parents=True)
    (src / "taskflow-contract.md").write_text("# contract\n", encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "api.json").write_text("{}", encoding="utf-8")
    _git(ws, "add", ".hiveweave/shared")
    _git(ws, "commit", "-m", "contract")

    gwt = GitWorktreeService()
    res = asyncio.run(gwt.create(str(ws), "A001", "feat-x"))
    assert res["success"] is True, res
    wt = tmp_path / "proj" / ".hiveweave" / "worktrees" / "A001"
    assert (wt / ".hiveweave" / "shared" / "taskflow-contract.md").exists()
    assert (
        wt / ".hiveweave" / "shared" / "nested" / "api.json"
    ).read_text(encoding="utf-8") == "{}"


