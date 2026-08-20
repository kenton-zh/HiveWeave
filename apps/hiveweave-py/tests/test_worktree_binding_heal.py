"""疏通: keep/heal worktree bindings instead of wiping them (P1-5 regression).

Wiping agents.workspace_path when cwd is MAIN made approved-unmerged trees
invisible to merge/reconcile. Agents then bounced on 'not registered'.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _executor_row(*, short_id="A011", ws_path=None):
    return {
        "id": "agent-a011",
        "short_id": short_id,
        "name": "星火",
        "role": "storage engineer",
        "permission_type": "executor",
        "status": "active",
        "workspace_path": ws_path,
        "worktree_error": None,
    }


def _make_agent():
    from hiveweave.agents.agent import Agent

    return Agent(agent_id="agent-a011", project_id="p-bind", config={})


def _make_tree(project: Path, short_id: str) -> Path:
    wt = project / ".hiveweave" / "worktrees" / short_id
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake", encoding="utf-8")
    (project / ".git").mkdir(exist_ok=True)
    return wt


def _no_wipe(update_mock: AsyncMock) -> None:
    for call in update_mock.await_args_list:
        payload = call.args[1] if len(call.args) >= 2 else {}
        if isinstance(payload, dict):
            assert payload.get("workspace_path") is not None or "workspace_path" not in payload


@pytest.mark.asyncio
async def test_idle_live_tree_keeps_binding_and_cwd(tmp_path: Path):
    """approved/idle with a live tree: stay on the tree, do not wipe DB."""
    project = tmp_path.resolve()
    wt = _make_tree(project, "A011")
    agent = _make_agent()
    row = _executor_row(ws_path=str(wt))

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(project)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_is_verify_only",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(),
        ) as ensure,
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=row)
        org.update_agent = AsyncMock()
        result = await agent._get_workspace_path()

    assert result == str(wt)
    ensure.assert_not_awaited()
    _no_wipe(org.update_agent)


@pytest.mark.asyncio
async def test_empty_db_heals_binding_from_disk(tmp_path: Path):
    """DB path empty but tree on disk → write binding back (疏通)."""
    project = tmp_path.resolve()
    wt = _make_tree(project, "A011")
    agent = _make_agent()
    row = _executor_row(ws_path=None)

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(project)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree._assignee_is_verify_only",
            AsyncMock(return_value=False),
        ),
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=row)
        org.update_agent = AsyncMock()
        result = await agent._get_workspace_path()

    assert Path(result).resolve() == wt.resolve()
    wrote = False
    for call in org.update_agent.await_args_list:
        payload = call.args[1]
        if payload.get("workspace_path"):
            wrote = True
            assert Path(payload["workspace_path"]).resolve() == wt.resolve()
    assert wrote, "heal must write workspace_path back"


@pytest.mark.asyncio
async def test_verify_only_keeps_worktree_without_wiping_binding(tmp_path: Path):
    project = tmp_path.resolve()
    wt = _make_tree(project, "A011")
    agent = _make_agent()
    row = _executor_row(ws_path=str(wt))

    with (
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value=str(project)),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.git_worktree._assignee_needs_write_worktree",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(),
        ) as ensure,
    ):
        org = Org.return_value
        org.get_agent = AsyncMock(return_value=row)
        org.update_agent = AsyncMock()
        result = await agent._get_workspace_path()

    assert Path(result).resolve() == wt.resolve()
    ensure.assert_not_awaited()
    _no_wipe(org.update_agent)


@pytest.mark.asyncio
async def test_protected_short_id_when_path_null(tmp_path: Path):
    import aiosqlite

    from hiveweave.services.git_worktree.reconcile import (
        _protected_worktree_short_ids,
    )

    db = tmp_path / ".hiveweave" / "data.db"
    db.parent.mkdir(parents=True)
    conn = await aiosqlite.connect(db)
    await conn.executescript(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, short_id TEXT, status TEXT, workspace_path TEXT
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT,
            is_archived INTEGER DEFAULT 0
        );
        INSERT INTO agents VALUES ('a1', 'A056', 'active', NULL);
        INSERT INTO tasks VALUES ('t1', 'a1', 'approved', 0);
        """
    )
    await conn.commit()
    await conn.close()

    async def _open(_ws):
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
        protected = await _protected_worktree_short_ids(str(tmp_path))

    assert "A056" in protected


@pytest.mark.asyncio
async def test_protected_relocated_binding_does_not_protect_canonical(
    tmp_path: Path,
):
    """Live A024-b must not also protect leftover canonical A024 husk."""
    import aiosqlite

    from hiveweave.services.git_worktree.reconcile import (
        _protected_worktree_short_ids,
    )

    db = tmp_path / ".hiveweave" / "data.db"
    db.parent.mkdir(parents=True)
    conn = await aiosqlite.connect(db)
    await conn.executescript(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, short_id TEXT, status TEXT, workspace_path TEXT
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, assignee_id TEXT, status TEXT,
            is_archived INTEGER DEFAULT 0
        );
        INSERT INTO agents VALUES (
            'a1', 'A024', 'active',
            'D:/proj/.hiveweave/worktrees/A024-b'
        );
        INSERT INTO tasks VALUES ('t1', 'a1', 'approved', 0);
        """
    )
    await conn.commit()
    await conn.close()

    async def _open(_ws):
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
        protected = await _protected_worktree_short_ids(str(tmp_path))

    assert "A024-b" in protected
    assert "A024" not in protected


@pytest.mark.asyncio
async def test_merge_reattaches_unregistered_tree(tmp_path: Path):
    from hiveweave.services.git_worktree.service import GitWorktreeService

    project = tmp_path.resolve()
    wt = _make_tree(project, "A056")
    svc = GitWorktreeService()

    async def _git(args, _cwd):
        if args[:2] == ["worktree", "list"]:
            return True, "D:/other  abc [main]\n"
        if args[0] == "rev-list":
            return True, "1\n"
        return True, ""

    with (
        patch.object(
            svc,
            "_resolve_effective_worktree_path",
            AsyncMock(return_value=str(wt)),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._has_git",
            return_value=True,
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            side_effect=_git,
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._try_reattach_worktree",
            AsyncMock(return_value=True),
        ) as reattach,
        patch(
            "hiveweave.services.git_worktree.service_merge._current_branch",
            AsyncMock(return_value="hw/A056/work"),
        ),
    ):
        result = await svc._validate_merge_preconditions(
            str(project), "A056", "hw/A056/work", "main"
        )

    reattach.assert_awaited_once()
    assert result is None


@pytest.mark.asyncio
async def test_assignee_is_verify_only_true_for_verify_running(tmp_path: Path):
    import aiosqlite

    from hiveweave.services.git_worktree.reconcile import _assignee_is_verify_only

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
            'v1', 'a1', 'running', 'VERIFY: parent', '[]', 0
        );
        """
    )
    await conn.commit()
    await conn.close()

    async def _open(_ws):
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
        assert await _assignee_is_verify_only(str(tmp_path), "A003") is True


@pytest.mark.asyncio
async def test_assignee_is_verify_only_false_for_approved_only(tmp_path: Path):
    import aiosqlite

    from hiveweave.services.git_worktree.reconcile import _assignee_is_verify_only

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
        INSERT INTO agents VALUES ('a1', 'A056', 'active');
        INSERT INTO tasks VALUES (
            't1', 'a1', 'approved', 'Halyard admin API', '[]', 0
        );
        """
    )
    await conn.commit()
    await conn.close()

    async def _open(_ws):
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
        assert await _assignee_is_verify_only(str(tmp_path), "A056") is False
