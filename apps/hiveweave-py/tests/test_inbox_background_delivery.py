"""Inbox delivery — progress messages wake (product rule).

Explicit wake=False still parks as background for rare system use.
Progress idempotency collapses duplicate FYI from the same sender.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services.inbox import InboxService

PROJECT_ID = "test-bg-delivery"
CEO_ID = "test-ceo"
DEV_ID = "test-dev"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (CEO_ID, DEV_ID) else None

        async def fake_get_agent_by_id(aid: str):
            return {"id": aid, "name": "x", "status": "active"}

        async def fake_publish(*args, **kwargs):
            return None

        inbox_module._migrated.discard(CEO_ID)
        inbox_module._migrated.discard(DEV_ID)
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch(
                "hiveweave.realtime.event_bus.status_event_bus"
                ".publish_chat_message",
                fake_publish,
            ),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)


async def _fetch_one(env, sql, params):
    conn = await project_db.ensure_project_db(env["workspace_path"])
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


@pytest.mark.asyncio
async def test_progress_message_wakes_via_pending_channel(env):
    """notify → message category (no taxonomy), should_wake=True, pending channel."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID,
        CEO_ID,
        "穷举验证 1720 节点全部完成，X_win=0",
        message_type="notify",
    )
    assert msg["should_wake"] is True
    assert msg["category"] == "message"

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 0
    assert row["wake"] == 1
    assert row["delivered"] == 1

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]
    assert await svc.get_undelivered_background(CEO_ID) == []

    await svc.mark_read_by_ids(CEO_ID, [msg["id"]])
    assert await svc.get_pending_messages(CEO_ID) == []


@pytest.mark.asyncio
async def test_explicit_wake_false_still_background(env):
    """Explicit wake=False keeps background piggyback path."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID,
        CEO_ID,
        "穷举验证 1720 节点全部完成，X_win=0",
        wake=False,
    )
    assert msg["should_wake"] is False

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 1
    assert row["wake"] == 0
    assert row["delivered"] == 0

    assert await svc.get_pending_messages(CEO_ID) == []
    bg = await svc.get_undelivered_background(CEO_ID)
    assert [m["id"] for m in bg] == [msg["id"]]

    await svc.mark_read_by_ids(CEO_ID, [msg["id"]])
    row = await _fetch_one(
        env, "SELECT delivered FROM inbox WHERE id = ?", [msg["id"]]
    )
    assert row["delivered"] == 1
    assert await svc.get_undelivered_background(CEO_ID) == []


@pytest.mark.asyncio
async def test_progress_collapse_dedupes_pending(env):
    """幂等键按内容哈希：同内容重发收敛，不同内容各自进 pending。"""
    svc = InboxService()
    m1 = await svc.send_message(
        DEV_ID, CEO_ID, "全部完成 1/3", message_type="notify"
    )
    m2 = await svc.send_message(
        DEV_ID, CEO_ID, "全部完成 1/3", message_type="notify"
    )

    pending = await svc.get_pending_messages(CEO_ID)
    assert len(pending) == 1
    assert pending[0]["id"] == m1["id"] == m2["id"]
    assert m2.get("deduped") is True

    m3 = await svc.send_message(
        DEV_ID, CEO_ID, "全部完成 2/3", message_type="notify"
    )
    pending = await svc.get_pending_messages(CEO_ID)
    assert len(pending) == 2
    assert m3.get("deduped") is not True

    await svc.mark_read_by_ids(CEO_ID, [m["id"] for m in pending])
    assert await svc.get_pending_messages(CEO_ID) == []


@pytest.mark.asyncio
async def test_command_message_uses_wake_channel_not_background(env):
    """command/ask 消息走 wake 通道，不进 background 通道。"""
    svc = InboxService()
    msg = await svc.send_message(DEV_ID, CEO_ID, "请在本轮明确二选一回复")

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]
    assert await svc.get_undelivered_background(CEO_ID) == []
