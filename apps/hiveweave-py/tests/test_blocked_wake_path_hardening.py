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
from hiveweave.services.tasks.lifecycle import (
    blocked_task_has_wake_path,
    blocked_task_needs_arbitration_escalation,
)
from hiveweave.services.wait_contract import wait_contract_service
from hiveweave.tools.result import ToolResult
from hiveweave.tools.tasks.lifecycle import (
    UpdateTaskStatusParams,
    update_task_status_tool,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401
from hiveweave.services.tasks.verify import VERIFY_KIND

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


# ── F6 前置项：第三类出口「等人裁决」（2026-09-17）────────────
#
# 背景（PLATFORM-ISSUES §11.6）：reconcile 只覆盖两类出口（deps 满足 /
# timer 到期）。第三类成因「等一个 agent 做裁决」**没有出口** ⇒ 没有任何
# 机制把它推回裁决者面前。现场取证（TEST_DSH_61 309e2489）：该任务
# wait_kind='timer'、wake_at 已过期 5.4h 却仍是 blocked —— 因为工具层当时
# 不给 waitKind=user 出口，agent 只能把裁决等待**伪装成 timer**。
#
# ⚠ 变异约定（阳性对照）：
#   - 删掉 `blocked_task_needs_arbitration_escalation` 的「出口失效」分支
#     ⇒ `test_escalation_detects_expired_timer_disguise` 转红；
#   - 把工具层 `kind in ("user","external")` 豁免去掉
#     ⇒ `test_tool_block_user_wait_kind_accepted` 转红；
#   - 让 `scan_overdue` 对 arbitration 套用 `_REVIEW_ESCALATABLE_STATUSES`
#     ⇒ `test_arbitration_obligation_escalates_while_blocked` 转红。
#   - 删掉 `unblock_task` 的 `settle_arbitration_on_unblock` 调用
#     ⇒ `test_arbitration_obligation_settled_on_unblock` 转红（审计 P1）。
#   - 删掉判据里的 deps 优先分支 ⇒
#     `test_escalation_helper_ignores_live_deps_exit` 转红（审计 P4）。
#   - 删掉工具层 `waitKind in ("user","external") and deps` 拒绝分支 ⇒
#     `test_tool_block_rejects_user_wait_kind_with_deps` 转红（OCR 评审）。


def test_escalation_helper_detects_no_exit_path():
    """① 无出口（deps 空 且 非 timer）且陈旧 ⇒ 需要升级。"""
    now = int(time.time() * 1000)
    stale = now - 60 * 60 * 1000  # 1h 前更新
    base = {
        "status": "blocked",
        "is_archived": 0,
        "claimed_at": stale,
        "assignee_id": "a1",
        "creator_id": "c1",
        "updated_at": stale,
        "depends_on": [],
        "wait_kind": None,
        "wake_at": None,
    }
    assert blocked_task_needs_arbitration_escalation(base, now)


def test_escalation_helper_detects_user_wait_kind():
    """显式 wait_kind='user'/'external' ⇒ 需要升级（无自动解封路径）。"""
    now = int(time.time() * 1000)
    stale = now - 60 * 60 * 1000
    for kind in ("user", "external"):
        assert blocked_task_needs_arbitration_escalation(
            {
                "status": "blocked",
                "is_archived": 0,
                "claimed_at": stale,
                "assignee_id": "a1",
                "creator_id": "c1",
                "updated_at": stale,
                "depends_on": [],
                "wait_kind": kind,
                "wake_at": None,
            },
            now,
        ), kind


def test_escalation_detects_expired_timer_disguise():
    """⭐ 出口失效：声明了 timer 出口，却已过期超过宽限仍 blocked（现场形态）。"""
    now = int(time.time() * 1000)
    stale = now - 6 * 60 * 60 * 1000
    # 现场 TEST_DSH_61 309e2489 的形态：wake_at 已过期 5.4h
    assert blocked_task_needs_arbitration_escalation(
        {
            "status": "blocked",
            "is_archived": 0,
            "claimed_at": stale,
            "assignee_id": "a1",
            "creator_id": "c1",
            "updated_at": stale,
            "depends_on": [],
            "wait_kind": "timer",
            "wake_at": now - 5 * 60 * 60 * 1000,  # 过期 5h
        },
        now,
    )


def test_escalation_helper_does_not_fire_on_healthy_waits():
    """⚠「该绿的不绿」标定：正常等待不得被升级（防误报淹没）。"""
    now = int(time.time() * 1000)
    stale = now - 60 * 60 * 1000
    base = {
        "status": "blocked",
        "is_archived": 0,
        "claimed_at": stale,
        "assignee_id": "a1",
        "creator_id": "c1",
        "updated_at": stale,
    }
    # 1) 未到期的 timer —— reconcile 会正常解封
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "depends_on": [], "wait_kind": "timer",
         "wake_at": now + 60 * 60 * 1000},
        now,
    )
    # 2) 刚过期但在宽限内 —— 给 reconcile 一个周期
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "depends_on": [], "wait_kind": "timer",
         "wake_at": now - 60 * 1000},
        now,
    )
    # 3) 有未满足依赖 —— reconcile 会管
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "depends_on": ["t1"], "wait_kind": "dependency", "wake_at": None},
        now,
    )
    # 4) 刚 block（未陈旧）—— 留给正常流程
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "updated_at": now - 60 * 1000, "depends_on": [],
         "wait_kind": None, "wake_at": None},
        now,
    )
    # 5) 非 blocked / 已归档 / 无 assignee / 无 creator
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "status": "running", "depends_on": [], "wait_kind": None,
         "wake_at": None},
        now,
    )
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "is_archived": 1, "depends_on": [], "wait_kind": None,
         "wake_at": None},
        now,
    )
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "assignee_id": None, "depends_on": [], "wait_kind": None,
         "wake_at": None},
        now,
    )
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "creator_id": None, "depends_on": [], "wait_kind": None,
         "wake_at": None},
        now,
    )
    # 6) 未 claim 的任务（出生即 blocked 的依赖任务）
    assert not blocked_task_needs_arbitration_escalation(
        {**base, "claimed_at": None, "depends_on": [], "wait_kind": None,
         "wake_at": None},
        now,
    )


