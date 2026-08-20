"""ledger.mine vs ledger.scope: empty mine ≠ org done (DSH_06)."""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.agent_router import agent_router
from hiveweave.services.org import OrgService
from hiveweave.services.platform_state import (
    LEDGER_SCOPE_RULE,
    build_platform_state,
    format_platform_state,
)

PROJECT_ID = "platform-state-scope-proj"
CEO_ID = "scope-ceo-0001"
MID_ID = "scope-mid-0001"
NESTED_ID = "scope-nested-01"
LEAF_ID = "scope-leaf-0001"
BLOCKED_ID = "blocked-p3-0001"
CANCELLED_ID = "cancelled-x-01"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_ws),
            patch(
                "hiveweave.agents.supervisor.agent_manager.get_agent",
                return_value=None,
            ),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        agent_router.clear_project(PROJECT_ID)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_org(ws: str) -> None:
    org = OrgService()
    await org.create_agent(
        {
            "id": CEO_ID,
            "project_id": PROJECT_ID,
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "status": "active",
            "short_id": "A001",
            "workspace_path": ws,
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": MID_ID,
            "project_id": PROJECT_ID,
            "name": "青梧",
            "role": "coordinator",
            "permission_type": "coordinator",
            "parent_id": CEO_ID,
            "status": "active",
            "short_id": "A002",
            "workspace_path": ws,
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": NESTED_ID,
            "project_id": PROJECT_ID,
            "name": "星野",
            "role": "coordinator",
            "permission_type": "coordinator",
            "parent_id": MID_ID,
            "status": "active",
            "short_id": "A003",
            "workspace_path": ws,
        },
        bootstrap=True,
    )
    await org.create_agent(
        {
            "id": LEAF_ID,
            "project_id": PROJECT_ID,
            "name": "蛋炒饭",
            "role": "签到排行榜工程师",
            "permission_type": "executor",
            "parent_id": NESTED_ID,
            "status": "active",
            "short_id": "A004",
            "workspace_path": ws,
        },
        bootstrap=True,
    )


async def _insert_task(
    ws: str,
    task_id: str,
    *,
    title: str,
    status: str,
    creator_id: str,
    assignee_id: str | None = None,
    updated_at: int | None = None,
    depends_on: str | None = None,
    policy_id: str | None = None,
):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    ts = now if updated_at is None else updated_at
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, depends_on, policy_id, created_at, updated_at, is_archived)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        [
            task_id,
            PROJECT_ID,
            title,
            creator_id,
            assignee_id,
            status,
            depends_on,
            policy_id,
            ts,
            ts,
        ],
    )
    await conn.commit()


async def _insert_inbox(
    ws: str,
    *,
    to_agent_id: str,
    from_agent_id: str,
    message: str,
    message_type: str,
    task_id: str | None,
    read: int = 0,
):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read,"
        " created_at, message_type, task_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            str(uuid.uuid4()),
            from_agent_id,
            to_agent_id,
            message,
            read,
            now,
            message_type,
            task_id,
        ],
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_ceo_mine_empty_scope_contains_blocked(env):
    ws = env["workspace_path"]
    await _seed_org(ws)
    await _insert_task(
        ws,
        BLOCKED_ID,
        title="P3 blocked",
        status="blocked",
        creator_id=NESTED_ID,
        assignee_id=LEAF_ID,
        depends_on='["other-task-01"]',
        policy_id="code_audit_unit",
    )
    await _insert_task(
        ws,
        CANCELLED_ID,
        title="cancelled leftover",
        status="cancelled",
        creator_id=MID_ID,
        assignee_id=LEAF_ID,
    )

    snap = await build_platform_state(agent_id=CEO_ID, project_id=PROJECT_ID)
    ledger = snap["ledger"]
    assert ledger["mine"] == []
    assert ledger["obligations"] == []
    scope = ledger["scope"]
    blocked = [t for t in scope if t.get("status") == "blocked"]
    assert blocked, f"CEO scope missing blocked task: {scope}"
    assert blocked[0]["title"] == "P3 blocked"
    assert CANCELLED_ID[:12] not in {t.get("id") for t in scope}

    text = format_platform_state(snap)
    assert LEDGER_SCOPE_RULE in text
    assert "ledger.mine empty" in text


