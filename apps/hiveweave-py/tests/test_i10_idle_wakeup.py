"""I10 (批9) 回归测试 — 无待办空转的调度推进（run 收口评估「下一个该谁动」）。

病灶（s3-clone_13 实测，PLATFORM-ISSUES §十五 I10/P1-6）：09:08:14 最后
一个 run 正常完成后组织空转 **14.9 分钟** —— 唯一兜底唤醒源是
silence_watchdog（阈值即系统最小空转粒度）；期间 [TASK CLOSED] 以
``inbox.wake=0`` 躺进收件箱（通知型不打断）。

修法三步（fixplan §四 I10 方法①②③）：
① run 正常完成收口点评估「下一个该谁动」，存在唯一可推进者 ⇒ 直接唤醒
  （判据纯状态：tasks 非终态 + agent_waits 未满足 + 各 agent 活跃度）；
② task_event_relay 投递目标是唯一可推进者 ⇒ ``wake=1``
  （此前硬编码只认 ``task.blocked``）；
③ 看门狗「无事可做」独立档（全员无待办 ⇒ 长档，与有人待办档区分）。

验收对照（fixplan，可机检）：
- 「run 完成 + 存在唯一可推进者」⇒ 唤醒触发（trigger 调用 / wake 位证据）；
- 「无待办」⇒ 不空唤醒（防循环）+ 看门狗独立档落位。

测试纪律：真实 per-project DB（temp workspace，照
test_adr001_idle_single_source 的 env 模式）；**不 monkeypatch
aiosqlite.Connection**（会干扰连接生命周期造成 flake，上一批踩过）。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.realtime.event_bus import status_event_bus
from hiveweave.services import game_time
from hiveweave.services import task as task_mod
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.idle_wakeup import (
    IDLE_WAKE_MAX_CONSECUTIVE,
    clear_sole_advancer_cache,
    compute_sole_advancer,
    maybe_wake_sole_advancer,
    reset_wake_budget,
)
from hiveweave.services.task import TaskService
from hiveweave.services.task_event_relay import TaskEventRelay

PROJECT_ID = "test-i10-project"
CEO_ID = "test-i10-ceo"
EXECUTOR_ID = "test-i10-executor"
OTHER_EXECUTOR_ID = "test-i10-executor-b"


@pytest.fixture(autouse=True)
def _clean_module_state():
    """清 idle_wakeup 模块缓存 + game_time 内存态，防测试间污染。"""
    from hiveweave.services import idle_wakeup

    clear_sole_advancer_cache()
    idle_wakeup._wake_budget.clear()
    game_time._states.clear()
    yield
    clear_sole_advancer_cache()
    idle_wakeup._wake_budget.clear()
    game_time._states.clear()


@pytest.fixture
async def env():
    from hiveweave.services import task as task_mod_

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.clear()
        task_mod_._migrated.clear()
        for aid in (CEO_ID, EXECUTOR_ID, OTHER_EXECUTOR_ID):
            project_db._agent_cache[aid] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            for aid in (CEO_ID, EXECUTOR_ID, OTHER_EXECUTOR_ID):
                project_db._agent_cache.pop(aid, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(env, agent_id, name, role="executor",
                        created_at=None, last_active_at=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, status, "
        "created_at, last_active_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
        [agent_id, PROJECT_ID, name, role, ts, last_active_at])
    await conn.commit()


async def _insert_task(env, *, status, assignee_id=None, creator_id=CEO_ID,
                       reviewer_id=None, claimed_at=-1, title="I10 task"):
    """claimed_at=-1 表示 NULL（未认领）。"""
    task_mod._migrated.clear()
    await task_mod._ensure_schema(PROJECT_ID)
    tid = str(uuid.uuid4())
    old = _now_ms() - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    cols = ("id, project_id, title, status, progress, creator_id, "
            "assignee_id, created_at, updated_at, is_archived")
    vals = [tid, PROJECT_ID, title, status, 0, creator_id,
            assignee_id, old, old, 0]
    if reviewer_id is not None:
        cols += ", reviewer_id"
        vals.append(reviewer_id)
    if claimed_at != -1:
        cols += ", claimed_at"
        vals.append(claimed_at)
    ph = ",".join("?" for _ in vals)
    await conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({ph})", vals)
    await conn.commit()
    return tid


async def _insert_wait(env, agent_id, expires_at):
    """未满足 wait 行（cleared_at IS NULL）—— 合法停泊证据。"""
    wait_contract_module._migrated.clear()
    from hiveweave.services.wait_contract import wait_contract_service

    await wait_contract_service.list_all_active(PROJECT_ID)  # ensure schema
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "expires_at, created_at) VALUES (?, ?, ?, 'task', 'i10', ?, ?)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, expires_at, _now_ms()])
    await conn.commit()


async def _insert_ask(env, from_id, to_id):
    """未解除回复契约（outstanding ask）—— 义务判据的 ask 半边。"""
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, wake, reply_contract_id) "
        "VALUES (?, ?, ?, 'ask?', 0, ?, 'ask', 1, 1, ?)",
        [str(uuid.uuid4()), from_id, to_id, _now_ms(), f"rc-{uuid.uuid4().hex[:8]}"])
    await conn.commit()


def _started_mock(started=1):
    from hiveweave.db import meta as meta_db

    async def fake_query_one(sql, params=None):
        if "is_started" in sql:
            return {"is_started": started}
        return None

    return patch.object(meta_db, "query_one", fake_query_one)


# ── §1 compute_sole_advancer：状态判据 ──────────────────────


async def test_sole_advancer_single_duty_holder(env):
    """run 完成 + 唯一义务人 ⇒ 返回该 agent（判据=状态：claimed running）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    with _started_mock(1):
        sole = await compute_sole_advancer(PROJECT_ID)
    assert sole is not None
    assert sole["agent_id"] == EXECUTOR_ID
    assert sole["obligations"], "义务清单非空（闭式 get_open_work_obligations）"


