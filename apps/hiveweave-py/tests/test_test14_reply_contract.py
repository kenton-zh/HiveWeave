"""TEST14 platform fixes: UNREPLIED_ASKS hard gate, WAIT name resolve,
soft-pass no longer suppresses backstop, contract-based pre_check.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.turn_exit import ExitContext, evaluate_turn_exit
from hiveweave.services.turn_result import TURN_RESULT_SCHEMA_VERSION
from hiveweave.services.turn_session import (
    HARD_COMMIT_GATE_CODES,
    classify_commit_gate_soft_warn,
    clear_pending_turn_result,
    filter_soft_passed_violations,
    set_pending_turn_result,
)
from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool

PROJECT_ID = "test14-reply-contract"
CEO = "ceo-t14-" + uuid.uuid4().hex[:8]
COORD = "coord-t14-" + uuid.uuid4().hex[:8]
COORD_NAME = "云岫"
COORD_SHORT = "A003"


@pytest.fixture(autouse=True)
def _clear_turn_session():
    clear_pending_turn_result("agent-t14")
    clear_pending_turn_result(CEO)
    clear_pending_turn_result(COORD)
    yield
    clear_pending_turn_result("agent-t14")
    clear_pending_turn_result(CEO)
    clear_pending_turn_result(COORD)


@pytest.fixture
async def env():
    from hiveweave.services import inbox as inbox_mod
    from hiveweave.services import task as task_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        # Fresh temp DB — clear module migration marks so ALTER runs again
        inbox_mod._migrated.discard(CEO)
        inbox_mod._migrated.discard(COORD)
        task_mod._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            conn = await project_db.ensure_project_db(workspace_path)
            project_db._agent_cache[CEO] = workspace_path
            project_db._agent_cache[COORD] = workspace_path
            for aid, name, short, role, perm in (
                (CEO, "归零", "A001", "ceo", "coordinator"),
                (COORD, COORD_NAME, COORD_SHORT, "前端架构师", "coordinator"),
            ):
                await conn.execute(
                    "INSERT OR REPLACE INTO agents "
                    "(id, project_id, name, short_id, role, permission_type, "
                    "status, parent_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    [
                        aid,
                        PROJECT_ID,
                        name,
                        short,
                        role,
                        perm,
                        CEO if aid == COORD else None,
                    ],
                )
            await conn.commit()
            try:
                yield {
                    "project_id": PROJECT_ID,
                    "workspace": workspace_path,
                    "ceo": CEO,
                    "coord": COORD,
                }
            finally:
                project_db._agent_cache.pop(CEO, None)
                project_db._agent_cache.pop(COORD, None)
                inbox_mod._migrated.discard(CEO)
                inbox_mod._migrated.discard(COORD)
                task_mod._migrated.discard(PROJECT_ID)
                async with project_db._ensure_lock:
                    c = project_db._cache.pop(workspace_path, None)
                if c is not None:
                    try:
                        await c.close()
                    except Exception:
                        pass


# ── P0a: UNREPLIED_ASKS always hard ─────────────────────────


def test_unreplied_asks_is_hard_gate_code():
    assert "UNREPLIED_ASKS" in HARD_COMMIT_GATE_CODES


def test_unreplied_asks_never_soft_passes():
    soft, hard = classify_commit_gate_soft_warn(
        "agent-t14", ["UNREPLIED_ASKS"]
    )
    assert soft == []
    assert hard == ["UNREPLIED_ASKS"]
    soft2, hard2 = classify_commit_gate_soft_warn(
        "agent-t14", ["UNREPLIED_ASKS"]
    )
    assert soft2 == []
    assert hard2 == ["UNREPLIED_ASKS"]


@pytest.mark.asyncio
async def test_commit_turn_unreplied_asks_hard_rejects_first_hit():
    """P0a: first UNREPLIED_ASKS must REJECT and not end_turn."""
    params = CommitTurnParams(
        phase="done_slice",
        summary="hallucinated report to CEO",
    )
    with patch(
        "hiveweave.db.meta.get_agent_project_id",
        new_callable=AsyncMock,
        return_value="proj-t14",
    ), patch(
        "hiveweave.services.turn_exit.pre_check_exit_gates",
        new_callable=AsyncMock,
        return_value=["UNREPLIED_ASKS"],
    ):
        r = await commit_turn_tool(params, "agent-t14", ".")
    assert r.success is False
    assert "REJECTED" in (r.error or "")
    assert "UNREPLIED_ASKS" in (r.error or "")
    assert r.extra.get("end_turn") is not True


# ── P0c: soft-pass does not suppress backstop ───────────────


def test_filter_soft_passed_is_noop():
    classify_commit_gate_soft_warn("agent-t14", ["WAIT_WITHOUT_ASK"])
    kept = filter_soft_passed_violations(
        "agent-t14", ["WAIT_WITHOUT_ASK", "ASSIGNEE_MUST_SUBMIT"]
    )
    assert kept == ["WAIT_WITHOUT_ASK", "ASSIGNEE_MUST_SUBMIT"]


@pytest.mark.asyncio
async def test_evaluate_turn_exit_not_suppressed_by_soft_pass():
    """Soft-pass at pre-check must not strip WAIT_WITHOUT_ASK from backstop."""
    classify_commit_gate_soft_warn("agent-t14", ["WAIT_WITHOUT_ASK"])
    set_pending_turn_result(
        "agent-t14",
        {
            "schema_version": TURN_RESULT_SCHEMA_VERSION,
            "phase": "waiting",
            "summary": "waiting",
            "waiting_on": [{"kind": "agent", "ref": "流火"}],
            "result": {},
            "extensions": {},
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id="agent-t14",
            project_id="proj-t14",
            tool_calls=[],
            pending_inbox_msgs=[],
            unreplied_asks=[],
            open_task_obligations=[],
            tasks_advanced=set(),
            messaged_refs=set(),
            outbound_ask_refs=set(),
            name_by_id={},
        )
    )
    assert "WAIT_WITHOUT_ASK" in decision.violations


def test_wait_without_ask_still_soft_then_hard():
    soft1, hard1 = classify_commit_gate_soft_warn(
        "agent-t14", ["WAIT_WITHOUT_ASK"]
    )
    assert soft1 == ["WAIT_WITHOUT_ASK"]
    assert hard1 == []
    soft2, hard2 = classify_commit_gate_soft_warn(
        "agent-t14", ["WAIT_WITHOUT_ASK"]
    )
    assert soft2 == []
    assert hard2 == ["WAIT_WITHOUT_ASK"]


# ── P0b: name / short_id enrichment in pre_check ────────────


@pytest.mark.asyncio
async def test_pre_check_wait_accepts_flower_name_ref(env):
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="please report module plan",
            message_type="ask",
            expect_report=True,
            wake=False,
        )

    violations = await pre_check_exit_gates(
        CEO,
        PROJECT_ID,
        "waiting",
        waiting_on=[{"kind": "agent", "ref": COORD_NAME}],
    )
    assert "WAIT_WITHOUT_ASK" not in violations


@pytest.mark.asyncio
async def test_pre_check_wait_accepts_short_id_ref(env):
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="ping",
            wake=False,
        )

    violations = await pre_check_exit_gates(
        CEO,
        PROJECT_ID,
        "waiting",
        waiting_on=[{"kind": "agent", "ref": COORD_SHORT}],
    )
    assert "WAIT_WITHOUT_ASK" not in violations


@pytest.mark.asyncio
async def test_pre_check_wait_accepts_uuid_prefix(env):
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="ping",
            wake=False,
        )

    violations = await pre_check_exit_gates(
        CEO,
        PROJECT_ID,
        "waiting",
        waiting_on=[{"kind": "agent", "ref": COORD[:8]}],
    )
    assert "WAIT_WITHOUT_ASK" not in violations


# ── P1a: contract-based unreplied pre_check ─────────────────


@pytest.mark.asyncio
async def test_pre_check_unreplied_survives_mark_read(env):
    """Marking ask as read must NOT clear UNREPLIED_ASKS pre-check."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        sent = await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="need your module breakdown",
            message_type="ask",
            expect_report=True,
            wake=False,
        )
    mid = sent.get("id")
    assert mid
    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    await conn.execute("UPDATE inbox SET read = 1 WHERE id = ?", [mid])
    await conn.commit()

    violations = await pre_check_exit_gates(COORD, PROJECT_ID, "done_slice")
    assert "UNREPLIED_ASKS" in violations


