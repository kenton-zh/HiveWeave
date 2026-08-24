"""2026-08-11 意见核实修复：orphan migrate 误杀 + is-ancestor 死代码 + 关父静默失败.

- Fix 1: migrate_orphan_approved 宽限期（approved→merge 正常窗口）+ pending
  merge obligation 检查 —— B4 事故（husk 阻塞 merge 被当孤儿静默 close）
- Fix 2: _enforce_merge_on_close 的 is-ancestor 校验复活（merge fact 存在时
  tip 必须真在 main；_task_skips_merge_gate 不再跳过 merge_fact）
- Fix 3: VERIFY 关父失败 → 事件 + 通知 creator（不再双重吞没）
- Fix 4: mark_verifying 失败 → 事件落账
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services import task as task_module
from hiveweave.services.obligation import ObligationLedger
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.verify import ORPHAN_APPROVED_GRACE_MS

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


async def _make_approved(ts, pid, title="Feature", creator=COORD, assignee=EXEC):
    tid = await ts.create_task(pid, title, "d", creator_id=creator, assignee_id=assignee)
    await ts.claim_task(pid, tid, assignee)
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, evidence={"tests_passed": True, "test_output": "ok"})
    await ts.start_review(pid, tid)
    await ts.review_task(pid, tid, "approve")
    assert (await ts.get_task(pid, tid))["status"] == "approved"
    return tid


# ── Fix 1: migrate 宽限期 + merge obligation ────────────────


@pytest.mark.asyncio
async def test_migrate_skips_freshly_approved(task_env):
    """approved 在宽限期内（approve→merge 正常窗口）→ migrate 不 close。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    # 更新 updated_at 为"刚刚"（migrate 宽限期判定依据）
    now = int(time.time() * 1000)
    await task_module._execute(
        pid, "UPDATE tasks SET updated_at = ? WHERE id = ?", [now, tid]
    )
    res = await ts.migrate_orphan_approved(pid)
    assert res["closed"] == 0
    assert (await ts.get_task(pid, tid))["status"] == "approved"


@pytest.mark.asyncio
async def test_migrate_closes_stale_approved_without_merge_obligation(task_env):
    """approved 超宽限期 + 无 pending merge obligation + 无 VERIFY 子 → 关闭。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    now = int(time.time() * 1000)
    await task_module._execute(
        pid, "UPDATE tasks SET updated_at = ? WHERE id = ?",
        [now - ORPHAN_APPROVED_GRACE_MS - 60_000, tid],
    )
    res = await ts.migrate_orphan_approved(pid)
    assert res["closed"] == 1
    assert (await ts.get_task(pid, tid))["status"] == "closed"


@pytest.mark.asyncio
async def test_migrate_keeps_stale_approved_with_pending_merge_obligation(task_env):
    """approved 超宽限期但 pending merge obligation 未 fulfill → 不 close
    （merge 流程进行中 —— B4 事故场景：merge 被阻塞时义务还挂着）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    now = int(time.time() * 1000)
    await task_module._execute(
        pid, "UPDATE tasks SET updated_at = ? WHERE id = ?",
        [now - ORPHAN_APPROVED_GRACE_MS - 60_000, tid],
    )
    # 挂一个 pending merge obligation（等价真实：approve 时 _inject 创建）
    await ObligationLedger().create(
        project_id=pid,
        owner_agent_id=COORD,
        obligation_type="merge",
        task_id=tid,
        context={},
    )
    res = await ts.migrate_orphan_approved(pid)
    assert res["closed"] == 0
    assert (await ts.get_task(pid, tid))["status"] == "approved"


# ── Fix 2: is-ancestor 校验复活 ─────────────────────────────