async def test_sole_advancer_none_when_no_open_work(env):
    """「无待办」⇒ None（防循环第一道闸：不空唤醒）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="closed", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    with _started_mock(1):
        assert await compute_sole_advancer(PROJECT_ID) is None


async def test_sole_advancer_none_when_two_duty_holders(env):
    """两个义务人 ⇒ 不唯一 ⇒ None（只对「唯一可推进者」定向唤醒）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_agent(env, OTHER_EXECUTOR_ID, "I10-EXEC-B", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    await _insert_task(env, status="running", assignee_id=OTHER_EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    with _started_mock(1):
        assert await compute_sole_advancer(PROJECT_ID) is None


async def test_sole_advancer_wait_parked_agent_excluded(env):
    """唯一义务人但挂未过期 wait（合法停泊）⇒ None；过期 ⇒ 恢复候选。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    await _insert_wait(env, EXECUTOR_ID, _now_ms() + 10 * 60 * 1000)
    with _started_mock(1):
        assert await compute_sole_advancer(PROJECT_ID) is None
    # wait 过期 → 停泊解除 → 义务恢复
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE agent_waits SET expires_at = ?",
                       [_now_ms() - 1000])
    await conn.commit()
    with _started_mock(1):
        sole = await compute_sole_advancer(PROJECT_ID)
    assert sole is not None and sole["agent_id"] == EXECUTOR_ID


async def test_sole_advancer_processing_agent_excluded(env, monkeypatch):
    """在干活的（processing）不是「下一个该谁动」的对象。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(EXECUTOR_ID, PROJECT_ID)])
    with _started_mock(1):
        assert await compute_sole_advancer(PROJECT_ID) is None


async def test_sole_advancer_excludes_finished_agent(env):
    """收口排除：刚完成 run 的 agent 不参与评估（它自己的出口链负责）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    with _started_mock(1):
        sole = await compute_sole_advancer(
            PROJECT_ID, exclude_agent_ids=frozenset({EXECUTOR_ID}))
    assert sole is None


async def test_sole_advancer_off_duty_project_none(env):
    """项目下班（is_started=0）⇒ None（收口评估不是下班的豁免通道）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    with _started_mock(0):
        assert await compute_sole_advancer(PROJECT_ID) is None


async def test_sole_advancer_ask_debt_counts_as_duty(env):
    """未解除回复契约（outstanding ask）也算义务（ADR-001 has_open_work
    同口径）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_agent(env, OTHER_EXECUTOR_ID, "I10-EXEC-B", created_at=old)
    await _insert_ask(env, OTHER_EXECUTOR_ID, EXECUTOR_ID)
    with _started_mock(1):
        sole = await compute_sole_advancer(PROJECT_ID)
    assert sole is not None and sole["agent_id"] == EXECUTOR_ID
    assert sole["ask_senders"]


# ── §2 maybe_wake_sole_advancer：触发 + 防循环 ───────────────


async def _wake_env(env, monkeypatch):
    """公共脚手架：单义务人 executor + trigger 双 mock。返回 (mock_sub, mock_coord)。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    mock_sub = AsyncMock()
    mock_coord = AsyncMock()
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [])
    return mock_sub, mock_coord


