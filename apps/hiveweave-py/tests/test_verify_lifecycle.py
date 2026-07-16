"""VERIFY lifecycle: post-merge claim, no pre-merge thrash, stale nudge heals dead-zone."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.task import TaskService
from hiveweave.services.telemetry import telemetry
from hiveweave.tools.task_tools import (
    VERIFY_STALE_MS,
    _nudge_one_verify_task,
    _spawn_post_approve_verify_task,
    nudge_stale_verify_tasks,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


@pytest.mark.asyncio
async def test_verify_created_not_actionable_pre_merge(task_env):
    """VERIFY must stay invisible to obligations until merge/stale nudge claims it."""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI", "d", creator_id=COORD, assignee_id=EXEC
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
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify", "mandatory"],
    )
    obs = await ts.get_actionable_obligations(pid, EXEC)
    ids = [t["id"] for t in obs]
    assert verify_id not in ids
    assert parent_id not in ids


@pytest.mark.asyncio
async def test_ordinary_created_not_actionable(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    obs = await ts.get_actionable_obligations(pid, EXEC)
    assert all(t["id"] != tid for t in obs)


@pytest.mark.asyncio
async def test_spawn_verify_stays_created(task_env):
    """Spawn leaves VERIFY created — claim happens on merge/stale nudge only."""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI work", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, parent_id, EXEC)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    parent = await ts.get_task(pid, parent_id)

    qa_id = "qa-verify-1"
    with patch(
        "hiveweave.tools.task_tools._find_independent_qa",
        AsyncMock(return_value=qa_id),
    ):
        verify_id = await _spawn_post_approve_verify_task(ts, pid, COORD, parent)
    assert verify_id
    verify = await ts.get_task(pid, verify_id)
    assert verify["status"] == "created"
    assert verify["assignee_id"] == qa_id
    assert verify["assignee_id"] != EXEC
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "verifying"


@pytest.mark.asyncio
async def test_nudge_claims_then_obligation(task_env):
    """Merge/stale nudge claims VERIFY → then it becomes an assignee obligation."""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI", "d", creator_id=COORD, assignee_id=EXEC
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
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )
    verify = await ts.get_task(pid, verify_id)

    with (
        patch(
            "hiveweave.db.meta.get_agent_by_id",
            new=AsyncMock(
                return_value={"id": EXEC, "name": "exec", "status": "active"}
            ),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
    ):
        ok = await _nudge_one_verify_task(pid, "system", verify, reason="merge")

    assert ok is True
    after = await ts.get_task(pid, verify_id)
    assert after["status"] == "claimed"
    obs = await ts.get_actionable_obligations(pid, EXEC)
    assert verify_id in [t["id"] for t in obs]


@pytest.mark.asyncio
async def test_stale_verify_nudge_claims_and_triggers(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI", "d", creator_id=COORD, assignee_id=EXEC
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
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )
    from hiveweave.services import task as task_module
    from hiveweave.tools import task_tools as tt

    old_ms = int(time.time() * 1000) - VERIFY_STALE_MS - 60_000
    await task_module._execute(
        pid, "UPDATE tasks SET updated_at = ? WHERE id = ?", [old_ms, verify_id]
    )
    tt._stale_verify_cooldowns.clear()
    telemetry.reset_counters_for_tests()

    send = AsyncMock()
    trigger = AsyncMock()
    with (
        patch(
            "hiveweave.db.meta.get_agent_by_id",
            new=AsyncMock(
                return_value={"id": EXEC, "name": "exec", "status": "active"}
            ),
        ),
        patch("hiveweave.services.inbox.InboxService.send_message", send),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(return_value=1),
        ),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
    ):
        n = await nudge_stale_verify_tasks(pid, now_ms=int(time.time() * 1000))

    assert n == 1
    assert (await ts.get_task(pid, verify_id))["status"] == "claimed"
    send.assert_awaited()
    kwargs = send.await_args.kwargs
    msg = kwargs.get("message") or ""
    assert "[POST-MERGE VERIFY]" in msg
    assert "Stale" in msg
    trigger.assert_awaited_with(EXEC)
    assert telemetry.snapshot_counters()["verify_stale_nudge"] == 1


@pytest.mark.asyncio
async def test_stale_verify_respects_cooldown(task_env):
    from hiveweave.tools import task_tools as tt
    from hiveweave.services import task as task_module

    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI", "d", creator_id=COORD, assignee_id=EXEC
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
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )
    old_ms = int(time.time() * 1000) - VERIFY_STALE_MS - 60_000
    await task_module._execute(
        pid, "UPDATE tasks SET updated_at = ? WHERE id = ?", [old_ms, verify_id]
    )
    tt._stale_verify_cooldowns.clear()

    with (
        patch(
            "hiveweave.db.meta.get_agent_by_id",
            new=AsyncMock(
                return_value={"id": EXEC, "name": "exec", "status": "active"}
            ),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
    ):
        now = int(time.time() * 1000)
        assert await nudge_stale_verify_tasks(pid, now_ms=now) == 1
        assert await nudge_stale_verify_tasks(pid, now_ms=now + 1000) == 0


@pytest.mark.asyncio
async def test_stale_verify_not_nudged_when_fresh(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await ts.create_task(
        pid, "UI", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, parent_id, EXEC)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    await ts.mark_verifying(pid, parent_id)
    await ts.create_task(
        pid,
        "VERIFY: UI",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify"],
    )

    with patch(
        "hiveweave.tools.task_tools._nudge_one_verify_task",
        new=AsyncMock(return_value=True),
    ) as nudge:
        n = await nudge_stale_verify_tasks(pid)
    assert n == 0
    nudge.assert_not_awaited()
