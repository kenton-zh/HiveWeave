"""Silence watchdog — agent 失联观测（潮汐事故盲区覆盖）.

被测文件: src/hiveweave/services/game_time.py
被测方法: GameTimeService._check_silent_agents(project_id)

测试策略（对齐 test_task_service.py）:
  - tempfile 创建真实 per-project DB；patch meta_db.get_project_workspace 路由
  - patch meta_db.query_one 控制 projects.is_started（上班豁免）
  - trigger_subordinate / status_event_bus.publish_stream_event /
    InboxService.send_message 以 AsyncMock 捕获（不发真实 LLM 调用）
  - agent_manager.list_processing / get_agent 用 monkeypatch 控制豁免分支
  - 每个测试清空 game_time._states，避免 tracker 跨用例污染
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
from hiveweave.services import wait_contract as wait_contract_module
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.system_state import system_state
from hiveweave.services.wait_contract import wait_contract_service

PROJECT_ID = "test-silence-project"
CEO_ID = "test-ceo"
EXECUTOR_ID = "test-executor"


@pytest.fixture(autouse=True)
def clean_states():
    """每个测试前后清空 game_time 内存态，防止 tracker 跨用例污染."""
    game_time._states.clear()
    yield
    game_time._states.clear()


@pytest.fixture
async def env():
    """真实 per-project DB（temp workspace）+ meta_db 路由 patch.

    清理时先弹出并关闭缓存连接再删临时目录（Windows 文件占用）。
    - task_mod._migrated 键控 project_id：同 PROJECT_ID 跨 temp workspace
      复用时必须 discard，否则新库 tasks 表跳过补列（due_at 等），
      探针查询炸 schema（fail-open 穿透误唤醒）。
    - project_db._agent_cache 预填：inbox 探针按 agent_id 路由 DB，
      测试未注册 Meta DB，须直接映射到 temp workspace。
    """
    from hiveweave.services import task as task_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.discard(PROJECT_ID)
        task_mod._migrated.discard(PROJECT_ID)
        project_db._agent_cache[CEO_ID] = workspace_path
        project_db._agent_cache[EXECUTOR_ID] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            project_db._agent_cache.pop(CEO_ID, None)
            project_db._agent_cache.pop(EXECUTOR_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(env, agent_id, name, parent_id=None,
                        created_at=None, status="active", role="executor",
                        permission_type=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    if permission_type is None:
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [agent_id, PROJECT_ID, name, role, parent_id, status, ts, ts])
    else:
        await conn.execute(
            "INSERT INTO agents (id, project_id, name, role, parent_id, "
            "status, permission_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [agent_id, PROJECT_ID, name, role, parent_id, status,
             permission_type, ts, ts])
    await conn.commit()


async def _insert_chat(env, agent_id, role, created_at):
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO chat_messages (id, agent_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), agent_id, role, "x", created_at])
    await conn.commit()


async def _insert_work_log(env, agent_id, created_at):
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO work_logs (id, agent_id, project_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, created_at])
    await conn.commit()


async def _insert_error_log(env, agent_id, summary, created_at):
    """P0-4: type='error' 的 work_log —— 沉默看门狗的「失败签名」来源."""
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO work_logs (id, agent_id, project_id, type, summary, "
        "created_at) VALUES (?, ?, ?, 'error', ?, ?)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, summary, created_at])
    await conn.commit()


async def _insert_open_task(env, assignee_id, *, status="running"):
    """TEST21 M7: silence only fires when agent has duty — seed an open task.

    Raw INSERT (no TaskService.start_task) so we do not write a fresh work_log
    that would reset the silence baseline to "just now".
    ADR-001：claimed_at 必填——闭式义务判定以 claimed_at 为锚点（生产
    claim/unblock 路径必写），raw INSERT 不带会让 assignee 义务漏判。
    """
    from hiveweave.services import task as task_mod

    task_mod._migrated.discard(PROJECT_ID)
    await task_mod._ensure_schema(PROJECT_ID)
    tid = str(uuid.uuid4())
    now = _now_ms()
    # Use an old updated_at so the task itself does not look like recent activity
    old = now - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, description, status, "
        "progress, creator_id, assignee_id, created_at, updated_at, "
        "claimed_at, is_archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        [tid, PROJECT_ID, "Silence duty task", "keep agent on duty",
         status, 20 if status == "running" else 10,
         CEO_ID, assignee_id, old, old, old],
    )
    await conn.commit()
    return tid


async def _insert_wait(env, agent_id, expires_at):
    """落盘一条 wait contract（先 list_all_active 确保 schema 已建）."""
    await wait_contract_service.list_all_active(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "wake_on, expires_at, created_at) VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
        [str(uuid.uuid4()), agent_id, PROJECT_ID, "user", "user",
         expires_at, _now_ms()])
    await conn.commit()


def _seed_state():
    """_check_silent_agents 需要 _states 里有该项目条目（生产由 tick 保证）."""
    game_time._states[PROJECT_ID] = {"project_id": PROJECT_ID}


def _trackers():
    return game_time._states[PROJECT_ID].get("silence_trackers", {})


def _health_events(mock_bus, health=None):
    """从 publish_stream_event 调用中提取 agent_health 事件."""
    events = []
    for call in mock_bus.await_args_list:
        event = call.args[1]
        if event.get("type") == "agent_health" and (
                health is None or event.get("health") == health):
            events.append(event)
    return events


def _started_mock(is_started=1):
    return patch("hiveweave.db.meta.query_one",
                 new=AsyncMock(return_value={"is_started": is_started}))


# ── 沉默超阈值 → 触发 + 红框 + 上级通知 ─────────────────────


async def test_silent_agent_triggers_wake_redbox_and_superior_notify(env, monkeypatch):
    """沉默 40 分钟：自醒 + 红框；持续失联只打日志，不 inbox 催上级."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)

    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID, created_at=old)
    await _insert_open_task(env, EXECUTOR_ID)
    _seed_state()

    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    svc = GameTimeService()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", mock_inbox):
        await svc._check_silent_agents(PROJECT_ID)

    triggered_ids = [c.args[0] for c in mock_trigger.await_args_list]
    assert EXECUTOR_ID in triggered_ids
    assert CEO_ID not in triggered_ids

    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    event = errors[0]
    assert event["agentId"] == EXECUTOR_ID
    assert event["projectId"] == PROJECT_ID
    assert "SILENCE WATCHDOG" in event["message"]
    assert isinstance(event["at"], int)

    assert mock_inbox.await_count == 0

    tracker = _trackers()[EXECUTOR_ID]
    assert tracker["flagged"] is True
    assert tracker["wake_ts"] > 0
    assert tracker["notify_ts"] > 0
    assert tracker["notify_count"] >= 1


