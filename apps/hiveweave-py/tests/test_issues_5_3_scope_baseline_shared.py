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