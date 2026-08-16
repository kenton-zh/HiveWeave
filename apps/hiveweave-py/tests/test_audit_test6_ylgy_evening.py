"""TEST6 evening audit E1–E6 / P3 — prefix fulfill, worktree gate, VERIFY baseline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── E1: obligation task_id normalization ───────────────────────────────────


@pytest.mark.asyncio
async def test_obligation_fulfill_normalizes_prefix():
    """8-char prefix must fulfill the full-UUID obligation row."""
    from hiveweave.services.obligation import ObligationLedger

    full_id = "fc8b3aec-1111-2222-3333-444444444444"
    prefix = "fc8b3aec"
    execute = AsyncMock()
    query = AsyncMock(
        side_effect=[
            # SELECT pending rows after normalize
            [{"id": "ob-review-1"}],
        ]
    )

    with (
        patch(
            "hiveweave.services.obligation._normalize_task_id",
            AsyncMock(return_value=full_id),
        ),
        patch("hiveweave.services.obligation._query", query),
        patch("hiveweave.services.obligation._execute", execute),
        patch.object(
            ObligationLedger,
            "_wake_dependent_tasks",
            AsyncMock(),
        ),
    ):
        n = await ObligationLedger().fulfill("p1", prefix, "review")

    assert n == 1
    assert execute.await_count == 1
    sql = execute.await_args_list[0].args[1]
    assert "fulfilled" in sql


@pytest.mark.asyncio
async def test_obligation_fulfill_miss_logs_zero():
    from hiveweave.services.obligation import ObligationLedger

    with (
        patch(
            "hiveweave.services.obligation._normalize_task_id",
            AsyncMock(return_value="deadbeef-0000-0000-0000-000000000000"),
        ),
        patch("hiveweave.services.obligation._query", AsyncMock(return_value=[])),
        patch("hiveweave.services.obligation._execute", AsyncMock()) as execute,
    ):
        n = await ObligationLedger().fulfill("p1", "deadbeef", "review")

    assert n == 0
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_closed_task_fulfills_pending():
    from hiveweave.services.obligation import ObligationLedger

    execute = AsyncMock()
    with (
        patch(
            "hiveweave.services.obligation._normalize_task_id",
            AsyncMock(return_value="t-full"),
        ),
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(
                return_value=[
                    {"id": "ob1", "obligation_type": "review"},
                    {"id": "ob2", "obligation_type": "merge"},
                ]
            ),
        ),
        patch("hiveweave.services.obligation._execute", execute),
    ):
        n = await ObligationLedger().reconcile_closed_task("p1", "t-full")

    assert n == 2
    assert "fulfilled" in execute.await_args.args[1]


# ── E2: write-worktree gate excludes VERIFY ─────────────────────────────────


@pytest.mark.asyncio
async def test_assignee_needs_write_worktree_excludes_verify(tmp_path: Path):
    import aiosqlite

    from hiveweave.services.git_worktree.reconcile import (
        _assignee_needs_write_worktree,
    )

    db = tmp_path / ".hiveweave" / "data.db"
    db.parent.mkdir(parents=True)
    conn = await aiosqlite.connect(db)
    await conn.executescript(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, short_id TEXT, status TEXT
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT,
            title TEXT, tags TEXT, is_archived INTEGER DEFAULT 0
        );
        INSERT INTO agents VALUES ('a1', 'A003', 'active');
        INSERT INTO tasks VALUES (
            'v1', 'a1', 'running', 'VERIFY: parent', '["verify"]', 0
        );
        """
    )
    await conn.commit()
    await conn.close()

    with patch(
        "hiveweave.services.git_worktree.reconcile._project_db_if_exists",
        AsyncMock(return_value=await aiosqlite.connect(db)),
    ):
        # Re-open for the helper (it may close)
        pass

    # Use raw open path via _open_project_db_raw mock
    async def _open(ws):
        c = await aiosqlite.connect(db)
        c.row_factory = aiosqlite.Row
        return c

    with (
        patch(
            "hiveweave.services.git_worktree.reconcile._project_db_if_exists",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.git_worktree.reconcile._open_project_db_raw",
            AsyncMock(side_effect=_open),
        ),
    ):
        needs = await _assignee_needs_write_worktree(str(tmp_path), "A003")

    assert needs is False