# ── 豁免场景不误报 ──────────────────────────────────────────


async def _assert_no_action(env, svc):
    """通用断言：豁免生效时不触发 / 不广播 / 不通知."""
    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", mock_inbox):
        await svc._check_silent_agents(PROJECT_ID)
    assert mock_trigger.await_count == 0
    assert _health_events(mock_bus) == []
    assert mock_inbox.await_count == 0


async def test_exempt_when_project_not_started(env, monkeypatch):
    """is_started=0 → 整个检查跳过（对齐 _check_stalled Case 4）."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 60 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    _seed_state()
    with _started_mock(0):
        await _assert_no_action(env, GameTimeService())


async def test_exempt_when_system_paused(env, monkeypatch):
    """系统 paused → 不观测."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    monkeypatch.setattr(system_state, "_paused", True)
    old = _now_ms() - 60 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


async def test_exempt_when_processing(env, monkeypatch):
    """processing 中的 agent 不观测."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(EXECUTOR_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 60 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


async def test_exempt_when_waiting_with_live_contract(env, monkeypatch):
    """waiting_human disposition + 未过期 wait contract → 合法等待，不观测."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: SimpleNamespace(disposition="waiting_human"))
    old = _now_ms() - 60 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    await _insert_wait(env, EXECUTOR_ID, expires_at=_now_ms() + 3600_000)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


