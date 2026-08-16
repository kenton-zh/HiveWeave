"""VERIFY lifecycle: post-merge claim, no pre-merge thrash, stale nudge heals dead-zone."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.task import TaskService
from hiveweave.services.telemetry import telemetry
from hiveweave.tools.task_tools import (
    VERIFY_STALE_MS,
    _nudge_one_verify_task,
    _spawn_post_approve_verify_task,
    nudge_pending_verify_tasks,
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
        source="system",
    )
    obs = await ts.get_actionable_obligations(pid, EXEC)
    ids = [t["id"] for t in obs]
    assert verify_id not in ids
    assert parent_id not in ids


@pytest.mark.asyncio
async def test_assigned_is_claimed_and_actionable(task_env):
    """Assign = claim: create with assignee → claimed → assignee obligation."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    task = await ts.get_task(pid, tid)
    assert task["status"] == "claimed"
    assert task["assignee_id"] == EXEC
    assert task["claimed_at"] is not None
    obs = await ts.get_actionable_obligations(pid, EXEC)
    assert tid in [t["id"] for t in obs]


@pytest.mark.asyncio
async def test_unassigned_draft_stays_created(task_env):
    """No assignee → draft stays created (claim_task still picks these up)."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Draft", "d", creator_id=COORD, assignee_id=None
    )
    task = await ts.get_task(pid, tid)
    assert task["status"] == "created"
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
        "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
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
async def test_spawn_skips_when_parent_assignee_is_qa(task_env):
    """QA's own delivery must not mint VERIFY-of-the-test-suite for a non-QA."""
    from hiveweave.services.tasks.verify import is_verify_title

    ts = TaskService()
    pid = task_env["project_id"]
    qa_id = "qa-delivery-1"
    parent_id = await ts.create_task(
        pid, "Integration suite", "d", creator_id=COORD, assignee_id=qa_id
    )
    await ts.claim_task(pid, parent_id, qa_id)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    parent = await ts.get_task(pid, parent_id)

    async def fake_get(aid, *a, **k):
        aid = str(aid)
        if aid == qa_id:
            return {
                "id": qa_id,
                "role": "qa_engineer",
                "status": "active",
                "permission_type": "executor",
            }
        return {
            "id": aid,
            "role": "executor",
            "status": "active",
            "permission_type": "executor",
        }

    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(side_effect=fake_get),
    ):
        verify_id = await _spawn_post_approve_verify_task(ts, pid, COORD, parent)

    assert verify_id is None
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "closed"
    kids = [
        t
        for t in await ts.list_tasks(pid, include_archived=True)
        if t.get("parent_task_id") == parent_id
    ]
    assert not any(is_verify_title(t.get("title")) for t in kids)


async def _approve_task(ts, pid, assignee, title):
    parent_id = await ts.create_task(
        pid, title, "d", creator_id=COORD, assignee_id=assignee
    )
    await ts.claim_task(pid, parent_id, assignee)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    return parent_id


def _agent_rows(*pairs: tuple[str, str]):
    """pairs of (agent_id, role)."""
    table = {aid: role for aid, role in pairs}

    async def fake_get(aid, *a, **k):
        aid = str(aid)
        role = table.get(aid, "executor")
        return {
            "id": aid,
            "role": role,
            "status": "active",
            "permission_type": "executor",
        }

    return fake_get


@pytest.mark.asyncio
async def test_spawn_still_when_executor_title_looks_like_tests(task_env):
    """Do not skip VERIFY by scanning the parent title."""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _approve_task(ts, pid, EXEC, "测试套件 Integration suite")
    parent = await ts.get_task(pid, parent_id)
    qa_id = "qa-verify-title"
    with (
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(side_effect=_agent_rows((EXEC, "executor"))),
        ),
        patch(
            "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
            AsyncMock(return_value=qa_id),
        ),
    ):
        verify_id = await _spawn_post_approve_verify_task(ts, pid, COORD, parent)
    assert verify_id
    verify = await ts.get_task(pid, verify_id)
    assert (verify.get("title") or "").startswith("VERIFY:")
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "verifying"