@pytest.mark.asyncio
async def test_scope_truncation_keeps_blocked_visible(env):
    ws = env["workspace_path"]
    await _seed_org(ws)
    now = int(time.time() * 1000)
    for i in range(41):
        await _insert_task(
            ws,
            f"filler-{i:02d}-{uuid.uuid4().hex[:8]}",
            title=f"filler {i}",
            status="running",
            creator_id=MID_ID,
            assignee_id=LEAF_ID,
            updated_at=now + i,
        )
    await _insert_task(
        ws,
        BLOCKED_ID,
        title="oldest blocked",
        status="blocked",
        creator_id=NESTED_ID,
        assignee_id=LEAF_ID,
        updated_at=now - 1_000_000,
    )

    snap = await build_platform_state(agent_id=CEO_ID, project_id=PROJECT_ID)
    ledger = snap["ledger"]
    assert ledger["scope_truncated"] is True
    blocked_in_page = any(t.get("status") == "blocked" for t in ledger["scope"])
    blocked_count = int((ledger.get("scope_status_counts") or {}).get("blocked") or 0)
    assert blocked_in_page or blocked_count > 0


@pytest.mark.asyncio
async def test_named_tasks_uses_structured_task_id_only(env):
    ws = env["workspace_path"]
    await _seed_org(ws)
    await _insert_task(
        ws,
        BLOCKED_ID,
        title="P3 blocked",
        status="blocked",
        creator_id=NESTED_ID,
        assignee_id=LEAF_ID,
    )
    await _insert_inbox(
        ws,
        to_agent_id=CEO_ID,
        from_agent_id=MID_ID,
        message="please look",
        message_type="ask",
        task_id=BLOCKED_ID,
    )
    await _insert_inbox(
        ws,
        to_agent_id=CEO_ID,
        from_agent_id=MID_ID,
        message=f"body-only mention of {BLOCKED_ID} with no column",
        message_type="notify",
        task_id=None,
    )

    snap = await build_platform_state(agent_id=CEO_ID, project_id=PROJECT_ID)
    named = snap["inbox"]["named_tasks"]
    assert any(
        n.get("task_id") == BLOCKED_ID and n.get("message_type") == "ask"
        for n in named
    )
    assert not any(n.get("message_type") == "notify" for n in named)


@pytest.mark.asyncio
async def test_mid_sees_descendant_assignee_via_get_all_descendants(env):
    """Grandchild assignee is visible to mid; get_subordinates (one level) would miss it."""
    ws = env["workspace_path"]
    await _seed_org(ws)
    await _insert_task(
        ws,
        BLOCKED_ID,
        title="leaf blocked",
        status="blocked",
        creator_id=NESTED_ID,
        assignee_id=LEAF_ID,
    )

    snap = await build_platform_state(agent_id=MID_ID, project_id=PROJECT_ID)
    assert snap["ledger"]["mine"] == []
    titles = [t.get("title") for t in snap["ledger"]["scope"]]
    assert "leaf blocked" in titles


def test_orphan_non_ceo_does_not_get_project_scope():
    from hiveweave.services.platform_state import _viewer_sees_project_scope

    assert _viewer_sees_project_scope(
        {"role": "executor", "parent_id": None, "permission_type": "readwrite"}
    ) is False
    assert _viewer_sees_project_scope(
        {"role": "hr", "parent_id": "", "permission_type": "readonly"}
    ) is False
    assert _viewer_sees_project_scope(
        {"role": "ceo", "parent_id": None, "permission_type": "coordinator"}
    ) is True