async def test_wake_triggers_sole_advancer(env, monkeypatch):
    mock_sub, mock_coord = await _wake_env(env, monkeypatch)
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord):
        woken = await maybe_wake_sole_advancer(PROJECT_ID, source="test")
    assert woken == EXECUTOR_ID
    assert mock_sub.await_count == 1
    assert mock_sub.await_args.args[0] == EXECUTOR_ID
    assert mock_sub.await_args.kwargs.get("force") is True, (
        "义务证据来自账本状态，必须穿透未读守卫（与看门狗穿透唤醒同据）")
    assert mock_coord.await_count == 0


async def test_wake_coordinator_role_uses_coordinator_trigger(
        env, monkeypatch):
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    # CEO 是 creator：submitted 待审 ⇒ 义务人
    await _insert_task(env, status="submitted", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    mock_sub = AsyncMock()
    mock_coord = AsyncMock()
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [])
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord):
        woken = await maybe_wake_sole_advancer(PROJECT_ID, source="test")
    assert woken == CEO_ID
    assert mock_coord.await_count == 1
    assert mock_sub.await_count == 0


async def test_wake_cap_blocks_consecutive_no_output(env, monkeypatch):
    """连唤上限（DSH maxConsecutiveWakes 同型）：同 agent 连唤达上限后
    停唤 —— agent 零产出（last_active_at 未推进）时第 4 次返回 None。"""
    mock_sub, mock_coord = await _wake_env(env, monkeypatch)
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord):
        for i in range(IDLE_WAKE_MAX_CONSECUTIVE):
            assert await maybe_wake_sole_advancer(
                PROJECT_ID, source="test") == EXECUTOR_ID, f"wake #{i + 1}"
        # 上限已到，零产出 ⇒ 拒发（防无限唤醒循环）
        assert await maybe_wake_sole_advancer(PROJECT_ID, source="test") is None
    assert mock_sub.await_count == IDLE_WAKE_MAX_CONSECUTIVE


async def test_wake_cap_resets_on_real_output(env, monkeypatch):
    """预算重置：last_active_at 推进过上次唤醒时刻（真实产出）⇒ 可再唤。"""
    mock_sub, mock_coord = await _wake_env(env, monkeypatch)
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord):
        for _ in range(IDLE_WAKE_MAX_CONSECUTIVE):
            await maybe_wake_sole_advancer(PROJECT_ID, source="test")
        assert await maybe_wake_sole_advancer(
            PROJECT_ID, source="test") is None  # 上限内零产出
        # agent 真实产出（last_active_at 推进到唤醒之后）
        conn = await ensure_project_db(env["workspace_path"])
        await conn.execute(
            "UPDATE agents SET last_active_at = ?", [_now_ms() + 1])
        await conn.commit()
        assert await maybe_wake_sole_advancer(
            PROJECT_ID, source="test") == EXECUTOR_ID, "产出推进 ⇒ 预算重置"


async def test_wake_budget_window_expiry_resets(env, monkeypatch):
    """预算窗口过期 ⇒ 计数作废（防陈旧计数永久关闭唤醒）。"""
    from hiveweave.services import idle_wakeup

    mock_sub, _ = await _wake_env(env, monkeypatch)
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator", AsyncMock()):
        for _ in range(IDLE_WAKE_MAX_CONSECUTIVE):
            await maybe_wake_sole_advancer(PROJECT_ID, source="test")
        key = (PROJECT_ID, EXECUTOR_ID)
        # 把上次唤醒时刻拨回窗口之外
        entry = idle_wakeup._wake_budget[key]
        entry["last_wake_ms"] -= idle_wakeup.IDLE_WAKE_BUDGET_WINDOW_MS + 1
        assert await maybe_wake_sole_advancer(
            PROJECT_ID, source="test") == EXECUTOR_ID


# ── §3 task_event_relay：唯一可推进者 wake=1 ─────────────────


def _relay_event(event_id, event_type, task_id, actor_id=None):
    return {
        "id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "actor_id": actor_id,
        "payload": "{}",
        "created_at": _now_ms(),
    }


