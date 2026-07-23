"""TEST11 field-issue fixes: blocked→running wait metadata, WAIT_WITHOUT_ASK,
submit-time reviewer_id, agent wait TTL jitter.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.turn_exit import (
    ExitContext,
    evaluate_turn_exit,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)
from hiveweave.services.wait_contract import default_ttl_ms


PROJECT_ID = "test11-field-fixes"
COORD = "coord-11"
EXEC = "exec-11"
QA = "qa-11"


@pytest.fixture
async def task_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
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
async def test_start_task_from_blocked_clears_wait_metadata(task_env):
    """P0 #5-L1: blocked→running via start_task must clear wait_kind."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "dependency:some blocker")

    blocked = await ts.get_task(pid, tid)
    assert blocked["status"] == "blocked"
    assert blocked.get("wait_kind") == "dependency"
    assert blocked.get("blocked_reason")

    # start_task must redirect to unblock_task and clear metadata
    await ts.start_task(pid, tid)
    running = await ts.get_task(pid, tid)
    assert running["status"] == "running"
    assert running.get("wait_kind") is None
    assert running.get("blocked_reason") is None


@pytest.mark.asyncio
async def test_transition_out_of_blocked_clears_wait_metadata(task_env):
    """P0 #5-L1: _transition leaving blocked clears wait fields."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Work2", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "dependency:x")
    await ts.unblock_task(pid, tid)

    running = await ts.get_task(pid, tid)
    assert running["status"] == "running"
    assert running.get("wait_kind") is None
    assert running.get("blocked_reason") is None


@pytest.mark.asyncio
async def test_submit_pins_reviewer_id_to_creator(task_env):
    """P1 #3: submit_task sets reviewer_id = creator_id by default."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={"tests_passed": True, "test_output": "ok"}
    )

    task = await ts.get_task(pid, tid)
    assert task["status"] == "submitted"
    assert task.get("reviewer_id") == COORD

    # Reviewer sees obligation at submitted (before start_review)
    obs = await ts.get_actionable_obligations(pid, COORD)
    match = [t for t in obs if t["id"] == tid]
    assert match
    assert match[0]["role_hint"] == "reviewer"
    assert match[0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_designated_reviewer_owns_submitted_obligation(task_env):
    """P1 #3: when reviewer ≠ creator, creator does not get review obligation."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid,
        tid,
        evidence={
            "tests_passed": True,
            "test_output": "ok",
            "reviewer_id": QA,
        },
    )

    task = await ts.get_task(pid, tid)
    assert task.get("reviewer_id") == QA

    qa_obs = await ts.get_actionable_obligations(pid, QA)
    assert any(t["id"] == tid and t["role_hint"] == "reviewer" for t in qa_obs)

    coord_obs = await ts.get_actionable_obligations(pid, COORD)
    # Creator should NOT have review duty while designated QA owns it
    assert not any(
        t["id"] == tid and t.get("status") == "submitted" for t in coord_obs
    )


def test_wait_without_ask_blocks_waiting_on_agent():
    """P1 #1a: phase=waiting on agent without message evidence → repair."""
    agent_id = "waiter-1"
    clear_pending_turn_result(agent_id)
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "waiting for peer",
            "waiting_on": [{"kind": "agent", "ref": "peer-1"}],
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id=agent_id,
            project_id="p",
            tool_calls=[],
            messaged_refs=set(),
            outbound_ask_refs=set(),
        )
    )
    assert not decision.ok
    assert "WAIT_WITHOUT_ASK" in decision.violations
    assert decision.should_repair
    clear_pending_turn_result(agent_id)


def test_wait_with_message_evidence_ok():
    """P1 #1a: messaged peer this turn → waiting allowed."""
    agent_id = "waiter-2"
    clear_pending_turn_result(agent_id)
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "asked peer",
            "waiting_on": [{"kind": "agent", "ref": "peer-1"}],
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id=agent_id,
            project_id="p",
            tool_calls=[],
            messaged_refs={"peer-1"},
            outbound_ask_refs=set(),
        )
    )
    assert decision.ok
    clear_pending_turn_result(agent_id)


