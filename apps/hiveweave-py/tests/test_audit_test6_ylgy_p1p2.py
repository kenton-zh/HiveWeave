"""TEST6 audit P1–P2: S8 orphan branches, S9 merge facts, S10 dismiss, S11 obligations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── S9: git_worktree_status merge facts ───────────────────────────────────


@pytest.mark.asyncio
async def test_git_worktree_status_includes_merge_facts():
    from hiveweave.tools.misc_tools import (
        GitWorktreeStatusParams,
        git_worktree_status_tool,
    )

    gwt = MagicMock()
    gwt.ensure_git_repo = AsyncMock()
    gwt.info = AsyncMock(
        return_value={
            "success": True,
            "status": {
                "branch": "hw/A004/work",
                "has_uncommitted": False,
                "head": "abc1234",
                "short_id": "A004",
                "base_branch": "main",
                "tip_is_ancestor_of_main": False,
                "commits_ahead": 3,
            },
        }
    )

    with (
        patch(
            "hiveweave.tools.misc_tools._get_worktree_context",
            AsyncMock(return_value=("/proj", "A001", "pid")),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            return_value=gwt,
        ),
    ):
        result = await git_worktree_status_tool(
            GitWorktreeStatusParams(shortId="A004"),
            "agent-1",
            "/proj",
            ctx=None,
        )

    assert result.success is True
    out = result.output or ""
    assert "tip_is_ancestor_of_main=false" in out
    assert "commits_ahead=3" in out
    assert "base=main" in out


# ── S8: orphan agent branch tagging ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_tags_orphan_agent_gone_branch(tmp_path: Path):
    """hw/<sid>/* with sid absent from agents → orphan_agent_branches."""
    import subprocess

    from hiveweave.services.git_worktree.reconcile import reconcile_worktrees

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
    (repo / "README").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    # Ensure main exists
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=repo, check=True, capture_output=True,
    )
    # Orphan tip not on main
    subprocess.run(
        ["git", "checkout", "-b", "hw/A099/work"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "orphan.txt").write_text("stranded")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "orphan work"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=repo, check=True, capture_output=True,
    )

    with patch(
        "hiveweave.services.git_worktree.reconcile._active_agent_short_ids",
        AsyncMock(return_value=({"A001", "A002"}, True)),  # A099 gone
    ), patch(
        "hiveweave.services.git_worktree.reconcile._project_db_if_exists",
        AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.git_worktree.reconcile._notify_orphan_branches",
        AsyncMock(return_value=None),
    ):
        report = await reconcile_worktrees(str(repo))

    orphans = report.get("orphan_agent_branches") or []
    assert any(o.get("branch") == "hw/A099/work" for o in orphans)
    orphan = next(o for o in orphans if o["branch"] == "hw/A099/work")
    assert orphan["reason"] == "orphan_agent_gone"
    assert orphan["tip_is_ancestor_of_main"] is False
    assert orphan["priority"] == "high"


# ── S10: dismiss unmerged tip forces quarantine ───────────────────────────


@pytest.mark.asyncio
async def test_find_reviewer_attestation_matches_ancestor_task(tmp_path):
    """S1: assignee test_run bound to parent unlocks child approve via extra_task_ids."""
    import time

    from hiveweave.db.project import ensure_project_db
    from hiveweave.services.attestation import (
        attestation_service,
        find_reviewer_attestation,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    # Point meta workspace lookup
    project_id = "p-anc"
    with patch(
        "hiveweave.db.meta.get_project_workspace",
        AsyncMock(return_value=str(ws)),
    ):
        await attestation_service.ensure_schema(project_id)
        aid = await attestation_service.create(
            project_id,
            agent_id="coord1",
            kind="test_run",
            command_or_url="npm test",
            exit_code=0,
            workspace=str(ws),
            stdout="ok",
            task_id="parent-task",
        )
        assert aid
        ok = await find_reviewer_attestation(
            project_id,
            "child-task",
            "ceo1",
            frozenset({"test_run"}),
            consume_agent_ids=["coord1"],
            extra_task_ids=["parent-task"],
            reviewer_must_hold=False,
        )
        assert ok is True
        # Wrong consume agent → False
        bad = await find_reviewer_attestation(
            project_id,
            "child-task",
            "ceo1",
            frozenset({"test_run"}),
            consume_agent_ids=["other"],
            extra_task_ids=["parent-task"],
            reviewer_must_hold=False,
        )
        assert bad is False


@pytest.mark.asyncio
async def test_review_obligation_task_scoped_idempotent_retarget():
    """S11: one pending review per task; submit retargets owner."""
    from hiveweave.services.obligation import ObligationLedger

    ledger = ObligationLedger()
    calls = {"n": 0}

    async def _query_side(pid, sql, params=None):
        calls["n"] += 1
        if "owner_agent_id" in sql and "review" not in sql.lower():
            return []
        if "obligation_type = 'review'" in sql or (
            "obligation_type = ?" in sql and params and "review" in params
        ):
            if calls["n"] <= 1:
                return []  # first create: no existing
            return [{"id": "ob-1", "owner_agent_id": "ceo1"}]
        return []

    execute = AsyncMock()
    with (
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(side_effect=_query_side),
        ),
        patch(
            "hiveweave.services.obligation._execute",
            execute,
        ),
    ):
        # First create inserts
        ob1 = await ledger.create(
            "p1", "ceo1", "review", task_id="t1",
            context={"source": "dispatch"},
        )
        assert ob1  # uuid or ob-1
        # Second create (submit, different owner) retargets
        ob2 = await ledger.create(
            "p1", "coord1", "review", task_id="t1",
            context={"source": "submit"},
        )
        assert ob2 == "ob-1"
        # UPDATE owner was issued
        assert any(
            "UPDATE obligations SET owner_agent_id" in str(c.args[1])
            for c in execute.await_args_list
            if len(c.args) > 1
        )


@pytest.mark.asyncio
async def test_dismiss_unmerged_tip_quarantines_and_notifies():
    from hiveweave.services.org import OrgService

    org = OrgService()
    agent_before = {
        "id": "exec1",
        "short_id": "A004",
        "name": "云帆",
        "workspace_path": "/proj/.hiveweave/worktrees/A004",
        "parent_id": "ceo1",
        "role": "board engineer",
        "status": "active",
    }
    updated = {**agent_before, "status": "archived"}

    gwt = MagicMock()
    gwt.quarantine_for_review = AsyncMock(
        return_value={
            "success": True,
            "path": "/proj/.hiveweave/worktrees/_quarantine/A004-x",
            "branch": "hw/A004/work",
        }
    )
    gwt.delete = AsyncMock()
    send = AsyncMock(return_value={"id": "m1"})

    with (
        patch.object(org, "get_subordinates", AsyncMock(return_value=[])),
        patch.object(org, "get_agent", AsyncMock(return_value=agent_before)),
        patch.object(org, "update_agent", AsyncMock(return_value=updated)),
        patch(
            "hiveweave.services.org_guardrails.check_dismiss_quota",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_guardrails.record_dismiss",
            AsyncMock(),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            AsyncMock(side_effect=Exception("skip tasks")),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            return_value=gwt,
        ),
        patch(
            "hiveweave.services.git_worktree.git_cmd._resolve_base_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "hiveweave.services.git_worktree.git_cmd._git",
            AsyncMock(return_value=(False, "")),  # not ancestor
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            AsyncMock(return_value=2),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            send,
        ),
        patch(
            "hiveweave.services.game_time.GameTimeService.cancel_alarms_for_agent",
            AsyncMock(),
        ),
        patch.object(org, "touch_org_version"),
        patch("hiveweave.services.org.Path") as P,
    ):
        P.return_value.is_dir.return_value = True
        result = await org.dismiss_agent("p1", "exec1", dismissed_by="ceo1")

    assert result.get("success") is True
    gwt.quarantine_for_review.assert_awaited()
    gwt.delete.assert_not_awaited()
    assert send.await_count >= 1
    texts = []
    for c in send.await_args_list:
        texts.append(str(c.kwargs.get("message") or ""))
        if c.args and len(c.args) >= 3:
            texts.append(str(c.args[2]))
    assert any("QUARANTINE" in t for t in texts)


# ── S11: review obligation on dispatch / submit / fulfill ─────────────────


@pytest.mark.asyncio
async def test_dispatch_creates_review_obligation():
    from hiveweave.services.dispatch import DispatchService

    ds = DispatchService()
    ds.task_service = MagicMock()
    ds.task_service.create_task = AsyncMock(return_value="task-1")
    ds.task_service.update_task = AsyncMock()
    ds.task_service.ensure_assignee_claimed = AsyncMock()
    ds.inbox = MagicMock()
    ds.inbox.send_message = AsyncMock()
    ds.handoff = MagicMock()
    ds.handoff.create_handoff = AsyncMock(return_value="h1")

    create_ob = AsyncMock(return_value="ob-1")
    with (
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.dispatch._ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.services.dispatch._execute",
            AsyncMock(),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/proj"),
        ),
        patch(
            "hiveweave.services.obligation.ObligationLedger.create",
            create_ob,
        ),
    ):
        result = await ds.dispatch_task(
            "p1", "ceo1", "exec1", "Implement feature X"
        )

    assert result.get("success") is True
    create_ob.assert_awaited()
    kwargs = create_ob.await_args
    # create(project_id, owner, type, task_id=...)
    assert kwargs.args[2] == "review" or (
        kwargs.kwargs.get("obligation_type") == "review"
    )
    assert "task-1" in str(kwargs)


@pytest.mark.asyncio
async def test_submit_creates_review_obligation():
    from hiveweave.services.task import TaskService

    ts = TaskService()
    create_ob = AsyncMock(return_value="ob-2")
    with (
        patch.object(ts, "require_task_id", AsyncMock(return_value="t1")),
        patch.object(
            ts,
            "get_task",
            AsyncMock(
                return_value={
                    "id": "t1",
                    "assignee_id": "exec1",
                    "creator_id": "ceo1",
                    "status": "running",
                }
            ),
        ),
        patch.object(ts, "_transition", AsyncMock()),
        patch.object(ts, "emit_task_event", AsyncMock()),
        patch(
            "hiveweave.services.tasks.submit._query",
            AsyncMock(
                side_effect=[
                    [{"evidence": "{}"}],
                    [{
                        "assignee_id": "exec1",
                        "creator_id": "ceo1",
                        "reviewer_id": None,
                        "tags": "[]",
                        "title": "feat",
                    }],
                ]
            ),
        ),
        patch(
            "hiveweave.services.tasks.submit._execute",
            AsyncMock(),
        ),
        patch.object(ts, "_is_verify_task", MagicMock(return_value=False)),
        patch(
            "hiveweave.services.obligation.ObligationLedger.create",
            create_ob,
        ),
    ):
        await ts.submit_task("p1", "t1", {"tests_passed": True})

    create_ob.assert_awaited()
    args = create_ob.await_args
    assert args.args[2] == "review"
    assert args.kwargs.get("task_id") == "t1" or "t1" in str(args)


@pytest.mark.asyncio
async def test_audit_backfills_missing_review_obligation():
    from hiveweave.services.obligation import ObligationLedger

    ledger = ObligationLedger()
    with (
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(
                side_effect=[
                    # open review-pipe tasks
                    [{
                        "id": "t-missing",
                        "creator_id": "ceo1",
                        "reviewer_id": "ceo1",
                        "assignee_id": "exec1",
                        "status": "submitted",
                    }],
                    # no existing obligation
                    [],
                ]
            ),
        ),
        patch.object(ledger, "create", AsyncMock(return_value="ob-x")) as create,
    ):
        fixed = await ledger.audit_missing_review_obligations("p1")

    assert fixed == ["t-missing"]
    create.assert_awaited_once()
