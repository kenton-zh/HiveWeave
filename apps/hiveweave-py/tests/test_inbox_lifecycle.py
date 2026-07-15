"""Inbox: refuse archived recipients; supersede watchdog upserts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.inbox import InboxService


@pytest.mark.asyncio
async def test_send_message_rejects_archived_recipient(monkeypatch):
    svc = InboxService()

    async def fake_get(aid):
        return {"id": aid, "name": "旧人", "status": "archived"}

    with patch("hiveweave.db.meta.get_agent_by_id", new=fake_get):
        with pytest.raises(ValueError, match="archived"):
            await svc.send_message("from-1", "to-archived", "hello")


@pytest.mark.asyncio
async def test_send_message_allows_active_recipient(monkeypatch):
    svc = InboxService()
    executed: list[tuple] = []

    async def fake_get(aid):
        return {"id": aid, "name": "墨白", "status": "active"}

    async def fake_ensure(aid):
        return None

    async def fake_execute(aid, sql, params):
        executed.append((aid, sql, params))

    async def fake_publish(*args, **kwargs):
        return None

    with (
        patch("hiveweave.db.meta.get_agent_by_id", new=fake_get),
        patch("hiveweave.services.inbox._ensure_schema", new=fake_ensure),
        patch("hiveweave.db.project.execute", new=fake_execute),
        patch(
            "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
            new=fake_publish,
        ),
    ):
        msg = await svc.send_message("from-1", "to-active", "hello")

    assert msg["to_agent_id"] == "to-active"
    assert msg["message"] == "hello"
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_supersede_watchdog_marks_prefix_rows():
    svc = InboxService()
    captured: list[tuple] = []

    async def fake_ensure(aid):
        return None

    async def fake_execute(aid, sql, params):
        captured.append((sql, params))

    with (
        patch("hiveweave.services.inbox._ensure_schema", new=fake_ensure),
        patch("hiveweave.db.project.execute", new=fake_execute),
    ):
        n = await svc.supersede_watchdog_messages("agent-1")

    assert n == 1
    sql, params = captured[0]
    assert "read = 1" in sql
    assert params[0] == "agent-1"
    assert "[TASK WATCHDOG]%" in params
    assert "[WATCHDOG]%" in params
    assert "[POST-MERGE VERIFY]%" in params


@pytest.mark.asyncio
async def test_dismiss_acks_inbox_and_closes_tasks():
    """dismiss_agent lifecycle: archive → reassign tasks → mark_all_read."""
    from hiveweave.services.org import OrgService

    org = OrgService()
    calls: list[str] = []

    async def fake_subs(aid):
        return []

    async def fake_get(aid):
        return {
            "id": aid,
            "name": "墨白",
            "parent_id": "arch-1",
            "status": "active",
            "short_id": "A3",
            "permission_type": "executor",
        }

    async def fake_update(aid, attrs):
        calls.append(f"update:{attrs.get('status')}")
        return {**await fake_get(aid), **attrs}

    class FakeConn:
        async def execute(self, sql, params=None):
            calls.append("tasks_sql")
            return None

        async def commit(self):
            calls.append("commit")

    async def fake_get_db(pid):
        return FakeConn()

    async def fake_mark_all(self, aid):
        calls.append(f"inbox_ack:{aid}")

    async def fake_cancel(self, pid, aid):
        calls.append("alarms")

    with (
        patch.object(org, "get_subordinates", new=fake_subs),
        patch.object(org, "get_agent", new=fake_get),
        patch.object(org, "update_agent", new=fake_update),
        patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            new=fake_get_db,
        ),
        patch(
            "hiveweave.services.inbox.InboxService.mark_all_read",
            new=fake_mark_all,
        ),
        patch(
            "hiveweave.services.game_time.GameTimeService.cancel_alarms_for_agent",
            new=fake_cancel,
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService.delete",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            new=AsyncMock(return_value="D:/fake/ws"),
        ),
    ):
        result = await org.dismiss_agent("proj-1", "eng-1")

    assert result.get("success") is True
    assert "update:archived" in calls
    assert "tasks_sql" in calls
    assert "inbox_ack:eng-1" in calls
    assert "alarms" in calls
