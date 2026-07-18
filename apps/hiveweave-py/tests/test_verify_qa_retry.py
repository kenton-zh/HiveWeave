"""VERIFY 死区回填：新 QA 到岗后 blocked VERIFY 自动 assign + unblock + 通知。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.task import TaskService
from hiveweave.tools.task_tools import retry_qa_blocked_verify_tasks

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401

QA = "qa-nova-1"

VERIFY_TAGS = ["verify", "mandatory", "post-merge"]
BLOCK_REASON = "No independent QA agent; hire QA before VERIFY can run"


def _agent(aid: str, role: str, parent_id: str | None = COORD) -> dict:
    return {
        "id": aid,
        "role": role,
        "status": "active",
        "parent_id": parent_id,
    }


async def _mk_blocked_verify(ts: TaskService, pid: str) -> tuple[str, str]:
    """父任务（实施者 EXEC）+ 无 QA 被 blocked 的 VERIFY 子任务。返回 (parent, verify)。"""
    parent_id = await ts.create_task(
        pid, "Fix login", "d", creator_id=COORD, assignee_id=EXEC
    )
    verify_id = await ts.create_task(
        pid,
        "VERIFY: Fix login",
        "verify",
        creator_id=COORD,
        assignee_id=None,
        parent_task_id=parent_id,
        tags=VERIFY_TAGS,
    )
    await ts.block_task(pid, verify_id, BLOCK_REASON)
    return parent_id, verify_id


def _nudge_patches(send: AsyncMock, trigger: AsyncMock):
    """让 _nudge_one_verify_task 走通（QA active + inbox + trigger 全 mock）。"""
    return (
        patch(
            "hiveweave.db.meta.get_agent_by_id",
            new=AsyncMock(
                return_value={"id": QA, "name": "nova", "status": "active"}
            ),
        ),
        patch("hiveweave.services.inbox.InboxService.send_message", send),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(return_value=1),
        ),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
    )


@pytest.mark.asyncio
async def test_retry_reattaches_blocked_verify_when_qa_arrives(task_env):
    """blocked+assignee NULL 的 VERIFY 在新 QA 出现后自动挂人、回到可认领态并唤醒。"""
    ts = TaskService()
    pid = task_env["project_id"]
    _, verify_id = await _mk_blocked_verify(ts, pid)

    send = AsyncMock()
    trigger = AsyncMock()
    agents = [_agent(EXEC, "前端工程师"), _agent(QA, "测试工程师")]
    p1, p2, p3, p4 = _nudge_patches(send, trigger)
    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents),
        ),
        p1, p2, p3, p4,
    ):
        n = await retry_qa_blocked_verify_tasks(pid)

    assert n == 1
    after = await ts.get_task(pid, verify_id)
    assert after["assignee_id"] == QA
    # nudge 通道接管：created → claimed（与正常 post-merge VERIFY 生命周期一致）
    assert after["status"] == "claimed"
    assert after["blocked_reason"] is None
    send.assert_awaited()
    kwargs = send.await_args.kwargs
    assert kwargs.get("to_agent_id") == QA
    assert "[POST-MERGE VERIFY]" in (kwargs.get("message") or "")
    trigger.assert_awaited_with(QA)
    # 挂人后进入 QA 的 assignee obligations
    obs = await ts.get_actionable_obligations(pid, QA)
    assert verify_id in [t["id"] for t in obs]


@pytest.mark.asyncio
async def test_retry_keeps_blocked_when_no_qa(task_env):
    """仍无独立 QA 时保持 blocked 不动、不通知。"""
    ts = TaskService()
    pid = task_env["project_id"]
    _, verify_id = await _mk_blocked_verify(ts, pid)

    send = AsyncMock()
    trigger = AsyncMock()
    agents = [_agent(EXEC, "前端工程师")]
    p1, p2, p3, p4 = _nudge_patches(send, trigger)
    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents),
        ),
        p1, p2, p3, p4,
    ):
        n = await retry_qa_blocked_verify_tasks(pid)

    assert n == 0
    after = await ts.get_task(pid, verify_id)
    assert after["status"] == "blocked"
    assert after["assignee_id"] is None
    assert after["blocked_reason"] == BLOCK_REASON
    send.assert_not_awaited()
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_never_assigns_parent_implementer(task_env):
    """独立性别名规则：实施者即使有 QA 角色也不能挂；有其他 QA 时挂其他 QA。"""
    ts = TaskService()
    pid = task_env["project_id"]
    _, verify_id = await _mk_blocked_verify(ts, pid)

    send = AsyncMock()
    trigger = AsyncMock()
    # 唯一的“QA 角色”恰是父任务实施者 EXEC → 视为无独立 QA，保持 blocked
    agents = [_agent(EXEC, "测试工程师"), _agent("exec-2", "后端工程师")]
    p1, p2, p3, p4 = _nudge_patches(send, trigger)
    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents),
        ),
        p1, p2, p3, p4,
    ):
        n = await retry_qa_blocked_verify_tasks(pid)
    assert n == 0
    after = await ts.get_task(pid, verify_id)
    assert after["status"] == "blocked"
    assert after["assignee_id"] is None
    send.assert_not_awaited()

    # 实施者是 QA 角色 + 另有独立 QA Nova → 挂 Nova 而非 EXEC
    agents2 = [_agent(EXEC, "测试工程师"), _agent(QA, "测试工程师")]
    p1, p2, p3, p4 = _nudge_patches(send, trigger)
    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents2),
        ),
        p1, p2, p3, p4,
    ):
        n = await retry_qa_blocked_verify_tasks(pid)
    assert n == 1
    after = await ts.get_task(pid, verify_id)
    assert after["assignee_id"] == QA
    assert after["assignee_id"] != EXEC
    assert after["status"] == "claimed"


@pytest.mark.asyncio
async def test_retry_ignores_assigned_or_non_blocked(task_env):
    """只处理 blocked+assignee NULL 的 VERIFY：已挂人/非 VERIFY 不动。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parent_id, verify_id = await _mk_blocked_verify(ts, pid)
    # 已有 assignee 的 blocked VERIFY —— 不属于本次死区，不动
    other_id = await ts.create_task(
        pid,
        "VERIFY: Other",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent_id,
        tags=VERIFY_TAGS,
    )
    await ts.claim_task(pid, other_id, EXEC)
    await ts.start_task(pid, other_id)
    await ts.block_task(pid, other_id, "external: waiting browser")
    # 非 VERIFY 的 blocked 任务，同样不动
    plain_id = await ts.create_task(
        pid, "Plain task", "d", creator_id=COORD, assignee_id=None
    )
    await ts.block_task(pid, plain_id, "user: need input")

    send = AsyncMock()
    trigger = AsyncMock()
    agents = [_agent(EXEC, "前端工程师"), _agent(QA, "测试工程师")]
    p1, p2, p3, p4 = _nudge_patches(send, trigger)
    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            new=AsyncMock(return_value=agents),
        ),
        p1, p2, p3, p4,
    ):
        n = await retry_qa_blocked_verify_tasks(pid)

    assert n == 1  # 只有 verify_id 被重挂
    other = await ts.get_task(pid, other_id)
    assert other["status"] == "blocked"
    assert other["assignee_id"] == EXEC
    plain = await ts.get_task(pid, plain_id)
    assert plain["status"] == "blocked"
    assert plain["assignee_id"] is None
