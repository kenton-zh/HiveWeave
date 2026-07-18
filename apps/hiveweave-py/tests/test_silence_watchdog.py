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
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        wait_contract_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ── Helpers ─────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_agent(env, agent_id, name, parent_id=None,
                        created_at=None, status="active"):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [agent_id, PROJECT_ID, name, "executor", parent_id, status, ts, ts])
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
    """沉默 40 分钟（> 10 min 阈值且 > 30 min 升级线）：
    一次唤醒 trigger + agent_health error 红框 + 上级 inbox 通知 + 上级 trigger."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)

    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID, created_at=old)
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

    # ① 唤醒尝试（executor）+ ③ 上级通知后的 trigger（ceo）
    triggered_ids = [c.args[0] for c in mock_trigger.await_args_list]
    assert EXECUTOR_ID in triggered_ids
    assert CEO_ID in triggered_ids

    # ② 红框：同构 agent.py _broadcast_agent_health 事件结构
    errors = _health_events(mock_bus, "error")
    assert len(errors) == 1
    event = errors[0]
    assert event["agentId"] == EXECUTOR_ID
    assert event["projectId"] == PROJECT_ID
    assert "SILENCE WATCHDOG" in event["message"]
    assert isinstance(event["at"], int)

    # ③ 上级通知一次
    assert mock_inbox.await_count == 1
    call = mock_inbox.await_args
    assert call.args[1] == CEO_ID
    assert "[SILENCE WATCHDOG]" in call.args[2]
    assert "潮汐" in call.args[2]

    # tracker 落账：flagged + wake_ts/notify_ts 已记录
    tracker = _trackers()[EXECUTOR_ID]
    assert tracker["flagged"] is True
    assert tracker["wake_ts"] > 0
    assert tracker["notify_ts"] > 0


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
    """同一 agent：wake 冷却（15 min）与 notify 冷却（30 min）内不重复动作；
    手动把 tracker 时间戳拨回过期后可再次触发."""
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing", lambda: [])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: None)
    old = _now_ms() - 40 * 60 * 1000
    await _insert_agent(env, EXECUTOR_ID, "潮汐", parent_id=CEO_ID, created_at=old)
    _seed_state()

    svc = GameTimeService()
    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with _started_mock(1), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", mock_inbox):
        # 第 1 轮：wake + error + notify 各一次
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert mock_inbox.await_count == 1

        # 第 2 轮（冷却内）：全部不重复
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 1
        assert mock_inbox.await_count == 1
        # trigger 总数仍是 2（executor wake + ceo notify），无新增
        assert mock_trigger.await_count == 2

        # 拨回 tracker 时间戳模拟冷却过期 → 第 3 轮再次 wake + notify
        tracker = _trackers()[EXECUTOR_ID]
        tracker["wake_ts"] -= game_time.STALL_COOLDOWN_MS
        tracker["notify_ts"] -= game_time.SILENCE_NOTIFY_COOLDOWN_MS
        await svc._check_silent_agents(PROJECT_ID)
        assert len(_health_events(mock_bus, "error")) == 2
        assert mock_inbox.await_count == 2