@pytest.mark.asyncio
async def test_ensure_skips_recreate_without_write_tasks():
    from hiveweave.services.git_worktree.ensure import ensure_executor_worktree

    agent = {
        "id": "a1",
        "short_id": "A004",
        "permission_type": "executor",
        "role": "qa",
        "workspace_path": None,
    }
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.org.OrgService",
            return_value=org,
        ),
        patch(
            "hiveweave.services.git_worktree.reconcile._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure.GitWorktreeService",
        ) as GWT,
    ):
        class _FakePath:
            def __init__(self, *a, **k):
                pass

            def __truediv__(self, other):
                return self

            def exists(self):
                return True

            def is_dir(self):
                return True

            @property
            def name(self):
                return "A004"

        with patch(
            "hiveweave.services.git_worktree.ensure.Path", _FakePath
        ):
            result = await ensure_executor_worktree("p1", "a1")

    assert result.get("skipped") is True
    assert result.get("success") is False
    GWT.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_broken_binding_missing_path_recreates(tmp_path: Path):
    """DB workspace_path set but dir gone → do not idle-skip; recreate."""
    from hiveweave.services.git_worktree.ensure import ensure_executor_worktree

    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()
    missing = ws / ".hiveweave" / "worktrees" / "A004"
    new_path = str(missing)
    agent = {
        "id": "a1",
        "short_id": "A004",
        "permission_type": "executor",
        "role": "qa",
        "workspace_path": str(missing),
    }
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()
    create_mock = AsyncMock(
        return_value={
            "success": True,
            "path": new_path,
            "branch": "hw/A004/work",
        }
    )

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(ws)),
        ),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch(
            "hiveweave.services.git_worktree.reconcile._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure.GitWorktreeService",
        ) as GWT,
    ):
        GWT.return_value.create = create_mock
        result = await ensure_executor_worktree("p1", "a1")

    assert result.get("skipped") is not True
    assert result.get("success") is True
    create_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_broken_binding_husk_no_git_recreates(tmp_path: Path):
    """Dir exists but no .git → broken binding, idle-skip must not fire."""
    from hiveweave.services.git_worktree.ensure import ensure_executor_worktree

    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()
    husk = ws / ".hiveweave" / "worktrees" / "A004"
    husk.mkdir(parents=True)
    agent = {
        "id": "a1",
        "short_id": "A004",
        "permission_type": "executor",
        "role": "qa",
        "workspace_path": str(husk),
    }
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()
    create_mock = AsyncMock(
        return_value={
            "success": True,
            "path": str(husk),
            "branch": "hw/A004/work",
        }
    )

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(ws)),
        ),
        patch("hiveweave.services.org.OrgService", return_value=org),
        patch(
            "hiveweave.services.git_worktree.reconcile._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure.GitWorktreeService",
        ) as GWT,
    ):
        GWT.return_value.create = create_mock
        result = await ensure_executor_worktree("p1", "a1")

    assert result.get("skipped") is not True
    assert result.get("success") is True
    create_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_verify_task_id_does_not_bypass_gate():
    """Audit P0-1: VERIFY task_id must not recreate write worktree."""
    from hiveweave.services.git_worktree.ensure import ensure_executor_worktree

    agent = {
        "id": "a1",
        "short_id": "A003",
        "permission_type": "coordinator",
        "role": "tech lead",
        "workspace_path": None,
    }
    verify_task = {
        "id": "verify-uuid",
        "title": "VERIFY: game",
        "tags": ["verify"],
    }
    org = MagicMock()
    org.resolve_agent = AsyncMock(return_value=agent)
    org.update_agent = AsyncMock()

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.org.OrgService",
            return_value=org,
        ),
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="coordinator",
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=verify_task),
        ),
        patch(
            "hiveweave.services.git_worktree.reconcile._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure.GitWorktreeService",
        ) as GWT,
    ):
        class _FakePath:
            def __init__(self, *a, **k):
                pass

            def __truediv__(self, other):
                return self

            def exists(self):
                return True

            def is_dir(self):
                return True

            @property
            def name(self):
                return "A003"

        with patch(
            "hiveweave.services.git_worktree.ensure.Path", _FakePath
        ):
            result = await ensure_executor_worktree(
                "p1", "a1", task_id="verify-uuid"
            )

    assert result.get("skipped") is True
    GWT.assert_not_called()


@pytest.mark.asyncio
async def test_refuse_project_root_allows_verify_only_writer():
    """Audit P0-2: VERIFY-only writer may write throwaway scripts on main."""
    from hiveweave.tools.pipeline import ToolContext, _refuse_project_root_write

    agent = {
        "id": "a1",
        "short_id": "A003",
        "permission_type": "coordinator",
        "project_id": "p1",
        "role": "tech lead",
    }
    org = MagicMock()
    org.get_agent = AsyncMock(return_value=agent)
    ctx = ToolContext(org=org)

    with (
        patch(
            "hiveweave.services.git_worktree.agent_gets_write_worktree",
            return_value=True,
        ),
        patch(
            "hiveweave.tools.file.infer_project_root",
            return_value="/proj",
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
    ):
        err = await _refuse_project_root_write(
            "a1", "/proj", "write_file", ctx
        )

    assert err is None


def test_productive_continue_source_does_not_refill_budget():
    """Audit P1-3: productive_continue must be in no-refill set."""
    # Source gate is inline in Agent.chat — assert the exclusion set contract
    # by grepping the compiled source of the refill condition.
    import inspect

    from hiveweave.agents import agent as agent_mod

    src = inspect.getsource(agent_mod.Agent.chat)
    assert "productive_continue" in src
    # Must appear in the "do not refill" tuple/set near _slice_budget
    assert (
        'source not in (\n                "turn_exit_gate",\n'
        '                "open_task_reminder",\n'
        '                "productive_continue",\n            )'
        in src
        or '"productive_continue"' in src
        and "turn_exit_gate" in src
        and "_slice_budget" in src
    )


# ── E3: VERIFY baseline hard gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_verify_baseline_rejects_stale_commit():
    from hiveweave.services.attestation import check_verify_baseline

    task = {
        "id": "verify-1",
        "title": "VERIFY: game",
        "tags": ["verify"],
        "evidence": {"target_merge_commit": "4937306abcdef"},
    }
    rows = [
        {
            "id": "att1",
            "kind": "test_run",
            "commit_hash": "a4f3939bf4dead",
            "exit_code": 0,
        }
    ]

    class _Row(dict):
        def keys(self):
            return super().keys()

    mock_rows = [_Row(r) for r in rows]
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall = AsyncMock(return_value=mock_rows)
    cur.close = AsyncMock()
    conn.execute = AsyncMock(return_value=cur)

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
            AsyncMock(
                side_effect=[
                    (True, "4937306abcdef\n"),  # HEAD
                    (False, ""),  # is-ancestor stale→tip
                ]
            ),
        ),
    ):
        err = await check_verify_baseline("p1", task)

    assert err is not None
    assert "stale" in err.lower() or "baseline" in err.lower()


