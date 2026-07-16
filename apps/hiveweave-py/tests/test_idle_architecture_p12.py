"""P1/P2 idle architecture: Wait Contract, merge window, spawn proxy, metrics."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.process_registry import (
    clear_registry_for_tests,
    prepare_spawn_command,
)
from hiveweave.services.telemetry import telemetry
from hiveweave.services.turn_result import WaitingOnItem
from hiveweave.services.wait_contract import (
    WaitContractService,
    category_to_wake_event,
    event_matches_waits,
    obligation_version,
)
from hiveweave.services.wake_policy import should_wake


PROJECT_ID = "test-p12-waits"
AGENT_ID = "agent-wait-1"


@pytest.fixture
async def wait_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())
        # Init minimal project DB
        await project_db.ensure_project_db(workspace_path)

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        from hiveweave.services import wait_contract as wc

        wc._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_wait_contract_persist_and_match(wait_env):
    svc = WaitContractService()
    pid = wait_env["project_id"]
    created = await svc.replace_waits(
        pid,
        AGENT_ID,
        [WaitingOnItem(kind="user", ref="user", note="验收")],
        phase="waiting",
        obligations=[{"id": "t1", "status": "running"}],
    )
    assert len(created) == 1
    assert "user_message" in created[0]["wakeOn"]
    assert created[0]["obligationVersion"] == obligation_version(
        [{"id": "t1", "status": "running"}]
    )

    active = await svc.list_active(pid, AGENT_ID)
    assert len(active) == 1

    assert event_matches_waits(active, event="user_message") is True
    assert event_matches_waits(active, event="ask_reply") is False

    # Wake policy with contracts
    assert (
        should_wake(
            "command",
            disposition="waiting_human",
            from_agent_id="user",
            active_waits=active,
        )
        is True
    )
    assert (
        should_wake(
            "ask",
            disposition="waiting_human",
            from_agent_id="peer-1",
            active_waits=active,
        )
        is False
    )

    n = await svc.clear_waits(pid, AGENT_ID)
    assert n >= 1
    assert await svc.list_active(pid, AGENT_ID) == []


def test_wait_ref_matches_flower_name_not_just_uuid():
    """Regression: commit_turn stores 花名; inbox from_agent_id is UUID."""
    waits = [
        {
            "kind": "agent",
            "ref": "天线",
            "wakeOn": ["ask_reply", "message_from_ref", "timeout"],
        }
    ]
    # UUID alone must NOT match 花名
    assert (
        event_matches_waits(
            waits,
            event="message_from_ref",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
        )
        is False
    )
    # With name → wake
    assert (
        event_matches_waits(
            waits,
            event="message_from_ref",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
            from_agent_name="天线",
            from_short_id="A002",
        )
        is True
    )
    assert (
        should_wake(
            "command",
            disposition="waiting_agent",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
            from_agent_name="天线",
            from_short_id="A002",
            active_waits=waits,
        )
        is True
    )
    # Wrong agent must not wake
    assert (
        should_wake(
            "command",
            disposition="waiting_agent",
            from_agent_id="other-uuid",
            from_agent_name="潮汐",
            from_short_id="A003",
            active_waits=waits,
        )
        is False
    )


def test_command_pierces_timer_external_waits():
    """CEO npm-install nudge must wake blocked 白鹭 despite timer wait."""
    waits = [
        {
            "kind": "timer",
            "ref": "alarm-1",
            "wakeOn": ["alarm", "timeout"],  # legacy contract without message_from_ref
        }
    ]
    assert (
        should_wake(
            "command",
            disposition="blocked",
            from_agent_id="ceo-uuid",
            from_agent_name="归零",
            from_short_id="A001",
            active_waits=waits,
        )
        is True
    )
    assert (
        should_wake(
            "progress",
            disposition="blocked",
            from_agent_id="ceo-uuid",
            from_agent_name="归零",
            active_waits=waits,
        )
        is False
    )

def test_category_to_wake_event():
    assert category_to_wake_event("command", from_agent_id="user") == "user_message"
    assert category_to_wake_event("task_transition") == "task_transition"
    assert category_to_wake_event("ask", from_agent_id="a1") == "ask_reply"


def test_spawn_proxy_rewrites_bare_vite():
    clear_registry_for_tests()
    cmd, env, err = prepare_spawn_command(
        "npx vite --host 0.0.0.0", project_id="p-spawn"
    )
    assert err is None
    assert "--port" in cmd
    assert "5173" not in cmd
    assert "4000" not in cmd
    assert env.get("PORT")
    assert not (env["PORT"] in ("5173", "4000", "4173"))


def test_spawn_proxy_rejects_reserved_explicit():
    cmd, env, err = prepare_spawn_command(
        "npx vite --port 5173", project_id="p-spawn"
    )
    assert err is not None
    assert "5173" in err


def test_telemetry_counters():
    telemetry.reset_counters_for_tests()
    telemetry.agent_wake("a1", "user")
    telemetry.agent_wake("a1", "trigger")
    telemetry.agent_no_progress("a1", streak=2)
    telemetry.inbox_deduped("a1", "progress")
    snap = telemetry.snapshot_counters()
    assert snap["wake_total"] == 2
    assert snap["wake_by_reason"]["user"] == 1
    assert snap["wake_by_reason"]["trigger"] == 1
    assert snap["no_progress_faults"] == 1
    assert snap["inbox_deduped"] == 1
    telemetry.reset_counters_for_tests()


@pytest.mark.asyncio
async def test_merge_window_coalesces_triggers():
    from hiveweave.agents.agent import Agent, AgentState

    agent = object.__new__(Agent)
    agent.id = "merge-1"
    agent.status = AgentState.IDLE
    agent._message_queue = []
    agent._MERGE_WINDOW_MS = 0
    agent._lock = __import__("asyncio").Lock()

    chats: list[tuple] = []

    async def fake_chat(msg, opts=None):
        chats.append((msg, opts or {}))
        return {"ok": True}

    agent.chat = fake_chat  # type: ignore

    await agent.enqueue_wake(
        "wake1",
        {"trigger": True, "inbox_msg_ids": ["m1"], "source": "t"},
    )
    await agent.enqueue_wake(
        "wake2",
        {"trigger": True, "inbox_msg_ids": ["m2", "m1"], "source": "t"},
    )
    await agent.enqueue_wake("user hi", {"trigger": False})

    await Agent._drain_message_queue(agent)

    assert len(chats) == 1
    msg, opts = chats[0]
    assert msg == "wake2"
    assert opts.get("merged_wakes") == 2
    assert opts.get("inbox_msg_ids") == ["m1", "m2"]
    # user still queued
    assert len(agent._message_queue) == 1
    assert agent._message_queue[0][0] == "user hi"


def test_matched_agent_wait_wakes_even_if_waiting_human():
    """Peer reply that satisfies an agent-wait must pierce waiting_human.

    Regression: CEO waited on 潮汐/墨染 while disposition=waiting_human;
    their module-split replies matched Wait Contract but wake_policy still
    returned False → inbox wake=0 → org deadlock.
    """
    waits = [
        {
            "kind": "agent",
            "ref": "潮汐",
            "wake_on": ["ask_reply", "message_from_ref", "timeout"],
            "phase": "waiting",
        }
    ]
    cat = "command"
    assert (
        should_wake(
            cat,
            disposition="waiting_human",
            from_agent_id="uuid-chaoxi",
            from_agent_name="潮汐",
            from_short_id="A003",
            active_waits=waits,
        )
        is True
    )
    # Unrelated peer still blocked
    assert (
        should_wake(
            cat,
            disposition="waiting_human",
            from_agent_id="uuid-other",
            from_agent_name="路人",
            from_short_id="A099",
            active_waits=waits,
        )
        is False
    )