async def test_relay_marks_wake_for_sole_advancer_recipient(env):
    """收件人是唯一可推进者 ⇒ wake=1；同批非义务收件人维持 wake=0。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    # t-open：exec 名下唯一开放义务（claimed running）⇒ exec 是唯一可推进者
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old,
                       title="唯一开放任务")
    # t-done：事件所指的已关闭任务（recipients = creator + assignee）
    closed_id = await _insert_task(
        env, status="closed", assignee_id=EXECUTOR_ID,
        creator_id=CEO_ID, claimed_at=old, title="已关闭任务")

    captured: list[dict] = []

    async def fake_send(self, **kwargs):
        captured.append(kwargs)
        return {"id": str(uuid.uuid4())}

    with _started_mock(1):
        clear_sole_advancer_cache()
        with patch("hiveweave.services.inbox.InboxService.send_message",
                   fake_send):
            await TaskEventRelay()._process_one(
                PROJECT_ID, _relay_event("evt-1", "task.closed", closed_id))

    by_recipient = {c["to_agent_id"]: c for c in captured}
    assert set(by_recipient) == {CEO_ID, EXECUTOR_ID}
    assert by_recipient[EXECUTOR_ID]["wake"] is True, (
        "唯一可推进者的定向投递必须 wake=1（I10 方法②）")
    assert by_recipient[CEO_ID]["wake"] is False, (
        "非唯一可推进者的通知型投递维持 FYI（防全员广播唤醒风暴）")


async def test_relay_keeps_fyi_when_no_sole_advancer(env):
    """无待办 ⇒ 无唯一可推进者 ⇒ 投递维持 wake=0（不空唤醒）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    closed_id = await _insert_task(
        env, status="closed", assignee_id=EXECUTOR_ID,
        creator_id=CEO_ID, claimed_at=old, title="已关闭任务")

    captured: list[dict] = []

    async def fake_send(self, **kwargs):
        captured.append(kwargs)
        return {"id": str(uuid.uuid4())}

    with _started_mock(1):
        clear_sole_advancer_cache()
        with patch("hiveweave.services.inbox.InboxService.send_message",
                   fake_send):
            await TaskEventRelay()._process_one(
                PROJECT_ID, _relay_event("evt-2", "task.closed", closed_id))

    assert captured
    assert all(c["wake"] is False for c in captured), (
        "无待办时投递不打断（防循环）")


async def test_relay_blocked_event_still_wakes_regardless(env):
    """task.blocked 职责信号维持既有 wake=1 语义（不因新分支回退）。"""
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "I10-CEO", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old)
    # 全部终态（无可推进者），blocked 事件仍唤醒 creator
    blocked_id = await _insert_task(
        env, status="blocked", assignee_id=EXECUTOR_ID,
        creator_id=CEO_ID, claimed_at=old, title="被阻塞任务")

    captured: list[dict] = []

    async def fake_send(self, **kwargs):
        captured.append(kwargs)
        return {"id": str(uuid.uuid4())}

    with _started_mock(1):
        clear_sole_advancer_cache()
        with patch("hiveweave.services.inbox.InboxService.send_message",
                   fake_send):
            await TaskEventRelay()._process_one(
                PROJECT_ID, _relay_event("evt-3", "task.blocked", blocked_id))

    assert captured
    assert all(c["wake"] is True for c in captured)


# ── §4 看门狗「无事可做」独立档 ──────────────────────────────


async def test_advancable_state_predicate(env):
    """_project_has_advancable_state 四态：非终态任务 / 未满足 wait / 全无。"""
    svc = GameTimeService()
    old = _now_ms() - 40 * 60 * 1000
    # 1) 无任何行 ⇒ False（查询得到但为空）
    assert await svc._project_has_advancable_state(PROJECT_ID) is False
    # 2) 非终态任务 ⇒ True
    tid = await _insert_task(env, status="running",
                             assignee_id=EXECUTOR_ID, creator_id=CEO_ID,
                             claimed_at=old)
    assert await svc._project_has_advancable_state(PROJECT_ID) is True
    # 3) 全部终态 ⇒ False
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE tasks SET status = 'closed' WHERE id = ?",
                       [tid])
    await conn.commit()
    assert await svc._project_has_advancable_state(PROJECT_ID) is False
    # 4) 未满足 wait（cleared_at IS NULL）⇒ True；清除后 ⇒ False
    await _insert_wait(env, EXECUTOR_ID, _now_ms() + 60 * 1000)
    assert await svc._project_has_advancable_state(PROJECT_ID) is True
    await conn.execute("UPDATE agent_waits SET cleared_at = ?", [_now_ms()])
    await conn.commit()
    assert await svc._project_has_advancable_state(PROJECT_ID) is False


def _silent_agent_mock(role="executor"):
    return lambda aid: SimpleNamespace(
        disposition="runnable",
        project_id=PROJECT_ID,
        config={"role": role},
    )