@pytest.mark.asyncio
async def test_spawn_still_when_executor_implementer_reassigned_to_qa(task_env):
    """Later QA assignee must not skip VERIFY for executor-written work."""
    from hiveweave.services import task as task_module

    ts = TaskService()
    pid = task_env["project_id"]
    qa_id = "qa-later-assignee"
    parent_id = await _approve_task(ts, pid, EXEC, "Feature")
    await task_module._execute(
        pid,
        "UPDATE tasks SET assignee_id = ? WHERE id = ?",
        [qa_id, parent_id],
    )
    parent = await ts.get_task(pid, parent_id)
    assert parent.get("implementer_id") == EXEC
    assert parent.get("assignee_id") == qa_id
    with (
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(
                side_effect=_agent_rows((EXEC, "executor"), (qa_id, "qa_engineer"))
            ),
        ),
        patch(
            "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
            AsyncMock(return_value="qa-independent"),
        ),
    ):
        verify_id = await _spawn_post_approve_verify_task(ts, pid, COORD, parent)
    assert verify_id
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "verifying"


@pytest.mark.asyncio
async def test_spawn_keeps_existing_verify_child_for_qa_parent(task_env):
    """Open VERIFY child wins over QA-delivery skip (do not close parent under it)."""
    ts = TaskService()
    pid = task_env["project_id"]
    qa_id = "qa-delivery-existing"
    parent_id = await _approve_task(ts, pid, qa_id, "Integration suite")
    child = await ts.create_task(
        pid,
        "VERIFY: Integration suite",
        "verify",
        creator_id=COORD,
        assignee_id=qa_id,
        parent_task_id=parent_id,
        tags=["verify", "mandatory"],
        source="system",
    )
    parent = await ts.get_task(pid, parent_id)
    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(side_effect=_agent_rows((qa_id, "qa_engineer"))),
    ):
        verify_id = await _spawn_post_approve_verify_task(ts, pid, COORD, parent)
    assert verify_id == child
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "verifying"
    assert parent2["status"] != "closed"


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
        source="system",
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
        source="system",
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
        source="system",
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
        source="system",
    )

    with patch(
        "hiveweave.tools.tasks.verify_merge._nudge_one_verify_task",
        new=AsyncMock(return_value=True),
    ) as nudge:
        n = await nudge_stale_verify_tasks(pid)
    assert n == 0
    nudge.assert_not_awaited()


async def _make_verify(pid, ts, title="UI"):
    """Parent approved + VERIFY child created (post-merge state)."""
    parent_id = await ts.create_task(
        pid, title, "d", creator_id=COORD, assignee_id=EXEC
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
        f"VERIFY: {title}",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=["verify", "mandatory"],
        source="system",
    )
    return verify_id


async def _nudge_with_mocks(pid, task):
    return await _nudge_one_verify_task(pid, "system", task, reason="merge")