async def test_expired_contract_does_not_exempt(env, monkeypatch):
    """wait contract 已过期 → 不再豁免，照常举红框."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: SimpleNamespace(disposition="waiting_human"))
    old = _now_ms() - 20 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    await _insert_wait(env, EXECUTOR_ID, expires_at=_now_ms() - 60_000)
    await _insert_open_task(env, EXECUTOR_ID)
    _seed_state()

    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    assert len(_health_events(mock_bus, "error")) == 1
    assert any(c.args[0] == EXECUTOR_ID for c in mock_trigger.await_args_list)


async def test_recent_output_or_young_agent_no_flag(env, monkeypatch):
    """近期有 work_log 产出 / 新建 agent（created_at 基线未满 10 min）→ 不观测."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    now = _now_ms()
    # agent-1: 创建很久但 2 分钟前有 work_log 产出
    await _insert_agent(env, "agent-1", "甲", created_at=now - 3600_000)
    await _insert_work_log(env, "agent-1", created_at=now - 2 * 60 * 1000)
    # agent-2: 5 分钟前刚建，无任何产出（created_at 基线保护期）
    await _insert_agent(env, "agent-2", "乙", created_at=now - 5 * 60 * 1000)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


async def test_user_messages_do_not_count_as_output(env, monkeypatch):
    """只有 user 角色消息（背景上下文）不算产出 → 老 agent 照常举红框."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    now = _now_ms()
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=now - 3600_000)
    # 1 小时前收到过背景 user 消息（trigger 上下文），但从未自己产出
    await _insert_chat(env, EXECUTOR_ID, "user", created_at=now - 3600_000)
    await _insert_open_task(env, EXECUTOR_ID)
    _seed_state()

    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    assert len(_health_events(mock_bus, "error")) == 1
    assert any(c.args[0] == EXECUTOR_ID for c in mock_trigger.await_args_list)


# ── 恢复产出 → ok 解除红框 ──────────────────────────────────


async def test_recovery_broadcasts_ok_and_clears_flag(env, monkeypatch):
    """先沉默举红框 → 恢复产出 → 下一轮广播 health=ok 且不再触发."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", created_at=old)
    await _insert_open_task(env, EXECUTOR_ID)
    _seed_state()

    svc = GameTimeService()
    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", AsyncMock()):
        # 第 1 轮：沉默 → error 红框
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert _trackers()[EXECUTOR_ID]["flagged"] is True

        # 恢复产出（assistant 消息）→ 第 2 轮：ok 解除，不再触发
        mock_trigger.reset_mock()
        mock_bus.reset_mock()
        await _insert_chat(env, EXECUTOR_ID, "assistant", _now_ms())
        await svc._check_silent_agents(PROJECT_ID)

        oks = _health_events(mock_bus, "ok")
        assert len(oks) == 1
        assert oks[0]["agentId"] == EXECUTOR_ID
        assert oks[0]["projectId"] == PROJECT_ID
        assert oks[0]["message"] == ""
        assert mock_trigger.await_count == 0
        assert _trackers()[EXECUTOR_ID]["flagged"] is False


# ── 冷却期内不重复 ──────────────────────────────────────────


async def test_cooldown_suppresses_repeat_wake_and_notify(env, monkeypatch):
    """同一 agent：wake 冷却内不重复自醒；notify 退避只记 tracker，不 inbox 催上级."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID, created_at=old)
    await _insert_open_task(env, EXECUTOR_ID)
    _seed_state()

    svc = GameTimeService()
    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", mock_inbox):
        # 第 1 轮：wake + error；escalate 只落 tracker
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert mock_inbox.await_count == 0
        assert mock_trigger.await_count == 1
        assert _trackers()[EXECUTOR_ID]["notify_count"] >= 1

        # 第 2 轮（冷却内）：不重复 wake / 不 inbox
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert mock_inbox.await_count == 0
        assert mock_trigger.await_count == 1

        # 拨回 tracker 时间戳模拟冷却过期 → 第 3 轮再次 wake；仍无上级 inbox
        tracker = _trackers()[EXECUTOR_ID]
        tracker["wake_ts"] -= game_time.STALL_COOLDOWN_MS
        tracker["notify_ts"] -= game_time.SILENCE_NOTIFY_BACKOFF_MS[1]
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 2
        assert mock_inbox.await_count == 0
        assert mock_trigger.await_count == 2
        assert _trackers()[EXECUTOR_ID]["notify_count"] >= 2


async def test_legal_idle_without_duty_skips_silence(env, monkeypatch):
    """TEST21 M7: no obligations / asks / waits → legal idle, no red-box."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "天线", parent_id=CEO_ID, created_at=old)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