def test_escalation_helper_ignores_reason_text():
    """HARD RULE：判据不得读 blocked_reason 文案（换措辞/换语言即绕过）。"""
    now = int(time.time() * 1000)
    stale = now - 60 * 60 * 1000
    base = {
        "status": "blocked",
        "is_archived": 0,
        "claimed_at": stale,
        "assignee_id": "a1",
        "creator_id": "c1",
        "updated_at": stale,
        "depends_on": ["t1"],  # 有出口 ⇒ 不该升级
        "wait_kind": "dependency",
        "wake_at": None,
    }
    # 同一状态、五种措辞（含法文）：结论必须完全一致
    for reason in (
        "等 CEO 裁决",
        "waiting for CEO decision",
        "en attente d'une décision",
        "WAITING ON HUMAN",
        "",
    ):
        assert not blocked_task_needs_arbitration_escalation(
            {**base, "blocked_reason": reason}, now
        ), reason


def test_escalation_helper_ignores_live_deps_exit():
    """审计 P4：有依赖出口（deps 非空）时，即便 wait_kind='user' 也不升级。

    deps 非空 ⇒ reconcile 在管 ——「出口未到」≠「出口失效」；同时防
    `waitKind='user'` + deps 并存（工具层接受该组合）时被误判为无出口。
    """
    now = int(time.time() * 1000)
    stale = now - 60 * 60 * 1000
    assert not blocked_task_needs_arbitration_escalation(
        {
            "status": "blocked",
            "is_archived": 0,
            "claimed_at": stale,
            "assignee_id": "a1",
            "creator_id": "c1",
            "updated_at": stale,
            "depends_on": '["t1"]',  # 字符串形态（DB 行原样）
            "wait_kind": "user",
            "wake_at": None,
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
        kind=VERIFY_KIND)
    queued_b = await ts.create_task(
        pid, "VERIFY: UI B", "verify",
        creator_id=COORD, assignee_id=EXEC,
        source="system",
        kind=VERIFY_KIND)
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
        kind=VERIFY_KIND)
    parked_b = await ts.create_task(
        pid, "VERIFY: UI B", "verify",
        creator_id=COORD, assignee_id=EXEC, source="system",
        kind=VERIFY_KIND)
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
        kind=VERIFY_KIND)
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
    """⚠ **2026-09-17 语义变更**：显式 waitKind='user' 现在**被接受**。

    原断言（Fix B，2026-08-11 slack-clone_01 死锁复盘）是「显式 waitKind
    也不能绕过『无 deps 无 wake_at 硬拒』」—— 当时的理由是：无自动解封路径
    的 block 会永久 parked，拖死整个 VERIFY 队列。

    **改的是断言，不是放宽约束**（本仓纪律要求写清这一点）：
    - 「无出口就永久 parked」这个前提**已被 2026-09-17 的升级兜底推翻**
      —— `audit_missing_arbitration_obligations` 给这类 blocked 登记
      `arbitration` 义务，由 `scan_overdue` 升级到 org parent（PLATFORM-ISSUES §11.6）。
    - **现场取证支持这个变更**：TEST_DSH_61 `309e2489` 的 blocked_reason 写
      「等 CEO waive/补录裁决」，但 `wait_kind` 却是 `'timer'` —— 正是因为
      工具层当时拒绝 `user`，agent 只能把裁决等待**伪装成 timer**，平台因此
      看不到真实意图。拒绝它并不能阻止 parked，只是让它**不可见**。
    - **约束没有消失**：真正危险的是「**既没声明 deps/wakeAt、又没声明
      user/external**」的裸 block ⇒ 仍被硬拒（见
      `test_tool_block_still_rejects_no_path_no_kind`）。
    """
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
    assert result.success is True, result.error
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"
    assert task["wait_kind"] == "user"
    assert task.get("wake_at") is None  # 无自动解封路径（由升级兜底接手）


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

        task_mod._migrated.clear()
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
                               source="system", kind=VERIFY_KIND)
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
                               source="system", kind=VERIFY_KIND)
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