@pytest.mark.asyncio
async def test_verify_nudge_serialized_while_another_in_flight(task_env):
    """并发验收串行化（issue #6）+ TOCTOU（审计 M1/S2）：并发 nudge 两个 VERIFY，恰一个成功。

    用 asyncio.gather 真实并发唤醒 A、B —— per-project 锁必须保证 check+claim
    原子：锁被取走前进入的第二个协程在锁释放后再检查，看到 A 已 claimed
    （in-flight）即被拒。断言恰有 1 个成功、恰 1 个保持 created，防 TOCTOU 回归。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")

    first = await ts.get_task(pid, first_id)
    second = await ts.get_task(pid, second_id)
    assert second["status"] == "created"

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
        (r1, r2) = await asyncio.gather(
            _nudge_with_mocks(pid, first),
            _nudge_with_mocks(pid, second),
        )
    # 恰一个唤醒成功，另一个被锁拒
    assert sorted([bool(r1), bool(r2)]) == [False, True]
    claimed_ids = [
        (await ts.get_task(pid, i))["status"] == "claimed"
        for i in (first_id, second_id)
    ]
    assert sum(claimed_ids) == 1


@pytest.mark.asyncio
async def test_verify_nudge_blocked_with_wake_path_holds_lock(task_env):
    """验收串行化（审计#1 + 2026-08-11 死锁复盘）：blocked 且有 assignee 且
    **有自动解封路径**（depends_on 非空）的 VERIFY 算 in-flight。

    运行中被阻塞（game_time 可自动 unblock 恢复）仍占用 MAIN 运行时，
    泵/下一个 nudge 不得唤醒第二个 VERIFY 造成双并发（issue #6）。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    blocker_id = await ts.create_task(
        pid, "Blocker", "d", creator_id=COORD, assignee_id=EXEC
    )

    first = await ts.get_task(pid, first_id)
    second = await ts.get_task(pid, second_id)
    # 第一 VERIFY 唤醒后进入 running（再被阻塞，assignee 保留 + 有依赖路径）
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
        assert await _nudge_with_mocks(pid, first) is True
    await ts.start_task(pid, first_id)
    await ts.block_task(
        pid, first_id, "外部依赖：等 blocker", depends_on_task_id=blocker_id
    )

    blocked = await ts.get_task(pid, first_id)
    assert blocked["status"] == "blocked"
    assert blocked["assignee_id"] == EXEC
    assert blocked["wait_kind"] == "dependency"  # deps 存在 → 结构化推断
    assert blocker_id in (blocked.get("depends_on") or [])

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
        assert await _nudge_with_mocks(pid, second) is False
    assert (await ts.get_task(pid, second_id))["status"] == "created"


@pytest.mark.asyncio
async def test_verify_nudge_blocked_parked_does_not_hold_lock(task_env):
    """2026-08-11 slack-clone_01 死锁回归：blocked + assignee 但**无自动解封
    路径**（depends_on 空、非 timer）的 VERIFY 是 parked 死区 —— 不占锁。

    曾导致：reconcile 永不 unblock、泵不敢放行、QA 被 claim 门禁指示
    commit_turn(waiting) 后永久等待 —— 全队 VERIFY 队列冻结 1h40m。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")

    first = await ts.get_task(pid, first_id)
    second = await ts.get_task(pid, second_id)
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
        assert await _nudge_with_mocks(pid, first) is True
    await ts.start_task(pid, first_id)
    await ts.block_task(
        pid, first_id, "归零策略：等全部 ROUND2 合并后在最新 tip 批量验收"
    )

    blocked = await ts.get_task(pid, first_id)
    assert blocked["status"] == "blocked"
    assert blocked["assignee_id"] == EXEC
    assert not (blocked.get("depends_on") or [])

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
        assert await _nudge_with_mocks(pid, second) is True
    assert (await ts.get_task(pid, second_id))["status"] == "claimed"


@pytest.mark.asyncio
async def test_verify_nudge_blocked_without_assignee_does_not_hold_lock(task_env):
    """必改（审计#1）：blocked 且无 assignee 的 VERIFY 是 QA 死区，不占锁。"""
    from hiveweave.services import task as task_module

    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")

    second = await ts.get_task(pid, second_id)
    # 模拟 spawn 时无独立 QA：blocked + assignee=NULL 的死区状态
    await task_module._execute(
        pid,
        "UPDATE tasks SET status = 'blocked', assignee_id = NULL, "
        "blocked_reason = 'verify_no_qa', updated_at = ? WHERE id = ?",
        [int(time.time() * 1000), first_id],
    )
    first = await ts.get_task(pid, first_id)
    assert first["status"] == "blocked"
    assert first["assignee_id"] is None

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
        assert await _nudge_with_mocks(pid, second) is True
    assert (await ts.get_task(pid, second_id))["status"] == "claimed"


