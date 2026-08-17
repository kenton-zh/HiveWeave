"""2026-08-11 slack-clone_01 VERIFY 队列死锁：四层修复回归测试.

- Fix A: parked blocked VERIFY（无自动解封路径）不占串行化锁
- Fix B: update_task_status blocked 结构化契约（dependsOnTaskIds / waitKind /
  wakeAt；无 deps 且无 wake_at 硬拒；不再从 blockedReason 文案猜意图）
- Fix C: BLOCKED STALE inbox 已禁用；reconcile 仍解封有路径的 blocked。
  parked（无 wake 路径）任务不再催 creator。
- 回归：blocked_task_has_wake_path 判定本身
"""

from __future__ import annotations

import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import game_time
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.lifecycle import blocked_task_has_wake_path
from hiveweave.services.wait_contract import wait_contract_service
from hiveweave.tools.result import ToolResult
from hiveweave.tools.tasks.lifecycle import (
    UpdateTaskStatusParams,
    update_task_status_tool,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401

PROJECT_ID = "test-blocked-wake-path"
CEO_ID = "test-ceo"
QA_ID = "test-qa"


@pytest.fixture(autouse=True)
def clean_states():
    game_time._states.clear()
    yield
    game_time._states.clear()


# ── blocked_task_has_wake_path 判定 ─────────────────────────


def test_wake_path_helper_cases():
    now = int(time.time() * 1000)
    assert blocked_task_has_wake_path(
        {"depends_on": ["t1"], "wait_kind": None, "wake_at": None}, now
    )
    assert blocked_task_has_wake_path(
        {"depends_on": '["t1","t2"]', "wait_kind": None, "wake_at": None}, now
    )
    assert blocked_task_has_wake_path(
        {"depends_on": [], "wait_kind": "timer", "wake_at": now + 60_000}, now
    )
    # 过期 timer 仍有解封路径（reconcile 同 tick 解封；泵先于 reconcile 运行，
    # 判 parked 会放行第二个 VERIFY 造成双并发）
    assert blocked_task_has_wake_path(
        {"depends_on": [], "wait_kind": "timer", "wake_at": now - 60_000}, now
    )
    # 无 wake_at 的 timer / 空 deps → parked
    assert not blocked_task_has_wake_path(
        {"depends_on": [], "wait_kind": "timer", "wake_at": None}, now
    )
    assert not blocked_task_has_wake_path(
        {"depends_on": [], "wait_kind": None, "wake_at": None}, now
    )
    # 中文 reason 不参与判定（HARD RULE：禁文案猜意图）
    assert not blocked_task_has_wake_path(
        {
            "depends_on": [],
            "wait_kind": None,
            "wake_at": None,
            "blocked_reason": "归零策略：等全部合并后批量验收",
        },
        now,
    )
    assert blocked_task_has_wake_path(
        {
            "depends_on": ["t1"],
            "wait_kind": None,
            "wake_at": None,
            "blocked_reason": "归零策略",
        },
        now,
    )


# ── Fix B: update_task_status 结构化契约 ────────────────────


async def _call_block_tool(params: UpdateTaskStatusParams, pid: str) -> ToolResult:
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new=AsyncMock(return_value=pid),
    ):
        return await update_task_status_tool(params, EXEC, "/tmp/ws")


@pytest.mark.asyncio
async def test_tool_block_without_deps_or_wake_at_hard_rejected(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="归零策略：等全部 ROUND2 合并后批量验收",
    )
    result = await _call_block_tool(params, pid)
    assert result.success is False
    msg = result.error or str(result)
    assert "auto-unblock" in msg and "dependsOnTaskIds" in msg
    task = await ts.get_task(pid, tid)
    assert task["status"] == "running"  # 未变 blocked


@pytest.mark.asyncio
async def test_tool_block_with_deps_list_and_chinese_reason(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    b1 = await ts.create_task(pid, "B1", "d", creator_id=COORD, assignee_id=EXEC)
    b2 = await ts.create_task(pid, "B2", "d", creator_id=COORD, assignee_id=EXEC)
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="归零策略：等全部 ROUND2 修复合并",
        depends_on_task_ids=[b1, b2],
    )
    result = await _call_block_tool(params, pid)
    assert result.success is True
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"
    assert task["wait_kind"] == "dependency"  # deps 存在 → 结构化推断
    deps = task.get("depends_on") or []
    assert b1 in deps and b2 in deps


