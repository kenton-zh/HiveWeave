"""2026-08-11 slack-clone_01 MERGE PENDING 噪音复盘：接收者 + waive 清理.

- Fix 1: approve 即时 [MERGE PENDING] 发给 creator（merge 职责方），
  与 game_time stale nudge / merge 后清理（verify._clear_merge_pending_inbox）
  一致 —— 第三方代审场景（reviewer ≠ creator）不再打扰审批人。
- Fix 2: waive_merge auto-close 后清理该任务残留的 [MERGE PENDING]/[MERGE PROXY]。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.review import _inject_merge_pending_wake
from hiveweave.tools.tasks.waive import (
    WaiveMergeParams,
    waive_merge_tool,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


# ── Fix 1: 即时 wake 接收者 = creator ───────────────────────


@pytest.mark.asyncio
async def test_merge_pending_wake_goes_to_creator_not_reviewer():
    """代审场景（reviewer ≠ creator）：即时 wake 打给 creator（merge 职责方），
    不再打扰审批人；merge 义务也记在 creator 名下。"""
    task = {
        "id": "t-merge-1",
        "title": "ROUND2 协调任务",
        "creator_id": "creator-1",
    }
    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            new=AsyncMock(),
        ) as trigger,
        patch(
            "hiveweave.services.obligation.ObligationLedger.create",
            new=AsyncMock(),
        ) as obl,
    ):
        await _inject_merge_pending_wake(
            project_id="p",
            reviewer_id="reviewer-1",
            task=task,
            short_id="A011",
            reason="approved_needs_merge",
        )
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["to_agent_id"] == "creator-1"
    assert "t-merge-" in kwargs["message"]  # tid[:8] 截断
    trigger.assert_awaited_once_with("creator-1")
    obl.assert_awaited_once()
    assert obl.await_args.kwargs["owner_agent_id"] == "creator-1"


@pytest.mark.asyncio
async def test_merge_pending_wake_falls_back_to_reviewer_without_creator():
    """creator 缺失 → fallback reviewer（保持可用）。"""
    task = {"id": "t-merge-2", "title": "X"}
    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.obligation.ObligationLedger.create",
            new=AsyncMock(),
        ),
    ):
        await _inject_merge_pending_wake(
            project_id="p",
            reviewer_id="reviewer-1",
            task=task,
        )
    assert send.await_args.kwargs["to_agent_id"] == "reviewer-1"


@pytest.mark.asyncio
async def test_merge_pending_wake_skips_user_sentinel_creator():
    """API 人类任务（creator="user" 哨兵，非 agent）→ 不发给它，fallback
    reviewer（审计 #4：发给 "user" inbox 无意义且 merge proxy 无路可走）。"""
    for sentinel in ("user", "用户", "human"):
        task = {"id": "t-merge-3", "title": "X", "creator_id": sentinel}
        with (
            patch(
                "hiveweave.services.inbox.InboxService.send_message",
                new=AsyncMock(),
            ) as send,
            patch(
                "hiveweave.agents.trigger.trigger_coordinator",
                new=AsyncMock(),
            ),
            patch(
                "hiveweave.services.obligation.ObligationLedger.create",
                new=AsyncMock(),
            ) as obl,
        ):
            await _inject_merge_pending_wake(
                project_id="p",
                reviewer_id="reviewer-1",
                task=task,
            )
        assert send.await_args.kwargs["to_agent_id"] == "reviewer-1"
        assert obl.await_args.kwargs["owner_agent_id"] == "reviewer-1"


# ── Fix 2: waive_merge 清理残留 MERGE PENDING ───────────────


async def _make_approved_task(ts, pid) -> str:
    """完整生命周期走到 approved（approve 时无 worktree → ahead=None →
    发即时 wake 但任务保持 approved，等价真实「先 approve 后 waive」）。"""
    tid = await ts.create_task(
        pid, "协调任务", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")
    assert (await ts.get_task(pid, tid))["status"] == "approved"
    return tid


@pytest.mark.asyncio
async def test_waive_merge_clears_residual_merge_pending(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved_task(ts, pid)

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(),
        ) as supersede,
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
    ):
        params = WaiveMergeParams(
            task_id=tid,
            reason="纯协调任务无独立代码交付，前端修复由实施者直接合并上 main",
        )
        result = await waive_merge_tool(params, COORD, "/tmp/ws")

    assert result.success is True
    assert (await ts.get_task(pid, tid))["status"] == "closed"
    # 残留 [MERGE PENDING] 被清理：发给 creator 的 inbox，前缀 + 任务 id 匹配
    supersede.assert_awaited_once()
    assert supersede.await_args.args[0] == COORD  # 位置参数 = 目标 agent
    kwargs = supersede.await_args.kwargs
    assert "[MERGE PENDING]" in kwargs["prefixes"]
    assert tid[:8] in kwargs["contains"]


@pytest.mark.asyncio
async def test_mark_verifying_cleans_owner_inbox_not_raw_creator(task_env):
    """钉住 verify.py 的清理目标改动：mark_verifying 清理 resolve_merge_owner
    解析出的 owner（代审场景 = creator），哨兵 creator 任务 fallback reviewer
    （此前只清原始 creator 列 —— 代审/哨兵场景残留）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")
    assert (await ts.get_task(pid, tid))["status"] == "approved"

    with (
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(),
        ) as supersede,
    ):
        await ts.mark_verifying(pid, tid)

    supersede.assert_awaited_once()
    assert supersede.await_args.args[0] == COORD  # owner = creator（真实 agent）
    kwargs = supersede.await_args.kwargs
    assert "[MERGE PENDING]" in kwargs["prefixes"]
    assert tid[:8] in kwargs["contains"]


@pytest.mark.asyncio
async def test_mark_verifying_sentinel_creator_falls_back_to_reviewer(task_env):
    """API 人类哨兵 creator（且 reviewer 同为哨兵——submit 时默认
    reviewer=creator）：resolve_merge_owner 全哨兵链 → None → 无人可清，
    supersede 不调用（不再清 "user" 空 inbox）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id="user", assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")
    task = await ts.get_task(pid, tid)
    assert task.get("reviewer_id") == "user"  # 哨兵链

    with (
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(),
        ) as supersede,
    ):
        await ts.mark_verifying(pid, tid)

    supersede.assert_not_awaited()
@pytest.mark.asyncio
async def test_waive_merge_not_approved_skips_cleanup(task_env):
    """未 auto-close（任务非 approved）→ 不清理（无事发生，无残留）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "协调任务", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.supersede_watchdog_messages",
            new=AsyncMock(),
        ) as supersede,
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
    ):
        params = WaiveMergeParams(
            task_id=tid,
            reason="任务尚未 approved，仅预登记豁免理由（测试路径）",
        )
        result = await waive_merge_tool(params, COORD, "/tmp/ws")

    assert result.success is True
    supersede.assert_not_awaited()
