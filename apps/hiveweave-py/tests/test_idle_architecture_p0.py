"""P0 idle architecture: wake policy, VERIFY lifecycle, reserved ports."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.process_registry import (
    ProcessRecord,
    check_command_reserved_ports,
    clear_registry_for_tests,
    is_reserved_port,
    lookup_by_port,
    register,
)
from hiveweave.services.task import TaskService
from hiveweave.services.wake_policy import classify_message, should_wake


def test_notify_still_wakes():
    cat = classify_message(
        message="任意语言的完成汇报",
        message_type="notify",
        from_agent_id="arch-1",
    )
    assert cat == "message"
    assert should_wake(cat) is True


def test_ask_still_wakes():
    cat = classify_message(
        message="any language please reply",
        expect_report=True,
        message_type="ask",
        from_agent_id="ceo",
    )
    assert cat == "message"
    assert should_wake(cat) is True


def test_waiting_human_allows_peer_notify():
    cat = classify_message(
        message="done",
        message_type="notify",
        from_agent_id="arch-1",
    )
    assert should_wake(cat, disposition="waiting_human", from_agent_id="arch-1") is True


def test_waiting_human_allows_user():
    cat = classify_message(
        message="page is blank",
        from_agent_id="user",
    )
    assert should_wake(cat, disposition="waiting_human", from_agent_id="user") is True


def test_hire_send_message_has_no_category_taxonomy():
    """Platform no longer classifies hire orders vs progress."""
    cat = classify_message(
        message="需招聘 4 人……请报各花名和ID。",
        message_type="normal",
        from_agent_id="ceo",
    )
    assert cat == "message"


def test_reserved_ports():
    assert is_reserved_port(5173)
    assert is_reserved_port(4000)
    assert not is_reserved_port(3000)
    err = check_command_reserved_ports("npx vite --port 5173")
    assert err is not None
    assert "5173" in err
    err2 = check_command_reserved_ports("npm run dev")
    assert err2 is not None
    ok = check_command_reserved_ports("npx vite --port 3000 --strictPort")
    assert ok is None


def test_platform_process_kill_guard():
    """Agents must not kill HiveWeave API/UI (TEST11 taskkill node.exe)."""
    from hiveweave.services.process_registry import check_platform_process_kill
    from hiveweave.tools.bash import _validate_command_safety

    # Wholesale image kill — the real incident
    assert check_platform_process_kill(
        'taskkill //F //IM node.exe 2>/dev/null; echo "done"'
    )
    assert check_platform_process_kill("taskkill /F /IM python.exe")
    assert check_platform_process_kill("Stop-Process -Name node -Force")
    assert check_platform_process_kill("pkill -f uvicorn")
    assert check_platform_process_kill("killall python")

    # Kill by reserved port
    assert check_platform_process_kill(
        'kill -9 $(lsof -ti:4000) 2>/dev/null; echo "killed"'
    )
    assert check_platform_process_kill("npx kill-port 5173")
    assert check_platform_process_kill("fuser -k 4173/tcp")

    # Project ports / unrelated commands still OK
    assert check_platform_process_kill(
        'kill -9 $(lsof -ti:3001) 2>/dev/null; echo "killed"'
    ) is None
    assert check_platform_process_kill("npx kill-port 3000") is None
    assert check_platform_process_kill("echo hello") is None
    assert check_platform_process_kill("lsof -i:3001") is None

    blocked, reason = _validate_command_safety(
        'taskkill //F //IM node.exe 2>/dev/null'
    )
    assert blocked
    assert "5173" in reason or "node" in reason.lower()


def test_process_registry_lookup():
    clear_registry_for_tests()
    register(
        ProcessRecord(
            project_id="p1",
            port=3000,
            pid=123,
            cwd="/tmp/game",
            command="vite --port 3000",
        )
    )
    hits = lookup_by_port(3000)
    assert len(hits) == 1
    assert hits[0].cwd == "/tmp/game"
    clear_registry_for_tests()


PROJECT_ID = "test-verify-lifecycle"
COORD = "coord-1"
EXEC = "exec-1"


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
async def test_actionable_includes_approved_as_creator_merge_duty(task_env):
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
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")

    # Parent is approved (no VERIFY close yet in this unit path)
    parent = await ts.get_task(pid, tid)
    assert parent["status"] == "approved"

    # CREATOR_MUST_MERGE: approved 任务是 creator 的 merge 义务
    creator_obs = await ts.get_actionable_obligations(pid, COORD)
    assert any(t["id"] == tid for t in creator_obs)


@pytest.mark.asyncio
async def test_verify_approve_closes_parent(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, parent_id, EXEC)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    await ts.mark_verifying(pid, parent_id)

    verify_id = await ts.create_task(
        pid,
        "VERIFY: UI work",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )
    await ts.claim_task(pid, verify_id, EXEC)
    await ts.start_task(pid, verify_id)
    await ts.submit_task(
        pid, verify_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, verify_id)
    await ts.review_task(pid, verify_id, "approve")

    verify = await ts.get_task(pid, verify_id)
    parent = await ts.get_task(pid, parent_id)
    assert verify["status"] == "closed"
    assert parent["status"] == "closed"


@pytest.mark.asyncio
async def test_progress_inbox_should_wake(monkeypatch):
    from hiveweave.services.inbox import InboxService

    svc = InboxService()

    async def fake_get(aid):
        return {"id": aid, "name": "归零", "status": "active"}

    async def fake_ensure(aid):
        return None

    inserts: list = []

    async def fake_execute(aid, sql, params):
        inserts.append((sql, params))

    async def fake_query_one(aid, sql, params=None):
        return None

    async def fake_publish(*a, **k):
        return None

    with (
        patch("hiveweave.db.meta.get_agent_by_id", new=fake_get),
        patch("hiveweave.services.inbox._ensure_schema", new=fake_ensure),
        patch("hiveweave.db.project.execute", new=fake_execute),
        patch("hiveweave.db.project.query_one", new=fake_query_one),
        patch(
            "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
            new=fake_publish,
        ),
    ):
        msg = await svc.send_message(
            "arch",
            "ceo",
            "全部完成。290/290 测试通过。三人交付全部批准。",
            message_type="notify",
            recipient_disposition="waiting_human",
        )

    assert msg["should_wake"] is True
    assert msg["category"] == "message"
    # wake=1 and read=0 in insert params
    insert = next(i for i in inserts if "INSERT INTO inbox" in i[0])
    # params: id, from, to, message, read, created, type, expect, priority, task, wake, key
    assert insert[1][4] == 0  # read
    assert insert[1][10] == 1  # wake


@pytest.mark.asyncio
async def test_migrate_orphan_approved(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "Ship UI", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, parent_id, EXEC)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    parent = await ts.get_task(pid, parent_id)
    assert parent["status"] == "approved"

    # No VERIFY child → migrate closes
    result = await ts.migrate_orphan_approved(pid)
    assert result["closed"] >= 1
    parent = await ts.get_task(pid, parent_id)
    assert parent["status"] == "closed"


@pytest.mark.asyncio
async def test_gate_preserves_inbox_ids_in_retrigger_opts():
    """Repair retrigger must carry inbox_msg_ids into chat opts."""
    import asyncio

    from hiveweave.agents.agent import Agent, AgentState

    captured: list[dict] = []
    agent = object.__new__(Agent)
    agent.id = "a1"
    agent.status = AgentState.IDLE
    agent._in_resume_cooldown = lambda: False  # type: ignore

    async def fake_chat(msg, opts=None):
        captured.append(opts or {})

    agent.chat = fake_chat  # type: ignore

    with (
        patch("hiveweave.agents.agent.SELF_RETRIGGER_DELAY_MS", 0),
        patch("asyncio.sleep", return_value=None),
    ):
        tasks: list = []

        def capture_task(coro, name=None):
            t = asyncio.ensure_future(coro)
            tasks.append(t)
            return t

        with patch("asyncio.create_task", side_effect=capture_task):
            await Agent._retrigger_for_turn_gate(
                agent,
                "fix me",
                inbox_msg_ids=["m1", "m2"],
            )
            if tasks:
                await asyncio.gather(*tasks)

    assert captured
    assert captured[0].get("inbox_msg_ids") == ["m1", "m2"]
    assert captured[0].get("trigger") is True


def test_no_progress_fingerprint_stable():
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    fp1 = Agent._compute_progress_fingerprint(
        agent,
        [{"id": "t1", "status": "in_progress"}],
        [],
        set(),
    )
    fp2 = Agent._compute_progress_fingerprint(
        agent,
        [{"id": "t1", "status": "in_progress"}],
        [],
        set(),
    )
    fp3 = Agent._compute_progress_fingerprint(
        agent,
        [{"id": "t1", "status": "submitted"}],
        [],
        set(),
    )
    assert fp1 == fp2
    assert fp1 != fp3


def test_turn_exit_never_continues_unlimited():
    from hiveweave.services.turn_exit import ExitContext, evaluate_turn_exit
    from hiveweave.services.turn_session import (
        clear_pending_turn_result,
        pop_pending_turn_result,
        set_pending_turn_result,
    )

    aid = "agent-no-continue"
    clear_pending_turn_result(aid)
    set_pending_turn_result(
        aid,
        {
            "schema_version": 1,
            "phase": "in_progress",
            "summary": "still going",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    try:
        decision = evaluate_turn_exit(
            ExitContext(
                agent_id=aid,
                project_id="p",
                tool_calls=[],
                pending_inbox_msgs=[],
                unreplied_asks=[],
                open_task_obligations=[{"id": "t1"}],
                tasks_advanced=set(),
            )
        )
        assert decision.ok
        assert decision.continue_work is False
    finally:
        pop_pending_turn_result(aid)