@pytest.mark.asyncio
async def test_tool_block_timer_wake_at_iso(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="等待外部验收窗口",
        wait_kind="timer",
        wake_at=deadline.isoformat(),
    )
    result = await _call_block_tool(params, pid)
    assert result.success is True
    task = await ts.get_task(pid, tid)
    assert task["wait_kind"] == "timer"
    expect_ms = int(deadline.timestamp() * 1000)
    assert abs(int(task["wake_at"]) - expect_ms) < 2000


@pytest.mark.asyncio
async def test_tool_block_timer_wake_at_epoch_ms_int(task_env):
    """wakeAt 传 epoch 毫秒数字也能解析（LLM 常见形态）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    now = int(time.time() * 1000)
    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="等窗口",
        wake_at=now + 3_600_000,
    )
    result = await _call_block_tool(params, pid)
    assert result.success is True
    task = await ts.get_task(pid, tid)
    assert task["wait_kind"] == "timer"
    assert int(task["wake_at"]) == now + 3_600_000


@pytest.mark.asyncio
async def test_tool_block_timer_wake_at_epoch_seconds(task_env):
    """wakeAt 传 epoch 秒（如 1750000000）自动按秒识别 ×1000，不会静默变成
    已过期毫秒时间戳（数据完整性审计 #1）。"""
    from hiveweave.tools.tasks.lifecycle import _parse_wake_at_ms

    assert _parse_wake_at_ms(1_750_000_000) == 1_750_000_000_000
    assert _parse_wake_at_ms("1750000000") == 1_750_000_000_000
    # 正常 epoch 毫秒（当前量级 1.75e12）原样通过
    now_ms = int(time.time() * 1000)
    assert _parse_wake_at_ms(now_ms) == now_ms


@pytest.mark.asyncio
async def test_tool_block_wakeat_unparseable_gets_clear_error(task_env):
    """wakeAt 给了但解析失败 → 明确报解析失败，而非误导性的「没给 wakeAt」."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="等窗口",
        wake_at="not-a-time",
    )
    result = await _call_block_tool(params, pid)
    assert result.success is False
    assert "not a parseable ISO-8601" in (result.error or "")
    task = await ts.get_task(pid, tid)
    assert task["status"] == "running"


@pytest.mark.asyncio
async def test_claim_gate_names_blocked_blocker_with_wake_path(task_env):
    """Fix D 钉测试：直接 claim 排队 VERIFY，被「blocked+有解封路径」的
    VERIFY 挡下时，错误必须点名阻塞者并如实说明（不承诺虚假唤醒）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    blocker_a = await ts.create_task(
        pid, "VERIFY: UI A", "verify",
        creator_id=COORD, assignee_id=EXEC,
        source="system",
    )
    queued_b = await ts.create_task(
        pid, "VERIFY: UI B", "verify",
        creator_id=COORD, assignee_id=EXEC,
        source="system",
    )
    dep = await ts.create_task(pid, "Blocker", "d",
                               creator_id=COORD, assignee_id=EXEC)
    await ts.claim_task(pid, blocker_a, EXEC)
    await ts.start_task(pid, blocker_a)
    await ts.block_task(pid, blocker_a, "等依赖",
                        depends_on_task_id=dep)
    assert (await ts.get_task(pid, queued_b))["status"] == "created"

    with pytest.raises(ValueError) as exc:
        await ts.claim_task(pid, queued_b, EXEC)
    msg = str(exc.value)
    assert blocker_a[:8] in msg
    assert "blocked" in msg
    assert "MAIN" in msg
    # 没有虚假承诺：有解封路径 → 说「MAIN frees / 自愈」，不说「一定能叫醒」
    assert (await ts.get_task(pid, queued_b))["status"] == "created"