@pytest.mark.asyncio
async def test_verify_nudge_rework_holds_lock(task_env):
    """必改（审计#1）：rework（VR 复审通过后回退重验）仍算 in-flight。"""
    from hiveweave.services import task as task_module

    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")

    first = await ts.get_task(pid, first_id)
    second = await ts.get_task(pid, second_id)
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
        assert await _nudge_with_mocks(pid, first) is True
    # claimed → running → blocked... rework 直接构造：行名走状态机外纠偏
    await task_module._execute(
        pid,
        "UPDATE tasks SET status = 'rework', updated_at = ? WHERE id = ?",
        [int(time.time() * 1000), first_id],
    )
    rework = await ts.get_task(pid, first_id)
    assert rework["status"] == "rework"

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
        assert await _nudge_with_mocks(pid, second) is False
    assert (await ts.get_task(pid, second_id))["status"] == "created"


@pytest.mark.asyncio
async def test_verify_pump_no_candidate_idempotent(task_env):
    """审计：泵在无候选（无 created+assignee VERIFY）时幂等返回 0，不抛错。"""
    ts = TaskService()
    pid = task_env["project_id"]
    assert await nudge_pending_verify_tasks(pid) == 0


@pytest.mark.asyncio
async def test_verify_pump_wakes_next_after_first_closes(task_env):
    """验收串行化泵：前置 VERIFY 收口后，队列中最老的 created VERIFY 被唤醒。"""
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")

    first = await ts.get_task(pid, first_id)
    second = await ts.get_task(pid, second_id)

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
        # 唤醒首个 → claimed（in-flight）
        assert await _nudge_with_mocks(pid, first) is True
        # 前置 in-flight 期间泵不推进
        assert await nudge_pending_verify_tasks(pid) == 0
        # 前置收口：VERIFY 走完 submit → review approve → 自动 close（真实路径）
        await ts.start_task(pid, first_id)
        await ts.submit_task(
            pid, first_id, evidence={"tests_passed": True, "test_output": "ok"}
        )
        await ts.start_review(pid, first_id)
        await ts.review_task(pid, first_id, "approve")
        assert (await ts.get_task(pid, first_id))["status"] == "closed"
        # 锁释放 → 收口路径内嵌泵已唤醒第二个；再手动泵无可推进（幂等）
        assert await nudge_pending_verify_tasks(pid) == 0
    assert (await ts.get_task(pid, second_id))["status"] == "claimed"


def _nudge_mock_patch():
    """与既有 nudge 测试同一套 mock：QA active + inbox + trigger。返回 4 个 patch。"""
    return (
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
    )


@pytest.mark.asyncio
async def test_verify_reassign_created_goes_through_serialize_lock(task_env):
    """审计 M1：created VERIFY 改派必须走锁，不得旁路直接 claimed。"""
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    OTHER = "exec-other"
    p1, p2, p3, p4 = _nudge_mock_patch()

    with (
        patch("hiveweave.services.org.OrgService.list_agents", new=AsyncMock()),
        p1, p2, p3, p4,
    ):
        # 先唤醒第一个 → claimed（in-flight）
        first = await ts.get_task(pid, first_id)
        assert await _nudge_with_mocks(pid, first) is True
        second = await ts.get_task(pid, second_id)
        # reassign 一个 created VERIFY：锁内检查发现有 in-flight → 保持 created
        await ts.reassign_task(
            pid, second_id, new_assignee_id=OTHER, reassigned_by="coord-1",
        )
    after = await ts.get_task(pid, second_id)
    assert after["status"] == "created"
    assert after["assignee_id"] == OTHER