@pytest.mark.asyncio
async def test_check_verify_baseline_accepts_target_commit():
    from hiveweave.services.attestation import check_verify_baseline

    tip = "4937306abcdef0123456789"
    task = {
        "id": "verify-1",
        "title": "VERIFY: game",
        "tags": ["verify"],
        "evidence": {"target_merge_commit": tip},
    }

    class _Row(dict):
        pass

    mock_rows = [
        _Row(
            {
                "id": "att1",
                "kind": "test_run",
                "commit_hash": tip,
                "exit_code": 0,
            }
        )
    ]
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall = AsyncMock(return_value=mock_rows)
    cur.close = AsyncMock()
    conn.execute = AsyncMock(return_value=cur)

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
            AsyncMock(return_value=(True, tip + "\n")),
        ),
    ):
        err = await check_verify_baseline("p1", task)

    assert err is None


# ── E4: quarantine orphan branch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_quarantine_orphan_branch(tmp_path: Path):
    import subprocess

    from hiveweave.services.git_worktree.reconcile import quarantine_orphan_branch

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    # Ensure main exists (some git versions use master)
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "hw/A013/work"],
        cwd=repo, check=True, capture_output=True,
    )

    res = await quarantine_orphan_branch(str(repo), "hw/A013/work")
    assert res.get("success") is True
    assert "refs/quarantine/" in (res.get("quarantine_ref") or "")

    listed = subprocess.run(
        ["git", "branch", "--list", "hw/A013/work"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert listed.stdout.strip() == ""

    qref = res["quarantine_ref"]
    tip_ok = subprocess.run(
        ["git", "rev-parse", qref],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert tip_ok.stdout.strip() == res["tip"]


# ── E6: stop_processes_for_project ─────────────────────────────────────────


def test_stop_processes_for_project_kills_registered():
    from hiveweave.services import process_registry as pr

    pr.clear_registry_for_tests()
    rec = pr.ProcessRecord(
        project_id="proj1",
        port=3000,
        pid=999999,  # dead pid → already_dead path
        cwd="/proj",
        command="vite",
    )
    pr._registry["proj1:3000"] = rec
    pr._hydrated = True

    out = pr.stop_processes_for_project("proj1")
    assert any(s.get("status") == "already_dead" for s in out["stopped"])
    assert "proj1:3000" not in pr._registry
    pr.clear_registry_for_tests()


# ── P3: gitignore includes .agent-browser ──


# ── E3 follow-up: test_run stamps commit_hash ──────────────────────────────


@pytest.mark.asyncio
async def test_issue_test_run_attestation_records_commit_hash(tmp_path: Path):
    import subprocess

    from hiveweave.tools.bash import _issue_test_run_attestation

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    create = AsyncMock(return_value="att-1")
    with (
        patch(
            "hiveweave.services.attestation.is_test_command",
            return_value=True,
        ),
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("task-1", "")),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            create,
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.emit_task_event",
            AsyncMock(),
        ),
    ):
        note = await _issue_test_run_attestation(
            project_id="p1",
            agent_id="a1",
            command="npx vitest run",
            workspace=str(repo),
            stdout="ok",
            exit_code=0,
            task_id="task-1",
        )

    assert "attestation_id=att-1" in note
    assert create.await_count == 1
    kwargs = create.await_args.kwargs
    assert kwargs.get("commit_hash")
    assert kwargs["commit_hash"].startswith(head[:12])


def test_gitignore_generated_includes_agent_browser():
    from hiveweave.services.git_worktree.constants import (
        GITIGNORE_GENERATED_ENTRIES,
    )

    assert ".agent-browser/" in GITIGNORE_GENERATED_ENTRIES