@pytest.mark.asyncio
async def test_unblock_verify_rejected_while_another_in_flight(task_env):
    """并发审计 F1：手动解封 parked VERIFY 时若另一 VERIFY 在飞 → 拒绝
    （否则双 VERIFY 上 MAIN，issue #6 违背）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    running_a = await ts.create_task(
        pid, "VERIFY: UI A", "verify",
        creator_id=COORD, assignee_id=EXEC, source="system",
    )
    parked_b = await ts.create_task(
        pid, "VERIFY: UI B", "verify",
        creator_id=COORD, assignee_id=EXEC, source="system",
    )
    # 先 park B（blocked 无解封路径，不占锁），再 claim A 使其在飞
    await ts.claim_task(pid, parked_b, EXEC)
    await ts.start_task(pid, parked_b)
    await ts.block_task(pid, parked_b, "手工挂起等批量验收")
    await ts.claim_task(pid, running_a, EXEC)  # A 在飞（claimed）
    assert (await ts.get_task(pid, parked_b))["status"] == "blocked"

    with pytest.raises(ValueError) as exc:
        await ts.unblock_task(pid, parked_b)
    assert "another VERIFY" in str(exc.value)
    assert "serialized" in str(exc.value).lower() or "in flight" in str(exc.value)
    assert (await ts.get_task(pid, parked_b))["status"] == "blocked"


@pytest.mark.asyncio
async def test_unblock_verify_allowed_when_no_in_flight(task_env):
    """并发审计 F1 反向：无在飞 VERIFY 时，解封 parked VERIFY 自己放行
    （except_id 自排除 + 非 VERIFY 任务不受门影响）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    parked = await ts.create_task(
        pid, "VERIFY: UI C", "verify",
        creator_id=COORD, assignee_id=EXEC, source="system",
    )
    plain = await ts.create_task(
        pid, "Plain", "d", creator_id=COORD, assignee_id=EXEC
    )
    for tid in (parked, plain):
        await ts.claim_task(pid, tid, EXEC)
        await ts.start_task(pid, tid)
        await ts.block_task(pid, tid, "等用户确认")

    await ts.unblock_task(pid, parked)
    assert (await ts.get_task(pid, parked))["status"] == "running"
    assert (await ts.get_task(pid, parked))["wait_kind"] is None
    await ts.unblock_task(pid, plain)
    assert (await ts.get_task(pid, plain))["status"] == "running"


@pytest.mark.asyncio
async def test_tool_block_explicit_wait_kind_does_not_bypass_rule(task_env):
    """显式 waitKind 也不能绕过「无 deps 无 wake_at 硬拒」."""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="等用户确认",
        wait_kind="user",
    )
    result = await _call_block_tool(params, pid)
    assert result.success is False
    assert "dependsOnTaskIds" in (result.error or "")
    task = await ts.get_task(pid, tid)
    assert task["status"] == "running"


# ── Fix C: BLOCKED STALE 看门狗 ────────────────────────────