@pytest.mark.asyncio
async def test_pre_check_unreplied_cleared_by_reply_to(env):
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        ask = await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="need plan",
            message_type="ask",
            expect_report=True,
            wake=False,
        )
        contract = ask.get("reply_contract_id")
        assert contract
        await InboxService().send_message(
            from_agent_id=COORD,
            to_agent_id=CEO,
            message="here is the plan M1-M5",
            reply_to=contract,
            wake=False,
        )

    violations = await pre_check_exit_gates(COORD, PROJECT_ID, "done_slice")
    assert "UNREPLIED_ASKS" not in violations


# ── Review follow-ups: archived exempt + waive + P1b ────────


@pytest.mark.asyncio
async def test_pre_check_unreplied_exempts_archived_sender(env):
    """Archived asker must not hard-reject debtor (align with evaluate)."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="ask before dismiss",
            message_type="ask",
            expect_report=True,
            wake=False,
        )

    conn = await project_db.get_project_db_by_project_id(PROJECT_ID)
    await conn.execute(
        "UPDATE agents SET status = 'archived' WHERE id = ?", [CEO]
    )
    await conn.commit()

    violations = await pre_check_exit_gates(COORD, PROJECT_ID, "done_slice")
    assert "UNREPLIED_ASKS" not in violations


@pytest.mark.asyncio
async def test_waive_reply_contracts_clears_pre_check(env):
    """Escape valve must close contracts, not only mark_read."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import pre_check_exit_gates

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        ask = await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="please reply",
            message_type="ask",
            expect_report=True,
            wake=False,
        )
    contract = ask.get("reply_contract_id")
    assert contract

    # mark_read alone is NOT enough after P1a
    await InboxService().mark_read_by_ids(COORD, [ask["id"]])
    assert "UNREPLIED_ASKS" in await pre_check_exit_gates(
        COORD, PROJECT_ID, "done_slice"
    )

    n = await InboxService().waive_reply_contracts(
        COORD,
        [{"contract_id": contract, "to_agent_id": CEO}],
        reason="escape_valve",
    )
    assert n == 1
    assert "UNREPLIED_ASKS" not in await pre_check_exit_gates(
        COORD, PROJECT_ID, "done_slice"
    )