@pytest.mark.asyncio
async def test_pump_skips_old_bad_candidate_wakes_newer_good(task_env):
    """审计 M2：候选不可唤醒时泵换下一个，不永久堵死队列。"""
    from hiveweave.tools import task_tools as tt

    ts = TaskService()
    pid = task_env["project_id"]
    # A 最老但 QA 停用（不可唤醒）；B 较新但 QA 活跃
    bad_id = await _make_verify(pid, ts, title="UI A")
    good_id = await _make_verify(pid, ts, title="UI B")
    # A 的 assignee 改成一个非 active 的 agent —— 与 get_agent_by_id mock 冲突，
    # 直接用 inactive 返回区分：
    # 用 patch get_agent_by_id 返回 active 给特定 agent? 简化：B 指派 EXEC（active），
    # A 指派到 inactive 的幽灵 agent。
    GHOST = "ghost-qa"
    from hiveweave.services import task as task_module
    from hiveweave.tools.tasks import verify_spawn as vs

    await task_module._execute(
        pid, "UPDATE tasks SET assignee_id = ? WHERE id = ?", [GHOST, bad_id]
    )
    vs._pump_failed_cooldowns.clear()

    def fake_get(agent_id):
        if agent_id == GHOST:
            return {"id": agent_id, "name": agent_id, "status": "suspended"}
        return {"id": agent_id, "name": agent_id, "status": "active"}

    with (
        patch(
            "hiveweave.db.meta.get_agent_by_id",
            new=AsyncMock(side_effect=lambda aid: fake_get(aid)),
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
        n = await nudge_pending_verify_tasks(pid)
    assert n == 1
    good = await ts.get_task(pid, good_id)
    assert good["status"] == "claimed"
    assert (await ts.get_task(pid, bad_id))["status"] == "created"


@pytest.mark.asyncio
async def test_archive_verify_triggers_pump_to_next(task_env):
    """审计 S3：VERIFY 归档（cancel）后立即泵队列，不等 tick。"""
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    p1, p2, p3, p4 = _nudge_mock_patch()

    with p1, p2, p3, p4:
        # 唤醒 A → claimed（in-flight）
        first = await ts.get_task(pid, first_id)
        assert await _nudge_with_mocks(pid, first) is True
        # 归档 A → 锁释放 + 内嵌泵唤醒 B
        await ts.archive_task(
            pid, first_id, archived_by="coord-1", reason="cancel wrong"
        )
    # B 被泵唤醒 → claimed（若无泵则需等 tick，此处断言即时推进）
    # 注意：归档在 archive_task 内联泵已唤醒 B
    after = await ts.get_task(pid, second_id)
    assert after["status"] == "claimed"


@pytest.mark.asyncio
async def test_unclaim_verify_triggers_pump(task_env):
    """审计 S3：VERIFY unclaim（释放认领）后立即释放泵，不等 tick。"""
    ts = TaskService()
    pid = task_env["project_id"]
    first_id = await _make_verify(pid, ts, title="UI A")
    second_id = await _make_verify(pid, ts, title="UI B")
    p1, p2, p3, p4 = _nudge_mock_patch()

    with p1, p2, p3, p4:
        first = await ts.get_task(pid, first_id)
        assert await _nudge_with_mocks(pid, first) is True
        await ts.unclaim_task(pid, first_id)
    after = await ts.get_task(pid, second_id)
    assert after["status"] == "claimed"


@pytest.mark.asyncio
async def test_verify_serialize_lock_reentrant_no_deadlock(task_env):
    """审计 SUGGESTED：锁必须可重入，避免锁内 agent.chat 回合再调 claim_task 死锁。

    _VerifySerializeLock 持锁期间（claim → _transition → wait-contract 触发
    → agent.chat 的 LLM 回合）与触发方是同一 asyncio task。若该 LLM 回合再调
    claim_task（默认非 bypass）会重入同一把锁 —— 不可重入锁将永久死锁。
    此处直接验证重入 acquire / release 语义：同 task 重入放行、深度计数正确、
    异 task 仍互斥排队。
    """
    from hiveweave.tools.tasks.verify_spawn import _verify_serialize_lock

    lk = _verify_serialize_lock("proj-reentrant")
    entered = 0
    released = 0

    # 同 task：最内层持锁中再 acquire（模拟 LLM 回合内 claim_task 重入）
    async with lk:
        entered += 1
        async with lk:
            entered += 1
        released += 1
    released += 1

    assert entered == 2
    assert released == 2
    assert not lk.locked()

    # 异 task 互斥：第二个协程必须等第一个释放后才拿到锁
    order: list[str] = []

    async def holder(name: str, delay: float) -> None:
        async with _verify_serialize_lock("proj-reentrant"):
            order.append(f"{name}:in")
            await asyncio.sleep(delay)
            order.append(f"{name}:out")

    await asyncio.gather(
        holder("a", 0.05),
        holder("b", 0.0),
    )
    assert order == ["a:in", "a:out", "b:in", "b:out"]


@pytest.mark.asyncio
async def test_verify_serialize_lock_release_skips_cancelled_waiter():
    """回归：release 必须跳过已取消的队首 waiter，唤醒下一个存活等待者。

    审计发现：Task.cancel() 会同步取消 waiter 的 fut，但其 finally 出队
    清理要等事件循环下一轮才执行。owner 若恰在此窗口内 release，旧实现
    只弹出这个已死 fut 便不再唤醒任何人 —— 剩余 waiter 的 fut 永不
    resolve，该项目 VERIFY 队列永久假死（agent cancel / safety timeout
    都会制造此窗口）。
    """
    from hiveweave.tools.tasks.verify_spawn import _verify_serialize_lock

    lk = _verify_serialize_lock("proj-cancel-skip")
    acquired: list[str] = []

    async def waiter(name: str) -> None:
        async with lk:
            acquired.append(name)

    await lk.acquire()
    dead = asyncio.create_task(waiter("dead"))
    live = asyncio.create_task(waiter("live"))
    await asyncio.sleep(0.01)  # 两个 waiter 均已入队等锁
    assert dead.cancel() is True  # fut 同步取消；其 finally 出队清理尚未执行
    lk.release()  # 队首已死：旧实现不再唤醒任何人，修复后须唤醒 live
    await asyncio.wait_for(live, timeout=2)
    assert acquired == ["live"]
    try:
        await dead
    except asyncio.CancelledError:
        pass
    assert not lk.locked()


@pytest.mark.asyncio
async def test_verify_serialize_lock_woken_waiter_cancelled_hands_off():
    """回归：被唤醒的 waiter 死在恢复运行前，必须把唤醒权交接给下一个。

    镜像窗口（子代理审计 R1）：release() 的 set_result 与 waiter 的 __step
    之间隔一轮事件循环；此窗口内 Task.cancel() 走 _must_cancel 路径，waiter
    恢复时 await fut 抛 CancelledError、从未成为 owner，owner 已是 None。
    不做交接则后续 waiter 的 fut 永不 resolve → 队列假死（同 CPython
    asyncio.Lock 的 CancelledError 交接模式）。release 与 cancel 均为同步
    调用、中间无 await，时序确定性成立。
    """
    from hiveweave.tools.tasks.verify_spawn import _verify_serialize_lock

    lk = _verify_serialize_lock("proj-woken-cancel")
    acquired: list[str] = []

    async def waiter(name: str) -> None:
        async with lk:
            acquired.append(name)

    await lk.acquire()
    w1 = asyncio.create_task(waiter("w1"))
    w2 = asyncio.create_task(waiter("w2"))
    await asyncio.sleep(0.01)  # w1/w2 均入队等锁
    lk.release()  # 唤醒 w1（set_result），但 w1 尚未恢复运行
    assert w1.cancel() is True  # fut 已 done → _must_cancel：恢复时抛 CancelledError
    await asyncio.wait_for(w2, timeout=2)  # w1 必须交接唤醒 w2
    assert acquired == ["w2"]
    try:
        await w1
    except asyncio.CancelledError:
        pass
    assert not lk.locked()