# ── F6 前置项端到端：工具出口 + 升级义务（2026-09-17）──────────


@pytest.mark.asyncio
async def test_tool_block_user_wait_kind_accepted(task_env):
    """⭐ 工具层必须给 waitKind='user'/'external' 真实出口。

    变异约定：把 `tools/tasks/lifecycle.py` 里 `kind in ("user","external")`
    的豁免去掉 ⇒ 本测试转红（agent 又会把裁决等待伪装成 timer）。
    """
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
        blocked_reason="等 CEO 裁决 waive",
        wait_kind="user",
    )
    result = await _call_block_tool(params, pid)
    assert result.success is True, result.error
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"
    assert task["wait_kind"] == "user"
    # 无自动解封路径是有意的：wake_at 必须为空（否则会被 reconcile 当 timer）
    assert task.get("wake_at") is None


@pytest.mark.asyncio
async def test_tool_block_still_rejects_no_path_no_kind(task_env):
    """反向回归：既无 deps/wakeAt 也无 user/external 的裸 block 仍被拒。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid, status="blocked", blocked_reason="没给任何出口"
    )
    result = await _call_block_tool(params, pid)
    assert result.success is False
    assert (await ts.get_task(pid, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_tool_block_rejects_user_wait_kind_with_deps(task_env):
    """OCR 评审（2026-09-17）：deps 与 user/external 语义矛盾 —— 有未满足
    依赖就有自动解封路径，升级判据也不认（「出口未到」≠「出口失效」）。
    同传会让回执谎称「无自动解封、会被升级」⇒ 工具层必须拒绝。

    变异约定：删掉 `waitKind in ("user","external") and deps` 的拒绝分支
    ⇒ 本测试转红。
    """
    ts = TaskService()
    pid = task_env["project_id"]
    blocker = await ts.create_task(
        pid, "Blocker", "d", creator_id=COORD, assignee_id=EXEC
    )
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)

    params = UpdateTaskStatusParams(
        task_id=tid,
        status="blocked",
        blocked_reason="依赖与等人同传",
        depends_on_task_ids=[blocker],
        wait_kind="user",
    )
    result = await _call_block_tool(params, pid)
    assert result.success is False
    assert "conflicts with dependsOnTaskIds" in (result.error or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_arbitration_obligation_registered_for_stale_blocked(gt_env):
    """⭐ 陈旧「等人裁决」blocked ⇒ 登记 arbitration 义务（端到端，状态判据）。

    变异约定：删掉 `audit_missing_arbitration_obligations` 的调用或判据 ⇒ 转红。
    """
    from hiveweave.services.obligation import ObligationLedger

    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI C", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system", kind=VERIFY_KIND)
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    # 现场形态：伪装成 timer，且 wake_at 已过期（_age_task 把 updated_at 拨旧）
    await ts.block_task(pid, tid, "等 CEO 裁决",
                        wait_kind="timer",
                        wake_at=int(time.time() * 1000) - 5 * 3600_000)
    await _age_task(gt_env, tid)

    ledger = ObligationLedger()
    created = await ledger.audit_missing_arbitration_obligations(pid)
    assert tid in created, "陈旧等人裁决 blocked 应登记 arbitration 义务"

    rows = await ledger.get_pending_for_agent(pid, CEO_ID)
    arb = [r for r in rows if r.get("obligation_type") == "arbitration"]
    assert len(arb) == 1
    assert arb[0]["task_id"] == tid

    # 幂等：再跑一次不重复登记
    again = await ledger.audit_missing_arbitration_obligations(pid)
    assert tid not in again


@pytest.mark.asyncio
async def test_arbitration_obligation_escalates_while_blocked(gt_env):
    """⭐ arbitration 义务在任务仍 blocked 时必须能升级（不被 review 白名单拦住）。

    变异约定：让 `scan_overdue` 对 arbitration 套用
    `_REVIEW_ESCALATABLE_STATUSES` ⇒ 本测试转红（正是 F6 要修的洞）。
    """
    from hiveweave.services.obligation import ObligationLedger

    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI D", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system", kind=VERIFY_KIND)
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等 CEO 裁决", wait_kind="user")
    await _age_task(gt_env, tid)

    ledger = ObligationLedger()
    await ledger.audit_missing_arbitration_obligations(pid)
    # 把义务的 deadline 拨到过去，触发升级
    conn = await project_db.ensure_project_db(gt_env["workspace_path"])
    past = int(time.time() * 1000) - 60_000
    await conn.execute(
        "UPDATE obligations SET deadline = ? "
        "WHERE task_id = ? AND obligation_type = 'arbitration'",
        [past, tid],
    )
    await conn.commit()

    # 升级目标打桩：本测试考的是「arbitration 在 blocked 态能否升级」，
    # 不是 org 树解析（后者在 test_audit_test18_fixes.py 覆盖）。
    with (
        patch.object(
            ObligationLedger, "_find_escalation_target",
            new=AsyncMock(return_value=CEO_ID),
        ),
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()) as send,
    ):
        escalated = await ledger.scan_overdue(pid)

    assert any(
        o.get("obligation_type") == "arbitration" for o in escalated
    ), "arbitration 义务应能升级（任务仍在 blocked）"
    assert send.await_count >= 1, "升级必须投递 inbox"

    rows = await ledger.get_pending_for_agent(pid, CEO_ID)
    arb = [r for r in rows if r.get("obligation_type") == "arbitration"][0]
    assert arb["escalated_to"] == CEO_ID
    assert (arb.get("escalation_count") or 0) >= 1


@pytest.mark.asyncio
async def test_arbitration_escalation_skipped_without_org_parent(gt_env):
    """⚠ 覆盖边界（如实声明）：creator 无 org parent（如 root CEO）⇒ 不升级。

    `_find_escalation_target` 返回 None 时 `scan_overdue` 静默 `continue`。
    这不是本批引入的 —— 所有义务类型（merge/review/verify）都同此边界。
    ⇒ 本机制**不覆盖**「creator 是 root」的场景；那条链上由人（用户）兜底。
    """
    from hiveweave.services.obligation import ObligationLedger

    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI F", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system", kind=VERIFY_KIND)
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等 CEO 裁决", wait_kind="user")
    await _age_task(gt_env, tid)

    ledger = ObligationLedger()
    await ledger.audit_missing_arbitration_obligations(pid)
    conn = await project_db.ensure_project_db(gt_env["workspace_path"])
    await conn.execute(
        "UPDATE obligations SET deadline = ? "
        "WHERE task_id = ? AND obligation_type = 'arbitration'",
        [int(time.time() * 1000) - 60_000, tid],
    )
    await conn.commit()

    with (
        patch.object(
            ObligationLedger, "_find_escalation_target",
            new=AsyncMock(return_value=None),  # root，无上级
        ),
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()) as send,
    ):
        escalated = await ledger.scan_overdue(pid)

    assert escalated == []
    assert send.await_count == 0
    # 义务仍在账上（未升级但未丢）—— 下次 creator 有上级时仍会被扫到
    rows = await ledger.get_pending_for_agent(pid, CEO_ID)
    assert any(r.get("obligation_type") == "arbitration" for r in rows)


@pytest.mark.asyncio
async def test_arbitration_obligation_not_escalated_after_unblock(gt_env):
    """反向回归：任务离开 blocked 后，arbitration 义务不再升级。"""
    from hiveweave.services.obligation import ObligationLedger

    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI E", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system", kind=VERIFY_KIND)
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等 CEO 裁决", wait_kind="user")
    await _age_task(gt_env, tid)

    ledger = ObligationLedger()
    await ledger.audit_missing_arbitration_obligations(pid)
    await ts.unblock_task(pid, tid)  # 裁决已下

    conn = await project_db.ensure_project_db(gt_env["workspace_path"])
    past = int(time.time() * 1000) - 60_000
    await conn.execute(
        "UPDATE obligations SET deadline = ? "
        "WHERE task_id = ? AND obligation_type = 'arbitration'",
        [past, tid],
    )
    await conn.commit()

    with (
        patch.object(
            ObligationLedger, "_find_escalation_target",
            new=AsyncMock(return_value=CEO_ID),  # 有上级，排除"无 parent"干扰
        ),
        patch("hiveweave.services.inbox.InboxService.send_message",
              new=AsyncMock()) as send,
    ):
        escalated = await ledger.scan_overdue(pid)

    assert not any(
        o.get("obligation_type") == "arbitration" for o in escalated
    ), "任务已解封 ⇒ 不应升级"
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_arbitration_obligation_settled_on_unblock(gt_env):
    """⭐ 审计 P1：任务离开 blocked ⇒ pending arbitration 义务必须结清。

    不结清则陈旧 pending 行（deadline 已过、escalation_count 续用）会让
    任务**下一次** block 无宽限立即升级。

    变异约定：删掉 `unblock_task` 的 `settle_arbitration_on_unblock`
    调用 ⇒ 本测试转红。
    """
    from hiveweave.services.obligation import ObligationLedger

    await _seed_agents(gt_env)
    ts = TaskService()
    pid = gt_env["project_id"]
    tid = await ts.create_task(pid, "VERIFY: UI G", "verify",
                               creator_id=CEO_ID, assignee_id=QA_ID,
                               source="system", kind=VERIFY_KIND)
    await ts.claim_task(pid, tid, QA_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(pid, tid, "等 CEO 裁决", wait_kind="user")
    await _age_task(gt_env, tid)

    ledger = ObligationLedger()
    await ledger.audit_missing_arbitration_obligations(pid)
    rows = await ledger.get_pending_for_agent(pid, CEO_ID)
    assert any(
        r.get("obligation_type") == "arbitration" for r in rows
    ), "前置：应有 pending arbitration 义务"

    await ts.unblock_task(pid, tid)  # 裁决已下

    rows = await ledger.get_pending_for_agent(pid, CEO_ID)
    assert not any(
        r.get("obligation_type") == "arbitration" for r in rows
    ), "解封后不得残留 pending arbitration 义务"
    # 行仍在账（fulfilled，非删除）—— 审计轨迹可查
    conn = await project_db.ensure_project_db(gt_env["workspace_path"])
    cur = await conn.execute(
        "SELECT status FROM obligations WHERE task_id = ? "
        "AND obligation_type = 'arbitration'",
        [tid],
    )
    assert [r[0] for r in await cur.fetchall()] == ["fulfilled"]