# ── P0-4 看门狗叫错人：legal idle 组织上下文 + 6+ 次同类终止升级上级 ──


async def test_legal_idle_with_healthy_subordinate_stays_exempt(env, monkeypatch):
    """P0-4 ①: 中层名下无义务、直属下级也未深陷（error 终止 < 阈值）→
    仍合法空闲，不误唤醒（防误报）。"""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    now = _now_ms()
    old = now - 40 * 60 * 1000
    await _insert_agent(env, "coord-3", "中层", role="技术协调员",
                        permission_type="coordinator", created_at=old)
    await _insert_agent(env, "leaf-3", "叶子", parent_id="coord-3",
                        created_at=old)
    # 下级仅 2 次 error（< 阈值 6），不算深陷
    for i in range(2):
        await _insert_error_log(env, "leaf-3", "tool_failed: x",
                                created_at=now - 60 * 1000)
    _seed_state()
    with _started_mock(1):
        await _assert_no_action(env, GameTimeService())


async def test_legal_idle_with_distressed_subordinate_triggers_manager(
        env, monkeypatch):
    """P0-4 ①: 中层名下无义务但其直属下级反复同签名失败（≥ 阈值）→
    不再判合法空闲，走唤醒 + 红框（看门狗叫错人回归）。"""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    now = _now_ms()
    old = now - 40 * 60 * 1000
    await _insert_agent(env, "coord-2", "中层", role="技术协调员",
                        permission_type="coordinator", created_at=old)
    await _insert_agent(env, "leaf-2", "叶子", parent_id="coord-2",
                        created_at=old)
    for i in range(game_time.DISTRESS_SAME_SIG_REPEAT + 2):
        await _insert_error_log(env, "leaf-2", "tool_failed: fix",
                                created_at=now - 20 * 60 * 1000 + i * 1000)
    _seed_state()

    mock_trigger_coord = AsyncMock()
    mock_trigger_sub = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator",
               mock_trigger_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               mock_trigger_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               mock_inbox):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    errors = _health_events(mock_bus, "error")
    # 中层被唤醒 + 红框（不再豁免为 legal idle）
    assert any(e["agentId"] == "coord-2" for e in errors)
    triggered = ([c.args[0] for c in mock_trigger_coord.await_args_list]
                 + [c.args[0] for c in mock_trigger_sub.await_args_list])
    assert "coord-2" in triggered
    # 中层尚无 parent / wake_count 未达阈值 → 未 inbox 上级
    assert mock_inbox.await_count == 0