@pytest.fixture
async def gt_env():
    """真实 per-project DB（temp workspace）+ meta 路由 patch（对齐
    test_silence_watchdog.py）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_query_one(sql: str, args=None):
            if "is_started" in sql:
                return {"is_started": 1}
            return None

        from hiveweave.services import task as task_mod

        task_mod._migrated.discard(PROJECT_ID)
        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.query_one", fake_query_one),
        ):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_agents(env):
    conn = await project_db.ensure_project_db(env["workspace_path"])
    now = int(time.time() * 1000)
    for aid, role, parent in (
        (CEO_ID, "ceo", None),
        (QA_ID, "qa_engineer", CEO_ID),
    ):
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            [aid, PROJECT_ID, role, role, parent, now, now],
        )
    await conn.commit()


async def _seed_live_wait_contract(env, agent_id):
    """agent_waits 活跃契约（cleared_at IS NULL）—— 曾让看门狗永久跳过."""
    conn = await project_db.ensure_project_db(env["workspace_path"])
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_waits ("
        "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, project_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, ref TEXT NOT NULL, wake_on TEXT NOT NULL DEFAULT '[]', "
        "expires_at INTEGER, obligation_version TEXT, phase TEXT, note TEXT, "
        "created_at INTEGER NOT NULL, cleared_at INTEGER)"
    )
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, wake_on, "
        "expires_at, phase, note, created_at, cleared_at) "
        "VALUES (?, ?, ?, 'task', 'some-task', '[]', ?, 'waiting', "
        "'commit_turn(waiting) 后形成的契约', ?, NULL)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, now + 3600_000, now],
    )
    await conn.commit()


async def _age_task(env, tid, minutes=40):
    conn = await project_db.ensure_project_db(env["workspace_path"])
    now = int(time.time() * 1000)
    await conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        [now - minutes * 60_000, tid],
    )
    await conn.commit()


def _run_watchdog(env):
    """Drive GameTimeService._reconcile_blocked_tasks with the pieces patched."""
    now = int(time.time() * 1000)
    game_time._states[PROJECT_ID] = {
        "duty_session_started_at_ms": now - 2 * 3600_000,
    }
    gts = GameTimeService()
    return gts._reconcile_blocked_tasks(PROJECT_ID)


@pytest.mark.asyncio
async def test_watchdog_parked_blocked_notified_to_creator_despite_live_wait(
    gt_env,
):
    """parked（无 wake 路径）blocked：不再发 [BLOCKED STALE]；reconcile only."""
    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI A", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system")
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "归零策略：等全部合并后批量验收")
    await _age_task(gt_env, tid)
    await _seed_live_wait_contract(gt_env, QA_ID)

    with (
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()) as send,
        patch.object(GameTimeService, "_watchdog_trigger", new=AsyncMock()),
    ):
        await _run_watchdog(gt_env)

    assert send.await_count == 0
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"


@pytest.mark.asyncio
async def test_watchdog_has_wake_path_with_live_wait_is_skipped(gt_env):
    """有 depends_on（wake 路径）+ assignee 活跃契约 → 看门狗跳过（reconcile 接管）。"""
    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    blocker = await ts.create_task(pid, "Blocker", "d",
                                   creator_id=CEO_ID, assignee_id=QA_ID)
    tid = await ts.create_task(pid, "VERIFY: UI B", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system")
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等 blocker",
                        depends_on_task_id=blocker)
    await _age_task(gt_env, tid)
    await _seed_live_wait_contract(gt_env, QA_ID)

    with (
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()) as send,
        patch.object(GameTimeService, "_watchdog_trigger", new=AsyncMock()),
    ):
        await _run_watchdog(gt_env)

    stale = [
        c for c in send.await_args_list
        if "BLOCKED STALE" in (c.kwargs.get("message") or "")
    ]
    assert not stale  # 有 wake 路径 + live wait → 不催


@pytest.mark.asyncio
async def test_reconcile_original_paths_unchanged(gt_env):
    """回归：timer 到期自动解封、depends_on 全完成自动解封 两条原路径不回归。"""
    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    now = int(time.time() * 1000)

    timer_tid = await ts.create_task(pid, "Timer", "d",
                                     creator_id=CEO_ID, assignee_id=QA_ID)
    await ts.claim_task(pid, timer_tid, QA_ID)
    await ts.start_task(pid, timer_tid)
    await ts.block_task(pid, timer_tid, "timer 到期自动解封",
                        wait_kind="timer", wake_at=now - 1000)
    assert (await ts.get_task(pid, timer_tid))["status"] == "blocked"

    dep_tid = await ts.create_task(pid, "Dep", "d",
                                   creator_id=CEO_ID, assignee_id=QA_ID)
    dep_blk = await ts.create_task(pid, "DepBlocker", "d",
                                   creator_id=CEO_ID, assignee_id=QA_ID)
    await ts.claim_task(pid, dep_tid, QA_ID)
    await ts.start_task(pid, dep_tid)
    await ts.block_task(pid, dep_tid, "依赖未完成",
                        depends_on_task_id=dep_blk)
    assert (await ts.get_task(pid, dep_tid))["status"] == "blocked"

    with (
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()),
        patch.object(GameTimeService, "_watchdog_trigger", new=AsyncMock()),
    ):
        await _run_watchdog(gt_env)

    # timer 过期 → 已被 reconcile 解封
    assert (await ts.get_task(pid, timer_tid))["status"] == "running"
    # depends_on 未完成 → 仍 blocked
    assert (await ts.get_task(pid, dep_tid))["status"] == "blocked"
    # blocker 走完整生命周期（start→submit→approve→close）后 reconcile
    # → 依赖满足自动解封（skip_merge_gate：本测试关注 reconcile 依赖路径，
    #   不测 merge 门）
    await ts.start_task(pid, dep_blk)
    await ts.submit_task(
        pid, dep_blk, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, dep_blk)
    await ts.review_task(pid, dep_blk, "approve")
    await ts.close_task(pid, dep_blk, skip_merge_gate=True)
    with (
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()),
        patch.object(GameTimeService, "_watchdog_trigger", new=AsyncMock()),
    ):
        await _run_watchdog(gt_env)
    assert (await ts.get_task(pid, dep_tid))["status"] == "running"