@pytest.mark.asyncio
async def test_watchdog_force_wakes_complete_agent():
    """P1b: force=True must not skip complete debtors."""
    from hiveweave.services.game_time import GameTimeService

    gt = GameTimeService("proj-force")
    calls: list[str] = []

    class FakeInst:
        disposition = "complete"
        project_id = "proj-force"
        config = {"role": "executor"}

    with patch(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        return_value=FakeInst(),
    ), patch(
        "hiveweave.services.task.TaskService.get_actionable_obligations",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "hiveweave.agents.trigger.is_coordinator",
        return_value=False,
    ), patch(
        "hiveweave.agents.trigger.trigger_subordinate",
        new_callable=AsyncMock,
        side_effect=lambda aid: calls.append(aid),
    ):
        await gt._watchdog_trigger("debtor-1")  # skipped
        assert calls == []
        await gt._watchdog_trigger("debtor-1", force=True)
        assert calls == ["debtor-1"]


@pytest.mark.asyncio
async def test_wait_timeout_renudges_outstanding_debtor(env):
    """P1b: expired agent-wait with open ask → ASK_OUTSTANDING + force wake."""
    from hiveweave.services.game_time import GameTimeService
    from hiveweave.services.inbox import InboxService

    async def fake_agent(aid: str):
        return {"id": aid, "status": "active", "name": aid}

    with patch("hiveweave.db.meta.get_agent_by_id", side_effect=fake_agent):
        await InboxService().send_message(
            from_agent_id=CEO,
            to_agent_id=COORD,
            message="report modules",
            message_type="ask",
            expect_report=True,
            wake=False,
        )

    gt = GameTimeService(PROJECT_ID)
    cleared = [
        {"agentId": CEO, "kind": "agent", "ref": COORD_NAME},
    ]
    force_calls: list[tuple] = []
    sent: list[dict] = []

    async def capture_send(**kwargs):
        sent.append(kwargs)
        return {"id": "m1", "should_wake": True}

    with patch(
        "hiveweave.services.wait_contract.wait_contract_service.backfill_null_expires",
        new_callable=AsyncMock,
    ), patch(
        "hiveweave.services.wait_contract.wait_contract_service.clear_expired",
        new_callable=AsyncMock,
        return_value=cleared,
    ), patch(
        "hiveweave.services.wait_contract.wait_contract_service.break_wait_cycles",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "hiveweave.services.org.OrgService.list_agents",
        new_callable=AsyncMock,
        return_value=[
            {"id": CEO, "name": "归零", "short_id": "A001"},
            {
                "id": COORD,
                "name": COORD_NAME,
                "short_id": COORD_SHORT,
            },
        ],
    ), patch.object(
        InboxService,
        "send_message",
        new_callable=AsyncMock,
        side_effect=capture_send,
    ), patch.object(
        gt,
        "_watchdog_trigger",
        new_callable=AsyncMock,
        side_effect=lambda aid, **kw: force_calls.append((aid, kw)),
    ):
        # get_outstanding_ask_recipients must see real DB ask
        await gt._process_wait_contracts(PROJECT_ID)

    assert any(
        "[ASK_OUTSTANDING]" in str(s.get("message") or "") for s in sent
    )
    assert any(
        aid == COORD and kw.get("force") is True for aid, kw in force_calls
    )
    assert any(
        "[WAIT_TIMEOUT]" in str(s.get("message") or "")
        and "ask_outstanding=True" in str(s.get("message") or "")
        for s in sent
    )