async def test_subordinate_repeated_failure_escalates_to_superior(env, monkeypatch):
    """P0-4 ②: 下级 wake_count ≥ 阈值且仍重复同签名失败 → inbox 上级 + 唤醒上级."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    now = _now_ms()
    # 错误集中在 20 min 前：落在 30min 窗口内，且距现在 ≥10min（够沉默）
    err_ts = now - 20 * 60 * 1000
    await _insert_agent(env, "sub-2", "反复失败", parent_id="mgmt-2",
                        created_at=now - 120 * 60 * 1000)
    await _insert_open_task(env, "sub-2")  # 有义务，非 legal idle
    for i in range(game_time.DISTRESS_SAME_SIG_REPEAT + 1):
        await _insert_error_log(env, "sub-2", "tool_failed: same",
                                created_at=err_ts + i * 1000)
    _seed_state()
    # 预置 wake_count = 阈值：本轮唤醒即触发升级上级
    trackers = game_time._states[PROJECT_ID].setdefault("silence_trackers", {})
    trackers["sub-2"] = {
        "flagged": False, "wake_ts": 0, "notify_ts": 0, "notify_count": 0,
        "idle_acknowledged": False,
        "wake_count": game_time.DISTRESS_SAME_SIG_REPEAT,
        "superior_informed": False,
    }

    mock_trigger_sub = AsyncMock()
    mock_inbox = AsyncMock()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               mock_trigger_sub), \
         patch("hiveweave.agents.trigger.trigger_coordinator",
               AsyncMock()), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               mock_inbox):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    # inbox 上级（含正文特征）且唤醒上级
    assert mock_inbox.await_count == 1
    args = mock_inbox.await_args
    assert args.kwargs["to_agent_id"] == "mgmt-2"
    assert "SILENCE WATCHDOG" in args.kwargs["message"]
    assert args.kwargs["priority"] == "urgent"
    # 单次触发：superior_informed 置位后不再重复 inbox
    assert _trackers()["sub-2"]["superior_informed"] is True
    assert _trackers()["sub-2"]["wake_count"] >= game_time.DISTRESS_SAME_SIG_REPEAT


# ── P0-1: complete 豁免前的项目完整性闸门 ─────────────────────


async def _insert_task(env, *, status, creator_id=CEO_ID, assignee_id=None):
    """P0-1 场景用：最小任务行（raw INSERT，无工作日志副作用）."""
    from hiveweave.services import task as task_mod

    task_mod._migrated.discard(PROJECT_ID)
    await task_mod._ensure_schema(PROJECT_ID)
    tid = str(uuid.uuid4())
    # 老时间戳，避免任务行本身被当成近期产出
    old = _now_ms() - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, status, progress, "
        "creator_id, assignee_id, created_at, updated_at, is_archived) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 0)",
        [tid, PROJECT_ID, "P0-1 probe task", status, creator_id, assignee_id,
         old, old])
    await conn.commit()
    return tid


async def test_pending_work_submitted_and_verifying(env):
    """project_has_unresolved_work：submitted / verifying 任务各自独立判为 pending."""
    svc = GameTimeService()
    # 空项目（无任务、无执行层 agent）→ 无 pending work
    assert await svc.project_has_unresolved_work(PROJECT_ID) is False

    tid = await _insert_task(env, status="submitted", assignee_id=EXECUTOR_ID)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is True

    # submitted 消失、仅剩 verifying → 仍 pending
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "UPDATE tasks SET status = 'verifying' WHERE id = ?", [tid])
    await conn.commit()
    assert await svc.project_has_unresolved_work(PROJECT_ID) is True


async def test_pending_work_idle_leaf_with_cooldown(env):
    """待命叶子：active 执行层零任务且创建超 10 min → pending；冷却期内不算."""
    svc = GameTimeService()
    now = _now_ms()
    # 冷却期内（5 min）的零任务叶子 → 不算 pending（给 CEO 派活时间）
    await _insert_agent(env, EXECUTOR_ID, "柚子", role="executor",
                        created_at=now - 5 * 60 * 1000)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is False

    # 叶子创建超过 10 min 仍零任务 → pending
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE agents SET created_at = ? WHERE id = ?",
                       [now - 20 * 60 * 1000, EXECUTOR_ID])
    await conn.commit()
    assert await svc.project_has_unresolved_work(PROJECT_ID) is True

    # 叶子名下有过任务（含 closed，NOT EXISTS 不按状态过滤）→ 非待命
    await _insert_task(env, status="closed", assignee_id=EXECUTOR_ID)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is False


async def test_pending_work_coordinator_not_counted_as_leaf(env):
    """N2: active 中层（中文 role + permission_type='coordinator'）零任务
    超宽限期 → 不算待命叶子（修复前 role 粗筛会误计）."""
    svc = GameTimeService()
    now = _now_ms()
    await _insert_agent(env, "coord-1", "协调", role="技术协调员",
                        permission_type="coordinator",
                        created_at=now - 20 * 60 * 1000)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is False


async def test_pending_work_hr_chinese_role_not_counted_as_leaf(env):
    """N2: active 中文「人力资源」role agent 零任务超宽限期 →
    不判 pending（修复前 SQL 的 role NOT IN ('ceo','hr') 排除不掉中文职称）."""
    svc = GameTimeService()
    now = _now_ms()
    await _insert_agent(env, "hr-1", "人事", role="人力资源",
                        created_at=now - 20 * 60 * 1000)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is False


async def test_pending_work_leaf_beyond_ten_coordinators(env):
    """R2 回归：候选查询 LIMIT 10 时前 10 行全是 coordinator/HR → 待命叶子
    漏判。12 个 coordinator + 1 个 executor（均零任务、超宽限期）→
    pending True（修复前 LIMIT 10 漏掉第 13 行叶子，返回 False）."""
    svc = GameTimeService()
    now = _now_ms()
    old = now - 20 * 60 * 1000
    for i in range(12):
        await _insert_agent(env, f"coord-r2-{i}", f"协调{i}",
                            role="技术协调员", permission_type="coordinator",
                            created_at=old)
    await _insert_agent(env, "leaf-r2", "叶子", role="executor", created_at=old)
    assert await svc.project_has_unresolved_work(PROJECT_ID) is True


async def test_complete_ceo_not_exempt_when_submitted_pending(env, monkeypatch):
    """P0-1 核心回归：CEO disposition=complete 但项目有 submitted 任务
    （CEO 是 creator → 有审查义务）→ 不豁免，沉默超阈值举红框 + 唤醒."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: SimpleNamespace(disposition="complete",
                                    project_id=PROJECT_ID,
                                    config={"role": "ceo"}))
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "知远", role="ceo", created_at=old)
    await _insert_task(env, status="submitted", creator_id=CEO_ID,
                       assignee_id=EXECUTOR_ID)
    _seed_state()

    mock_trigger_coord = AsyncMock()
    mock_trigger_sub = AsyncMock()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator",
               mock_trigger_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               mock_trigger_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    # 红框 1 次，指向 CEO（修复前：complete 无条件豁免，无任何动作）
    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    assert errors[0]["agentId"] == CEO_ID
    # 唤醒落到 coordinator / subordinate 其中一条 trigger 路径
    triggered = ([c.args[0] for c in mock_trigger_coord.await_args_list]
                 + [c.args[0] for c in mock_trigger_sub.await_args_list])
    assert CEO_ID in triggered


