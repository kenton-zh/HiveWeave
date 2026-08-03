"""Wake economics — notify FYI must not wake recipients (TEST18 finding).

Root cause fixed: ``_send_message_core`` never passed ``wake`` to
``inbox.send_message``, so the default (wake=True) applied even to
``notify_agent`` FYI broadcasts — every broadcast started a full LLM turn
in every recipient (TEST18: coordinator's interface-doc broadcast woke 8
leaves; peer handshake messages produced micro-runs).

Decision lives in ``inbox.send_message`` (after auto-close reply_to
resolution):
- message_type="notify", no reply contract → wake=False (background,
  read=1/delivered=0, bundled on next natural wake)
- message_type="notify" replying to an ask contract → wake=True
  (asker is waiting on UNREPLIED_ASKS gate; absorbing would delay 15min+)
- message_type="ask" → wake=True (reply contract)
- other types → product default (any message wakes)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services.inbox import InboxService

PROJECT_ID = "test-wake-econ"
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
async def test_notify_plain_goes_background(env):
    """notify without reply contract → wake=0, read=1, delivered=0 (background)."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID, CEO_ID, "接口文档已更新 v2", message_type="notify"
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


@pytest.mark.asyncio
async def test_notify_replying_to_contract_wakes(env):
    """notify that auto-closes an open ask contract → wake=True (reply, not FYI)."""
    svc = InboxService()

    # CEO asks DEV (creates a reply contract in DEV's inbox, from CEO)
    ask = await svc.send_message(
        CEO_ID, DEV_ID, "请回复测试结果", message_type="ask",
        expect_report=True,
    )
    contract_id = ask["reply_contract_id"]
    assert contract_id

    # DEV replies with notify, WITHOUT explicit reply_to → auto-close fills it
    reply = await svc.send_message(
        DEV_ID, CEO_ID, "测试全部通过", message_type="notify"
    )
    assert reply["should_wake"] is True

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [reply["id"]],
    )
    assert row["read"] == 0
    assert row["wake"] == 1
    assert row["delivered"] == 1

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [reply["id"]]


@pytest.mark.asyncio
async def test_notify_urgent_wakes(env):
    """urgent notify (incident broadcast) must not be absorbed."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID, CEO_ID, "生产故障，请知悉", message_type="notify", priority="urgent"
    )
    assert msg["should_wake"] is True

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 0
    assert row["wake"] == 1
    assert row["delivered"] == 1

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]


@pytest.mark.asyncio
async def test_ask_always_wakes(env):
    """ask carries a reply contract → wake=True, pending channel."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID, CEO_ID, "请回复", message_type="ask"
    )
    assert msg["should_wake"] is True

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 0
    assert row["wake"] == 1
    assert row["delivered"] == 1

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]


@pytest.mark.asyncio
async def test_normal_keeps_product_default(env):
    """normal keeps any-message-wakes product rule → wake=True, pending channel."""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID, CEO_ID, "普通消息"
    )
    assert msg["should_wake"] is True

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 0
    assert row["wake"] == 1
    assert row["delivered"] == 1

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]