@pytest.mark.asyncio
async def test_close_blocks_merge_fact_tip_not_in_main(task_env):
    """merge fact 存在但分支 tip 不在 main → close 被拒（is-ancestor 复活，
    _task_skips_merge_gate 不再跳过 merge_fact）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    # 打 merge fact 证据（但分支实际不在 main —— 模拟 merge 后又新增 commit）
    ev = {"merge_commit": "deadbeef00000000000000000000000000000000",
          "tests_passed": True}
    await task_module._execute(
        pid, "UPDATE tasks SET evidence = ? WHERE id = ?",
        [__import__("json").dumps(ev), tid],
    )
    with (
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.git_worktree._resolve_base_branch",
            new=AsyncMock(return_value="main"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            new=AsyncMock(side_effect=lambda cmd, cwd: (
                (True, "hw/A004/work") if cmd[0] == "rev-parse" and cmd[1] == "--verify"
                else (False, "not ancestor")  # merge-base --is-ancestor 失败
            )),
        ),
        patch(
            "hiveweave.services.git_worktree._has_git",
            return_value=True,
        ),
        patch(
            "hiveweave.services.git_worktree._current_branch",
            new=AsyncMock(return_value="hw/A004/work"),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService._resolve_effective_worktree_path",
            new=AsyncMock(return_value="/tmp/ws/.hiveweave/worktrees/A004"),
        ),
        patch(
            "hiveweave.services.org.OrgService.resolve_agent",
            new=AsyncMock(return_value={"short_id": "A004"}),
        ),
        patch(
            "hiveweave.services.tasks.close.CloseMixin._rollback_close_to_approved",
            new=AsyncMock(),
        ),
    ):
        with pytest.raises(Exception) as exc:
            await ts.close_task(pid, tid)
    assert "branch" in str(exc.value) or "merge" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_close_allows_merge_fact_tip_in_main(task_env):
    """merge fact 存在且 tip 在 main → close 放行（is-ancestor 通过）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    ev = {"merge_commit": "deadbeef00000000000000000000000000000000",
          "tests_passed": True}
    await task_module._execute(
        pid, "UPDATE tasks SET evidence = ? WHERE id = ?",
        [__import__("json").dumps(ev), tid],
    )
    with (
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.git_worktree._resolve_base_branch",
            new=AsyncMock(return_value="main"),
        ),
        patch(
            "hiveweave.services.git_worktree._git",
            new=AsyncMock(side_effect=lambda cmd, cwd: (
                (True, "") if cmd[0] == "rev-parse" and cmd[1] == "--verify"
                else (True, "")  # merge-base --is-ancestor 成功
            )),
        ),
        patch(
            "hiveweave.services.git_worktree._has_git",
            return_value=True,
        ),
        patch(
            "hiveweave.services.git_worktree._current_branch",
            new=AsyncMock(return_value="hw/A004/work"),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService._resolve_effective_worktree_path",
            new=AsyncMock(return_value="/tmp/ws/.hiveweave/worktrees/A004"),
        ),
        patch(
            "hiveweave.services.org.OrgService.resolve_agent",
            new=AsyncMock(return_value={"short_id": "A004"}),
        ),
        patch(
            "hiveweave.services.obligation.ObligationLedger.fulfill",
            new=AsyncMock(return_value=0),
        ),
    ):
        await ts.close_task(pid, tid)
    assert (await ts.get_task(pid, tid))["status"] == "closed"


# ── Fix 3: VERIFY 关父失败 → 事件 + 通知 ────────────────────


@pytest.mark.asyncio
async def test_verify_parent_close_failure_notifies_creator(task_env):
    """VERIFY approve 联动关父失败（merge gate 拒）→ 事件 + 通知 creator，
    不再双重吞没。"""
    from hiveweave.tools.task_tools import _spawn_post_approve_verify_task  # noqa: F401

    ts = TaskService()
    pid = task_env["project_id"]
    parent_id = await _make_approved(ts, pid)

    # 模拟 VERIFY child 已 approved（父仍在 approved —— mark_verifying 失败场景）
    verify_id = await ts.create_task(
        pid, "VERIFY: Feature", "verify",
        creator_id=COORD, assignee_id=EXEC,
        parent_task_id=parent_id, source="system",
    )
    await ts.claim_task(pid, verify_id, EXEC)
    await ts.start_task(pid, verify_id)
    await ts.submit_task(
        pid, verify_id, evidence={"verdict": "PASS", "tests_passed": True}
    )
    await ts.start_review(pid, verify_id)

    with (
        patch(
            "hiveweave.services.tasks.close.CloseMixin._enforce_merge_on_close",
            new=AsyncMock(side_effect=Exception("merge gate: branch not in main")),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_coordinator",
            new=AsyncMock(),
        ),
    ):
        # approve VERIFY → 服务层联动 _close_verify_and_parent → 父 close 被 gate 拒
        await ts.review_task(pid, verify_id, "approve")

    # 事件落账（verify_parent_close_failed）
    events = [
        r["event_type"]
        for r in await task_module._query(
            pid, "SELECT event_type FROM task_events WHERE task_id = ?",
            [parent_id],
        )
    ]
    assert "verify_parent_close_failed" in events
    # creator 收到通知
    assert send.await_count >= 1
    msg = send.await_args.kwargs["message"]
    assert "VERIFY PARENT CLOSE FAILED" in msg
    assert send.await_args.kwargs["to_agent_id"] == COORD


# ── Fix 4: mark_verifying 失败事件 ──────────────────────────


@pytest.mark.asyncio
async def test_mark_verifying_failure_emits_event(task_env):
    """mark_verifying 抛异常 → 事件落账（不再静默吞没）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await _make_approved(ts, pid)
    with patch(
        "hiveweave.services.task.TaskService.mark_verifying",
        new=AsyncMock(side_effect=Exception("boom")),
    ):
        from hiveweave.tools.tasks.verify_spawn import _spawn_post_approve_verify_task

        with patch(
            "hiveweave.tools.tasks.verify_spawn._find_independent_qa",
            new=AsyncMock(return_value=EXEC),
        ):
            await _spawn_post_approve_verify_task(ts, pid, COORD,
                                                  await ts.get_task(pid, tid))
    events = [
        r["event_type"]
        for r in await task_module._query(
            pid, "SELECT event_type FROM task_events WHERE task_id = ?", [tid]
        )
    ]
    assert "parent_mark_verifying_failed" in events