def test_agent_wait_ttl_has_jitter():
    """P2 #1d: agent TTL varies ±20% by agent_id."""
    base_a = default_ttl_ms("agent", "agent-aaa")
    base_b = default_ttl_ms("agent", "agent-bbb")
    bare = default_ttl_ms("agent")
    assert abs(base_a - bare) <= bare * 0.2 + 1
    assert abs(base_b - bare) <= bare * 0.2 + 1
    # Deterministic
    assert default_ttl_ms("agent", "agent-aaa") == base_a
    # Different agents usually differ (hash collision possible but rare)
    # Just assert both in range
    assert int(bare * 0.8) <= base_a <= int(bare * 1.2)
    assert int(bare * 0.8) <= base_b <= int(bare * 1.2)


@pytest.mark.asyncio
async def test_block_with_depends_on_task_id_merges(task_env):
    """P2 #5-L2: dependsOnTaskId is written into depends_on."""
    ts = TaskService()
    pid = task_env["project_id"]
    blocker = await ts.create_task(
        pid, "Blocker", "desc", creator_id=COORD, assignee_id=EXEC
    )
    tid = await ts.create_task(
        pid, "Dependent", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.block_task(
        pid,
        tid,
        "dependency:waiting on blocker",
        depends_on_task_id=blocker,
    )
    task = await ts.get_task(pid, tid)
    deps = task.get("depends_on") or []
    assert blocker in deps


@pytest.mark.asyncio
async def test_verify_submit_forces_creator_as_reviewer(task_env):
    """Audit H5: VERIFY submit pins reviewer to creator, not evidence QA."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid,
        "VERIFY: Feature",
        "verify",
        creator_id=COORD,
        assignee_id=QA,
        tags=["verify"],
    )
    await ts.claim_task(pid, tid, QA)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid,
        tid,
        evidence={
            "tests_passed": True,
            "test_output": "ok",
            "reviewer_id": QA,  # attempt self-review — must be ignored
        },
    )
    task = await ts.get_task(pid, tid)
    assert task.get("reviewer_id") == COORD


def test_wait_without_ask_ignores_tool_arg_intent_only():
    """Audit M6: tool_calls recipients alone do not satisfy WAIT_WITHOUT_ASK."""
    agent_id = "waiter-3"
    clear_pending_turn_result(agent_id)
    set_pending_turn_result(
        agent_id,
        {
            "phase": "waiting",
            "summary": "intended ask",
            "waiting_on": [{"kind": "agent", "ref": "peer-1"}],
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id=agent_id,
            project_id="p",
            tool_calls=[
                {
                    "function": {
                        "name": "ask_agent",
                        "arguments": {"to": "peer-1", "message": "hi"},
                    }
                }
            ],
            messaged_refs=set(),
            outbound_ask_refs=set(),
        )
    )
    assert not decision.ok
    assert "WAIT_WITHOUT_ASK" in decision.violations
    clear_pending_turn_result(agent_id)


def test_collect_unreplied_asks_ignores_ask_type_without_expect():
    """Required #1: message_type=ask with expect_report=0 is not an obligation.

    After ask-chain downgrade, replies become notify + expect=0; peers must
    not get a new unreplied ask from that.
    """
    from hiveweave.services.turn_exit import collect_unreplied_asks

    pending = [
        {
            "id": "m1",
            "from_agent_id": "peer-a",
            "message_type": "ask",
            "expect_report": 0,
            "message": "reply that was downgraded",
        },
        {
            "id": "m2",
            "from_agent_id": "peer-b",
            "message_type": "ask",
            "expect_report": 1,
            "message": "real ask",
        },
    ]
    unreplied = collect_unreplied_asks(pending, tool_calls=[])
    assert len(unreplied) == 1
    assert unreplied[0]["from_agent_id"] == "peer-b"


@pytest.mark.asyncio
async def test_ask_chain_downgrade_strips_ask_message_type(task_env):
    """Required #1: reply_to + ask → expect=0 and message_type != ask."""
    from hiveweave.db import project as project_db
    from hiveweave.services.inbox import InboxService

    ws = task_env["workspace"]
    # Route inbox writes for both agents into the temp project DB
    project_db._agent_cache[COORD] = ws
    project_db._agent_cache[EXEC] = ws
    await project_db.ensure_project_db(ws)

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    inbox = InboxService()
    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        ask = await inbox.send_message(
            from_agent_id=COORD,
            to_agent_id=EXEC,
            message="please report",
            message_type="ask",
            expect_report=True,
            wake=True,
        )
        contract = ask.get("reply_contract_id")
        assert contract
        assert ask.get("expect_report") is True
        assert (ask.get("message_type") or "").lower() == "ask"

        reply = await inbox.send_message(
            from_agent_id=EXEC,
            to_agent_id=COORD,
            message="done",
            message_type="ask",
            expect_report=True,
            reply_to=contract,
            wake=True,
        )
    assert reply.get("expect_report") is False
    assert (reply.get("message_type") or "").lower() != "ask"
    assert reply.get("reply_contract_id") is None

    project_db._agent_cache.pop(COORD, None)
    project_db._agent_cache.pop(EXEC, None)

@pytest.mark.asyncio
async def test_pre_check_wait_without_ask(task_env):
    """Required #2: pre_check emits WAIT_WITHOUT_ASK for bare agent waits."""
    from hiveweave.services.turn_exit import pre_check_exit_gates

    pid = task_env["project_id"]
    violations = await pre_check_exit_gates(
        EXEC,
        pid,
        "waiting",
        waiting_on=[{"kind": "agent", "ref": "someone-unknown"}],
    )
    assert "WAIT_WITHOUT_ASK" in violations


@pytest.mark.asyncio
async def test_break_wait_cycles_wakes_earliest_waiter(task_env):
    """Medium: asymmetric wake — earliest created_at waiter is wakeFirstId."""
    import time
    import uuid

    from hiveweave.db.project import ensure_project_db
    from hiveweave.services import wait_contract as wc_mod
    from hiveweave.services.wait_contract import wait_contract_service

    pid = task_env["project_id"]
    ws = task_env["workspace"]
    wc_mod._migrated.discard(pid)
    await wait_contract_service.list_all_active(pid)
    conn = await ensure_project_db(ws)
    now = int(time.time() * 1000)
    early, late = COORD, EXEC
    # A↔B mutual agent waits; COORD waited first
    for aid, ref, created in (
        (early, late, now - 60_000),
        (late, early, now - 10_000),
    ):
        await conn.execute(
            "INSERT INTO agent_waits "
            "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
            "created_at, cleared_at) VALUES (?, ?, ?, 'agent', ?, '[]', ?, ?, NULL)",
            [str(uuid.uuid4()), aid, pid, ref, now + 3_600_000, created],
        )
    await conn.commit()

    resolve = lambda r: r if r in (early, late) else None
    breaks = await wait_contract_service.break_wait_cycles(pid, resolve)
    assert len(breaks) == 1
    assert breaks[0]["wakeFirstId"] == early
    assert set(breaks[0]["memberIds"]) == {early, late}
    active = await wait_contract_service.list_all_active(pid)
    assert active == []


@pytest.mark.asyncio
async def test_task_stall_skips_assignee_with_live_wait(task_env):
    """Medium: TASK STALL must not wake assignees holding a live wait contract."""
    import time
    from unittest.mock import AsyncMock

    from hiveweave.db.project import ensure_project_db
    from hiveweave.services import game_time as gt
    from hiveweave.services.game_time import GameTimeService
    from hiveweave.services.wait_contract import wait_contract_service

    pid = task_env["project_id"]
    ts = TaskService()
    tid = await ts.create_task(
        pid, "Stale work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    # Make running age exceed 20min threshold (and duty session)
    old = int(time.time() * 1000) - 40 * 60 * 1000
    conn = await ensure_project_db(task_env["workspace"])
    await conn.execute(
        "UPDATE tasks SET updated_at = ?, created_at = ? WHERE id = ?",
        [old, old, tid],
    )
    await conn.commit()

    await wait_contract_service.replace_waits(
        pid,
        EXEC,
        [{"kind": "agent", "ref": COORD}],
        phase="waiting",
    )

    gt._states[pid] = {
        "project_id": pid,
        "duty_session_started_at_ms": old,
        "ledger_nudge_cooldowns": {},
        "task_stall_counts": {},
    }
    sent: list[str] = []

    async def capture_send(**kwargs):
        sent.append(str(kwargs.get("message") or ""))
        return {"id": "m"}

    svc = GameTimeService()
    with (
        patch(
            "hiveweave.db.meta.query_one",
            new=AsyncMock(return_value={"is_started": 1}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(side_effect=capture_send),
        ),
        patch.object(svc, "_watchdog_trigger", new=AsyncMock()),
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(
                return_value=[
                    {"id": COORD, "parent_id": None},
                    {"id": EXEC, "parent_id": COORD},
                ]
            ),
        ),
    ):
        await svc._nudge_stale_ledger(pid)

    stall_msgs = [m for m in sent if "[TASK STALL]" in m]
    assert stall_msgs == [], f"expected no stall wake, got {stall_msgs}"
    gt._states.pop(pid, None)


@pytest.mark.asyncio
async def test_dead_agent_backfill_from_last_active(task_env):
    """Medium: one-shot activated_at backfill from last_active_at (no wake)."""
    import time
    from unittest.mock import AsyncMock

    from hiveweave.db.project import ensure_project_db
    from hiveweave.services import game_time as gt
    from hiveweave.services.game_time import (
        DEAD_AGENT_THRESHOLD_MS,
        GameTimeService,
    )

    pid = task_env["project_id"]
    ws = task_env["workspace"]
    conn = await ensure_project_db(ws)
    now = int(time.time() * 1000)
    created = now - DEAD_AGENT_THRESHOLD_MS - 60_000
    last_active = now - 30_000
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, status, "
        "created_at, updated_at, last_active_at, activated_at) "
        "VALUES (?, ?, ?, 'executor', 'active', ?, ?, ?, NULL)",
        [EXEC, pid, "Exec", created, created, last_active],
    )
    await conn.commit()

    gt._states[pid] = {"project_id": pid}
    wake_calls: list[str] = []

    async def no_wake(aid: str):
        wake_calls.append(aid)

    svc = GameTimeService()
    with (
        patch(
            "hiveweave.agents.supervisor.agent_manager.get_agent",
            return_value=None,
        ),
        patch.object(svc, "_watchdog_trigger", new=AsyncMock(side_effect=no_wake)),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(return_value={"id": "m"}),
        ),
    ):
        await svc._check_dead_agents(pid)

    row = await (
        await conn.execute(
            "SELECT activated_at FROM agents WHERE id = ?", [EXEC]
        )
    ).fetchone()
    assert row["activated_at"] == last_active
    assert gt._states[pid].get("activated_at_backfilled") is True
    # Backfilled agents are no longer NULL → no restart/wake storm
    assert wake_calls == []
    gt._states.pop(pid, None)


def test_clear_waits_sources_exclude_stall_and_ledger():
    """Medium: stall/ledger wake sources must not clear wait contracts.

    Mirrors agent.chat ``_CLEAR_WAIT_SOURCES`` — stall nudges keep legal waits.
    """
    clear_sources = frozenset({
        "", "user", "chat",
        "wait_timeout", "wait_cycle", "wait_satisfied",
        "message_from_ref",
    })
    for src in (
        "task_stall",
        "ledger",
        "blocked_stale",
        "silent_agent",
        "dead_agent",
        "open_task_reminder",
    ):
        assert src not in clear_sources
