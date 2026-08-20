"""kind=agent wait: notify (no replyTo) wakes and clears only that wait."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.agents.trigger import wake_source_for_pending
from hiveweave.db import project as project_db
from hiveweave.db.project import execute_by_project
from hiveweave.services import inbox as inbox_mod
from hiveweave.services import wait_contract as wait_mod
from hiveweave.services.agent_router import AgentRoute, agent_router
from hiveweave.services.inbox import InboxService
from hiveweave.services.wait_contract import (
    apply_wake_admit_wait_clear,
    event_matches_waits,
    wait_contract_service,
)

PROJECT_ID = "test-wait-notify-clears"
WAITER_ID = "11111111-1111-4111-8111-111111111111"
QINGWU_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "22222222-3333-4333-8333-333333333333"
WAITER_NAME = "蛋炒饭"
QINGWU_NAME = "青梧"
WAITER_SID = "A980"
QINGWU_SID = "A981"
OTHER_SID = "A982"
BASH_REF = "bg-bash-abc"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_mod._migrated.discard(PROJECT_ID)
        for aid in (WAITER_ID, QINGWU_ID, OTHER_ID):
            inbox_mod._migrated.discard(aid)
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            await project_db.ensure_project_db(workspace_path)
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        agent_router.clear_project(PROJECT_ID)
        for aid in (WAITER_ID, QINGWU_ID, OTHER_ID):
            project_db._agent_cache.pop(aid, None)
            inbox_mod._migrated.discard(aid)
        wait_mod._migrated.discard(PROJECT_ID)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_agent(
    pid: str,
    ws: str,
    *,
    agent_id: str,
    name: str,
    short_id: str,
    role: str = "executor",
) -> None:
    now = int(time.time() * 1000)
    await execute_by_project(
        pid,
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        [agent_id, short_id, pid, name, role, now, now],
    )
    agent_router.register(
        AgentRoute(
            agent_id=agent_id,
            project_id=pid,
            workspace_path=ws,
            short_id=short_id,
            name=name,
            role=role,
            status="active",
        )
    )
    project_db._agent_cache[agent_id] = ws


async def _seed_team(env: dict) -> None:
    pid = env["project_id"]
    ws = env["workspace"]
    await _seed_agent(
        pid, ws, agent_id=WAITER_ID, name=WAITER_NAME, short_id=WAITER_SID
    )
    await _seed_agent(
        pid, ws, agent_id=QINGWU_ID, name=QINGWU_NAME, short_id=QINGWU_SID,
        role="coordinator",
    )
    await _seed_agent(
        pid, ws, agent_id=OTHER_ID, name="路人甲", short_id=OTHER_SID
    )


async def _park_agent_and_bash_waits(pid: str, agent_ref: str) -> None:
    await wait_contract_service.replace_waits(
        pid,
        WAITER_ID,
        [
            {"kind": "agent", "ref": agent_ref},
            {"kind": "external", "ref": BASH_REF},
        ],
        phase="waiting",
    )


def _active_by_kind(waits: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for w in waits:
        out.setdefault(str(w.get("kind") or ""), []).append(w)
    return out


@pytest.mark.asyncio
async def test_notify_wakes_and_admit_clears_only_kind_agent_wait(env):
    """Notify without replyTo wakes; admit clears agent wait, not bg-bash."""
    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_NAME)

    inbox = InboxService()
    sent = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="不 waive",
        message_type="notify",
    )
    assert sent.get("should_wake") is True
    assert sent.get("deduped") is not True

    mode = await apply_wake_admit_wait_clear(
        pid,
        WAITER_ID,
        source="message_from_ref",
        from_agent_id=QINGWU_ID,
        trigger=True,
    )
    assert mode == "scoped"
    remaining = await wait_contract_service.list_active(pid, WAITER_ID)
    by_kind = _active_by_kind(remaining)
    assert by_kind.get("agent") in (None, [])
    bash = by_kind.get("external") or []
    assert len(bash) == 1
    assert bash[0]["ref"] == BASH_REF
    assert bash[0].get("clearedAt") is None


@pytest.mark.asyncio
async def test_matching_uses_name_and_short_id_against_uuid_sender(env):
    """Wait.ref as 花名 or A981 matches sender uuid via OrgService resolve."""
    await _seed_team(env)
    pid = env["project_id"]
    inbox = InboxService()

    await _park_agent_and_bash_waits(pid, QINGWU_NAME)
    named = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="不 waive-name",
        message_type="notify",
    )
    assert named.get("should_wake") is True
    assert await event_matches_waits(
        await wait_contract_service.list_active(pid, WAITER_ID),
        event="message_from_ref",
        from_agent_id=QINGWU_ID,
        project_id=pid,
    )

    await wait_contract_service.clear_waits(pid, WAITER_ID)
    await _park_agent_and_bash_waits(pid, QINGWU_SID)
    short = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="不 waive-sid",
        message_type="notify",
    )
    assert short.get("should_wake") is True

    n = await wait_contract_service.clear_kind_agent_waits_for_sender(
        pid, WAITER_ID, QINGWU_ID
    )
    assert n >= 1
    remaining = await wait_contract_service.list_active(pid, WAITER_ID)
    assert all(w.get("kind") != "agent" for w in remaining)
    assert any(w.get("ref") == BASH_REF for w in remaining)


@pytest.mark.asyncio
async def test_prefix_coincidence_does_not_match(env):
    """startswith-style prefix hits must not wake or clear."""
    await _seed_team(env)
    pid = env["project_id"]
    inbox = InboxService()

    # 花名 prefix: ref 青 vs 青梧
    await wait_contract_service.replace_waits(
        pid,
        WAITER_ID,
        [{"kind": "agent", "ref": "青"}],
        phase="waiting",
    )
    prefix_name = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="prefix-name",
        message_type="notify",
    )
    assert prefix_name.get("should_wake") is False
    assert await event_matches_waits(
        await wait_contract_service.list_active(pid, WAITER_ID),
        event="message_from_ref",
        from_agent_id=QINGWU_ID,
        from_agent_name=QINGWU_NAME,
        from_short_id=QINGWU_SID,
        project_id=pid,
    ) is False

    # UUID 8-char prefix of 青梧 vs full uuid (old startswith would hit)
    await wait_contract_service.replace_waits(
        pid,
        WAITER_ID,
        [{"kind": "agent", "ref": QINGWU_ID[:8]}],
        phase="waiting",
    )
    prefix_uuid = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="prefix-uuid",
        message_type="notify",
    )
    assert prefix_uuid.get("should_wake") is False

    # Two uuids sharing an 8-char prefix: wait on OTHER, notify from 青梧
    await wait_contract_service.replace_waits(
        pid,
        WAITER_ID,
        [{"kind": "agent", "ref": OTHER_ID}],
        phase="waiting",
    )
    shared = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="shared-prefix",
        message_type="notify",
    )
    assert shared.get("should_wake") is False
    n = await wait_contract_service.clear_kind_agent_waits_for_sender(
        pid, WAITER_ID, QINGWU_ID
    )
    assert n == 0
    still = await wait_contract_service.list_active(pid, WAITER_ID)
    assert len(still) == 1
    assert still[0]["ref"] == OTHER_ID


@pytest.mark.asyncio
async def test_wake_source_and_park_exemption(env):
    """Pending matching wait → message_from_ref; park must not demote it."""
    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_NAME)

    inbox = InboxService()
    sent = await inbox.send_message(
        from_agent_id=QINGWU_ID,
        to_agent_id=WAITER_ID,
        message="不 waive",
        message_type="notify",
    )
    assert sent.get("should_wake") is True

    pending = await inbox.get_pending_messages(WAITER_ID)
    src = await wake_source_for_pending(
        pending, project_id=pid, waiter_agent_id=WAITER_ID
    )
    assert src == "message_from_ref"

    parked = await inbox.park_pending_wakes(WAITER_ID)
    assert parked == 0
    still = await inbox.get_pending_messages(WAITER_ID)
    assert any(m.get("id") == sent["id"] for m in still)


@pytest.mark.asyncio
async def test_wait_satisfied_does_not_clear_agent_wait(env):
    """wait_satisfied + clear_waits=False leaves kind=agent wait intact."""
    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_ID)
    mode = await apply_wake_admit_wait_clear(
        pid,
        WAITER_ID,
        source="wait_satisfied",
        from_agent_id="system",
        trigger=True,
        clear_waits=False,
    )
    assert mode == "skip"
    remaining = await wait_contract_service.list_active(pid, WAITER_ID)
    kinds = {w["kind"] for w in remaining}
    assert "agent" in kinds
    assert "external" in kinds


@pytest.mark.asyncio
async def test_admit_clears_matching_sender_not_first_inbox_row(env):
    """First unread can be unrelated; matching notify still clears the wait."""
    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_NAME)

    mode_first_only = await apply_wake_admit_wait_clear(
        pid,
        WAITER_ID,
        source="message_from_ref",
        from_agent_id=OTHER_ID,
        trigger=True,
    )
    assert mode_first_only == "scoped"
    still_waiting = await wait_contract_service.list_active(pid, WAITER_ID)
    assert any(w.get("kind") == "agent" for w in still_waiting)

    mode = await apply_wake_admit_wait_clear(
        pid,
        WAITER_ID,
        source="message_from_ref",
        from_agent_id=OTHER_ID,
        from_agent_ids=[QINGWU_ID],
        trigger=True,
    )
    assert mode == "scoped"
    remaining = await wait_contract_service.list_active(pid, WAITER_ID)
    by_kind = _active_by_kind(remaining)
    assert by_kind.get("agent") in (None, [])
    bash = by_kind.get("external") or []
    assert len(bash) == 1
    assert bash[0]["ref"] == BASH_REF


@pytest.mark.asyncio
async def test_message_from_ref_without_senders_does_not_full_clear(env):
    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_NAME)
    mode = await apply_wake_admit_wait_clear(
        pid,
        WAITER_ID,
        source="message_from_ref",
        from_agent_id=None,
        trigger=True,
    )
    assert mode == "skip"
    remaining = await wait_contract_service.list_active(pid, WAITER_ID)
    kinds = {w["kind"] for w in remaining}
    assert "agent" in kinds
    assert "external" in kinds


@pytest.mark.asyncio
async def test_attach_wait_clear_senders_collects_matching_only(env):
    from hiveweave.agents.trigger import _attach_wait_clear_senders
    from hiveweave.services.wait_contract import matching_sender_ids_for_waiter

    await _seed_team(env)
    pid = env["project_id"]
    await _park_agent_and_bash_waits(pid, QINGWU_NAME)
    matched = await matching_sender_ids_for_waiter(
        pid, WAITER_ID, [OTHER_ID, QINGWU_ID]
    )
    assert matched == [QINGWU_ID]

    latch: dict = {}
    await _attach_wait_clear_senders(
        latch,
        [
            {"from_agent_id": OTHER_ID, "message": "noise"},
            {"from_agent_id": QINGWU_ID, "message": "不 waive"},
        ],
        project_id=pid,
        waiter_agent_id=WAITER_ID,
    )
    assert latch.get("wait_clear_sender_ids") == [QINGWU_ID]