async def _run_silent_check(monkeypatch):
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        _silent_agent_mock())
    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", mock_trigger)
    with _started_mock(1), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus):
        await GameTimeService()._check_silent_agents(PROJECT_ID)
    return mock_trigger, mock_bus


async def test_watchdog_ask_debt_counts_as_advancable_short_tier(
        env, monkeypatch):
    """批 9 审计 P2-2 口径对齐：**ask 债也是可推进状态** ⇒ 走 10min 短档。

    组织零任务 + 零未满足 wait，但存在未读 expect_report ask（ask-only
    组织）——修复前判据不数 ask ⇒ 被误判「无事可做」走 30min 长档，该
    债务人的看门狗兜底退化。15min 沉默 > 10min 短档 ⇒ 必须唤醒。
    """
    old = _now_ms() - 15 * 60 * 1000  # 15min 沉默：>10min 短档、<30min 长档
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old,
                        last_active_at=old)
    await _insert_agent(env, OTHER_EXECUTOR_ID, "I10-EXEC-B",
                        created_at=old, last_active_at=old)
    # ask 债让 executor 落入检测（不被合法 idle 豁免），且本身即「有事」
    await _insert_ask(env, OTHER_EXECUTOR_ID, EXECUTOR_ID)
    game_time._states[PROJECT_ID] = {"silence_trackers": {}}

    mock_trigger, mock_bus = await _run_silent_check(monkeypatch)

    assert mock_trigger.await_count == 1, (
        "ask 债 = 可推进状态 ⇒ ask-only 组织必须走短档（15min > 10min 触发）")
    assert _health_error_events(mock_bus), "红框必须同时举起来"


async def test_watchdog_truly_nothing_todo_zero_triggers(env, monkeypatch):
    """真正的「无事可做」（零任务 + 零 wait + 零 ask）：全员被合法 idle
    豁免 ⇒ 零触发零红框（15min < 30min 长档的语义由豁免路径承载）。"""
    old = _now_ms() - 15 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old,
                        last_active_at=old)
    await _insert_agent(env, OTHER_EXECUTOR_ID, "I10-EXEC-B",
                        created_at=old, last_active_at=old)
    game_time._states[PROJECT_ID] = {"silence_trackers": {}}

    mock_trigger, mock_bus = await _run_silent_check(monkeypatch)

    assert mock_trigger.await_count == 0, "真无事可做 ⇒ 无人在检测面 ⇒ 零触发"
    assert _health_error_events(mock_bus) == []


async def test_watchdog_pending_work_tier_wakes_at_short_threshold(
        env, monkeypatch):
    """有人待办档：组织存在非终态任务 + 检测对象沉默 15min ⇒ 正常唤醒
    （≥10min 短档）+ 红框 —— 分档不削弱有人待办时的兜底。"""
    old = _now_ms() - 15 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "I10-EXEC", created_at=old,
                        last_active_at=old)
    await _insert_task(env, status="running", assignee_id=EXECUTOR_ID,
                       creator_id=CEO_ID, claimed_at=old)
    game_time._states[PROJECT_ID] = {"silence_trackers": {}}

    mock_trigger, mock_bus = await _run_silent_check(monkeypatch)

    assert mock_trigger.await_count == 1, (
        "有人待办 + 沉默超短档 ⇒ 必须唤醒（分档只放宽无事可做形态）")
    assert _health_error_events(mock_bus), "红框必须同时举起来"


def _health_error_events(mock_bus) -> list[dict]:
    out = []
    for c in mock_bus.await_args_list:
        evt = (c.args[1] if len(c.args) > 1 else c.kwargs.get("event", {}))
        if evt.get("health") == "error":
            out.append(evt)
    return out


# ── §5 收口点接线守卫 ───────────────────────────────────────


def test_run_completion_wiring_source_guard():
    """completion.py 正常完成收口点必须接线 idle_wakeup（防接线回潮）。"""
    from hiveweave.agents import completion

    src = Path(completion.__file__).read_text(encoding="utf-8")
    assert "maybe_wake_sole_advancer" in src, (
        "run 收口点未接线「下一个该谁动」评估（I10 方法①回潮）")
    assert "exclude_agent_ids=frozenset({agent.id})" in src, (
        "收口排除缺失：刚完成 run 的 agent 不得自唤（防唤醒循环）")
    # 接线必须挂在正常完成路径上（exit_decision.ok），停泊/失败路径不评估
    assert "exit_decision.ok and not _stall_parked" in src