async def test_complete_ceo_exempt_when_no_pending_work(env, monkeypatch):
    """complete + 项目无 pending work → 维持合法 idle 豁免（不举红框不唤醒）."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: SimpleNamespace(disposition="complete",
                                    project_id=PROJECT_ID,
                                    config={"role": "ceo"}))
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "知远", role="ceo", created_at=old)
    _seed_state()

    mock_trigger_coord = AsyncMock()
    mock_trigger_sub = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator",
               mock_trigger_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate",
               mock_trigger_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               mock_inbox):
        await GameTimeService()._check_silent_agents(PROJECT_ID)
    assert mock_trigger_coord.await_count == 0
    assert mock_trigger_sub.await_count == 0
    assert _health_events(mock_bus) == []


# ── wait 豁免穿透（resubmit 唤醒丢失事故回归）─────────────────


async def _insert_inbox_ask(env, from_id, to_id, *, read=1,
                            reply_contract_id=None):
    """raw INSERT 一条 expect_report ask（先触发 _ensure_schema 补列）.

    read 默认 1：ghost ask 场景 —— 已读但契约未解除。
    """
    from hiveweave.services.inbox import InboxService

    await InboxService().get_pending_messages(to_id)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, wake, reply_contract_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ask', 1, 1, ?)",
        [str(uuid.uuid4()), from_id, to_id, "请回复此 ask", read,
         _now_ms() - 30 * 60 * 1000,
         reply_contract_id or str(uuid.uuid4())])
    await conn.commit()


async def _insert_inbox_reply(env, from_id, to_id, reply_to):
    """插一条 reply 行闭合契约（reply_to 指向原 ask 的 contract id）."""
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, wake, reply_to) "
        "VALUES (?, ?, ?, ?, 1, ?, 'normal', 0, 0, ?)",
        [str(uuid.uuid4()), from_id, to_id, "回复", _now_ms(), reply_to])
    await conn.commit()


def _trigger_mocks():
    return (AsyncMock(), AsyncMock())


def _triggered_ids(mock_coord, mock_sub, *, force: bool | None = None):
    """收集被唤醒的 agent id；force 非 None 时只统计 force 匹配的调用."""
    calls = list(mock_coord.await_args_list) + list(mock_sub.await_args_list)
    ids = []
    for c in calls:
        if force is not None and c.kwargs.get("force", False) != force:
            continue
        ids.append(c.args[0])
    return ids


def _waiting_ceo_agent(aid):
    """生产口径的 waiting agent mock：有效 disposition + config.role，
    确保 CEO 走 trigger_coordinator（未读守卫）路径而非 subordinate."""
    return SimpleNamespace(
        disposition="waiting_agent",
        config={"role": "ceo"},
        project_id=PROJECT_ID,
    )


async def test_wait_exemption_penetrated_by_creator_submitted_task(
        env, monkeypatch):
    """事故核心回归：CEO waiting + live wait contract + 名下(creator)有
    submitted 任务且 inbox 全读 → wait 豁免被义务穿透：沉默超阈值
    红框 + 唤醒，且唤醒带 force=True 穿透 coordinator 未读守卫
    （修复前：豁免短路，CEO 只能等 TTL）。"""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        _waiting_ceo_agent)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "归零", role="ceo", created_at=old)
    await _insert_wait(env, CEO_ID, expires_at=_now_ms() + 3600_000)
    await _insert_task(env, status="submitted", creator_id=CEO_ID,
                       assignee_id=EXECUTOR_ID)
    _seed_state()

    mock_coord, mock_sub = _trigger_mocks()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    assert errors[0]["agentId"] == CEO_ID
    # CEO（coordinator 路径）必须带 force=True 被穿透唤醒——
    # force=False 会被未读守卫拦截，穿透修复形同虚设
    assert CEO_ID in _triggered_ids(mock_coord, mock_sub, force=True)
    assert any(c.args[0] == CEO_ID for c in mock_coord.await_args_list)


async def test_wait_exemption_penetrated_by_ghost_ask(env, monkeypatch):
    """ghost ask 穿透：waiting + live wait + 收到 expect_report ask 已读
    未回复（契约未解除）→ 不豁免；补 reply 闭合契约后恢复豁免。"""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        _waiting_ceo_agent)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "归零", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID,
                        created_at=old)
    await _insert_wait(env, CEO_ID, expires_at=_now_ms() + 3600_000)
    contract_id = str(uuid.uuid4())
    await _insert_inbox_ask(env, EXECUTOR_ID, CEO_ID, read=1,
                            reply_contract_id=contract_id)
    _seed_state()

    svc = GameTimeService()
    mock_coord, mock_sub = _trigger_mocks()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               AsyncMock()):
        # 第 1 轮：ghost ask（已读未回）→ 穿透 → 红框 + force 唤醒
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert CEO_ID in _triggered_ids(mock_coord, mock_sub, force=True)

        # 补 reply 闭合契约 → 第 2 轮：恢复 wait 豁免（无债不醒）
        mock_bus.reset_mock()
        mock_coord.reset_mock()
        mock_sub.reset_mock()
        await _insert_inbox_reply(env, CEO_ID, EXECUTOR_ID, contract_id)
        await svc._check_silent_agents(PROJECT_ID)

    assert _health_events(mock_bus, "error") == []
    assert _triggered_ids(mock_coord, mock_sub) == []


async def test_ghost_ask_counts_as_duty_even_when_read(env, monkeypatch):
    """无 wait 场景的口径回归：inbox 全读但 ask 契约未解除 → 不算合法
    idle，沉默超阈值照样红框唤醒（read ≠ replied）。"""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, CEO_ID, "归零", role="ceo", created_at=old)
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID,
                        created_at=old)
    await _insert_inbox_ask(env, EXECUTOR_ID, CEO_ID, read=1)
    _seed_state()

    mock_coord, mock_sub = _trigger_mocks()
    mock_bus = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_coordinator", mock_coord), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_sub), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message",
               AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    assert errors[0]["agentId"] == CEO_ID
    # 沉默唤醒统一带 force=True（无 wait 场景同一调用点）
    assert CEO_ID in _triggered_ids(mock_coord, mock_sub, force=True)
