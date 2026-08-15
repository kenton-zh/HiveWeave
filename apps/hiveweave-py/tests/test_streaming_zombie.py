"""P0-3 streaming 僵尸自愈 —— 「卡住中的流」检测与强制中断.

被测文件:
  - src/hiveweave/services/game_time.py
      (_streaming_stuck_ms / _sweep_orphan_streaming / _check_silent_agents)
  - src/hiveweave/agents/recovery.py (force_interrupt_stuck_stream)

背景: A037 事故 —— agent 卡在流式状态 11 分钟，safety timer 未清僵尸，
看门狗因 PROCESSING 豁免永不唤醒。修复后:
  1. 正常流式（有事件活动）不受影响
  2. 卡住中的流（超阈值无事件）被强制清理 + 中断回合
  3. 真孤儿（agent 已 idle）仍走既有 clear_orphan_streaming
  4. 看门狗不再豁免 streaming 僵尸

测试策略（对齐 test_silence_watchdog.py）:
  - tempfile 真实 per-project DB；patch meta_db.get_project_workspace 路由
  - agent_manager.list_processing / get_agent 用 monkeypatch 控制分支
  - status_event_bus.publish_stream_event 以 AsyncMock 捕获
"""

from __future__ import annotations

import asyncio
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
from hiveweave.services.game_time import GameTimeService, _streaming_stuck_ms

PROJECT_ID = "test-zombie-project"
CEO_ID = "test-ceo"
STUCK_ID = "agent-stuck"
HEALTHY_ID = "agent-healthy"


@pytest.fixture(autouse=True)
def clean_states():
    """每个测试前后清空 game_time 内存态，防止 tracker 跨用例污染."""
    game_time._states.clear()
    yield
    game_time._states.clear()


@pytest.fixture
async def env():
    """真实 per-project DB（temp workspace）+ meta_db 路由 patch."""
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


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_streaming_row(env, agent_id, *, content="", created_at=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    await conn.execute(
        "INSERT INTO chat_messages (id, agent_id, role, content, "
        "is_streaming, created_at) VALUES (?, ?, 'assistant', ?, 1, ?)",
        [str(uuid.uuid4()), agent_id, content, ts])
    await conn.commit()


async def _streaming_rows(env, agent_id):
    conn = await ensure_project_db(env["workspace_path"])
    cursor = await conn.execute(
        "SELECT is_streaming, content FROM chat_messages WHERE agent_id = ?",
        [agent_id])
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


# ── _streaming_stuck_ms 单元判定 ─────────────────────────────


def test_stuck_ms_none_when_no_activity_signal():
    """无活动时间戳（0 / 属性缺失）→ None fail-open（硬龄路径兜底）."""
    now = _now_ms()
    assert _streaming_stuck_ms(SimpleNamespace(), now) is None
    assert _streaming_stuck_ms(
        SimpleNamespace(_last_stream_activity_at=0.0), now) is None


def test_stuck_ms_none_when_activity_fresh():
    """健康流式（活动新鲜）→ None，不误伤."""
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 60_000,  # 1min 前有事件
        _active_tools={},
    )
    assert _streaming_stuck_ms(agent, now) is None


def test_stuck_ms_fires_after_threshold():
    """超阈值（默认 5min）无事件 → 返回卡住毫秒数."""
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 6 * 60_000,  # 6min 无事件
        _active_tools={},
    )
    stuck = _streaming_stuck_ms(agent, now)
    assert stuck is not None and stuck >= 6 * 60_000


def test_stuck_ms_spawn_subagent_uses_default_tool_cap():
    """spawn_subagent is off-turn; 6min quiet while 'executing' is a zombie."""
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={"tc-1": ("spawn_subagent", now - 6 * 60_000)},
    )
    assert _streaming_stuck_ms(agent, now) is not None


def test_stuck_ms_bash_within_max_timeout_not_zombie():
    """Foreground bash may run up to 600s; 6min quiet is still inside cap+60s."""
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={"tc-1": ("bash", now - 6 * 60_000)},
    )
    assert _streaming_stuck_ms(agent, now) is None


def test_stuck_ms_bash_fires_after_max_timeout():
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 12 * 60_000,
        _active_tools={"tc-1": ("bash", now - 12 * 60_000)},
    )
    assert _streaming_stuck_ms(agent, now) is not None


def test_stuck_ms_write_file_not_killed_at_six_minutes():
    now = _now_ms()
    agent = SimpleNamespace(
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={"tc-1": ("write_file", now - 6 * 60_000)},
    )
    assert _streaming_stuck_ms(agent, now) is None


# ── sweep：卡住中的流被清 + 强制中断；健康流式不动 ──────────


@pytest.mark.asyncio
async def test_sweep_clears_stuck_stream_and_interrupts(env, monkeypatch):
    """PROCESSING + 6min 无事件 + DB 有 streaming 行:
    行被清（空内容回填「[流式响应被中断]」）+ 红框广播 + 强制中断一次."""
    now = _now_ms()
    stuck_agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    healthy_agent = SimpleNamespace(
        id=HEALTHY_ID,
        _last_stream_activity_at=now - 30_000,  # 30s 前有事件
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    agents = {STUCK_ID: stuck_agent, HEALTHY_ID: healthy_agent}
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID), (HEALTHY_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: agents.get(aid))

    # 两个 agent 各有 1 条「年轻」streaming 行（< 10min 软龄，不被旧逻辑清）
    await _insert_streaming_row(env, STUCK_ID, content="", created_at=now - 6 * 60_000)
    await _insert_streaming_row(env, HEALTHY_ID, content="partial", created_at=now - 30_000)

    mock_bus = AsyncMock()
    with patch.object(status_event_bus, "publish_stream_event", mock_bus):
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    # 卡住 agent：行清 0 + 空内容回填中断文案 + 强制中断一次
    rows = await _streaming_rows(env, STUCK_ID)
    assert rows and all(r[0] == 0 for r in rows)
    assert rows[0][1] == "[流式响应被中断]"
    stuck_agent.force_interrupt_stuck_stream.assert_awaited_once()

    # 红框广播（agent_health error 结构同构 _broadcast_agent_health）
    health = [c.args[1] for c in mock_bus.await_args_list
              if c.args[1].get("type") == "agent_health"]
    assert len(health) == 1
    assert health[0]["agentId"] == STUCK_ID
    assert health[0]["health"] == "error"
    assert "STREAMING ZOMBIE" in health[0]["message"]

    # 健康 agent：行保留 streaming=1 + 内容不动 + 不中断
    rows = await _streaming_rows(env, HEALTHY_ID)
    assert rows and all(r[0] == 1 for r in rows)
    assert rows[0][1] == "partial"
    healthy_agent.force_interrupt_stuck_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_skips_stuck_agent_without_streaming_rows(env, monkeypatch):
    """判活信号失效但 DB 无 streaming 行 → fail-open 不动手（不中断）."""
    now = _now_ms()
    agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 30 * 60_000,  # 很久无事件
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    mock_bus = AsyncMock()
    with patch.object(status_event_bus, "publish_stream_event", mock_bus):
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    agent.force_interrupt_stuck_stream.assert_not_awaited()
    assert mock_bus.await_count == 0


@pytest.mark.asyncio
async def test_sweep_interrupt_failure_does_not_break_sweep(env, monkeypatch):
    """O3 新契约：强制中断抛错 → sweep 不炸 + 僵尸行保留（is_streaming=1）
    供下一轮 sweep 重试；红框广播仍发."""
    now = _now_ms()
    agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    # 僵尸行需老于 stuck 时刻（默认 5min 阈值）才会被守卫计入
    await _insert_streaming_row(env, STUCK_ID, content="partial",
                                created_at=now - 6 * 60_000)

    mock_bus = AsyncMock()
    with patch.object(status_event_bus, "publish_stream_event", mock_bus):
        # 不抛异常即通过；中断失败 → 行保留（修复前：清行在中断前，行被清）
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    rows = await _streaming_rows(env, STUCK_ID)
    assert rows and all(r[0] == 1 for r in rows)
    assert rows[0][1] == "partial"
    # 红框广播仍发（供人工发现）
    health = [c.args[1] for c in mock_bus.await_args_list
              if c.args[1].get("type") == "agent_health"]
    assert len(health) == 1
    assert health[0]["agentId"] == STUCK_ID
    assert health[0]["health"] == "error"


@pytest.mark.asyncio
async def test_sweep_retries_interrupt_after_failure(env, monkeypatch):
    """O3: force 首次抛错保留行 → 下一轮 sweep 再次尝试成功 → 行被清 + 中断两次."""
    now = _now_ms()
    agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(
            side_effect=[RuntimeError("boom"), True]),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    await _insert_streaming_row(env, STUCK_ID, content="",
                                created_at=now - 6 * 60_000)

    svc = GameTimeService()
    with patch.object(status_event_bus, "publish_stream_event", AsyncMock()):
        # 第 1 轮：中断失败 → 行保留（is_streaming 仍 1）
        await svc._sweep_orphan_streaming(PROJECT_ID)
        rows = await _streaming_rows(env, STUCK_ID)
        assert rows and all(r[0] == 1 for r in rows)

        # 第 2 轮：中断成功 → 行被清 + 空内容回填中断文案
        await svc._sweep_orphan_streaming(PROJECT_ID)

    rows = await _streaming_rows(env, STUCK_ID)
    assert rows and all(r[0] == 0 for r in rows)
    assert rows[0][1] == "[流式响应被中断]"
    assert agent.force_interrupt_stuck_stream.await_count == 2


@pytest.mark.asyncio
async def test_sweep_time_limited_clear_spares_new_turn_placeholder(env, monkeypatch):
    """O2: 旧回合僵尸行 + 新回合 placeholder（created_at 新鲜）共存时，
    只清 stuck 时刻之前就存在的旧行，新占位行不被误清."""
    now = _now_ms()
    agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    # 旧僵尸行（stuck 时刻之前）+ 新回合占位行（刚刚写入，时间戳新鲜）
    await _insert_streaming_row(env, STUCK_ID, content="old-zombie",
                                created_at=now - 6 * 60_000)
    await _insert_streaming_row(env, STUCK_ID, content="new-placeholder",
                                created_at=now)

    with patch.object(status_event_bus, "publish_stream_event", AsyncMock()):
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    rows = await _streaming_rows(env, STUCK_ID)
    by_content = {r[1]: r[0] for r in rows}
    assert by_content["old-zombie"] == 0
    assert by_content["new-placeholder"] == 1
    agent.force_interrupt_stuck_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_interrupts_stuck_agent_with_row_older_than_soft_age(env, monkeypatch):
    """C1 回归：stuck（6min 无事件）+ 行 11min 前（> 10min 软龄）→
    中断先行、孤儿清理殿后 —— 中断仍发生 + 行被清。

    修复前：孤儿清理先摘行（软龄绕过 protect）→ 中断路径 DB COUNT 守卫
    查不到行 → 提前 return，不中断（A037 实况 11 分钟）。
    """
    now = _now_ms()
    agent = SimpleNamespace(
        id=STUCK_ID,
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    # 行年龄 11min > 软龄（SAFETY_TIMEOUT_MS=10min）——修复前会被孤儿清理
    # 先摘掉，导致 stuck 中断路径 COUNT=0 提前 return。
    await _insert_streaming_row(env, STUCK_ID, content="",
                                created_at=now - 11 * 60_000)

    with patch.object(status_event_bus, "publish_stream_event", AsyncMock()):
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    # 中断发生（force 被 await 一次）+ 行被清（中断路径收尾，空内容回填文案）
    agent.force_interrupt_stuck_stream.assert_awaited_once()
    rows = await _streaming_rows(env, STUCK_ID)
    assert rows and all(r[0] == 0 for r in rows)
    assert rows[0][1] == "[流式响应被中断]"


@pytest.mark.asyncio
async def test_sweep_healthy_long_round_row_kept_no_interrupt(env, monkeypatch):
    """Healthy long round (event 30s ago, row 11min old) stays streaming.

    PROCESSING rows are not age-finalized; stuck detection uses quiet cap.
    """
    now = _now_ms()
    agent = SimpleNamespace(
        id=HEALTHY_ID,
        _last_stream_activity_at=now - 30_000,  # 30s 前有事件 → 健康
        _active_tools={},
        force_interrupt_stuck_stream=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(HEALTHY_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent", lambda aid: agent)

    await _insert_streaming_row(env, HEALTHY_ID, content="partial",
                                created_at=now - 11 * 60_000)

    with patch.object(status_event_bus, "publish_stream_event", AsyncMock()):
        await GameTimeService()._sweep_orphan_streaming(PROJECT_ID)

    rows = await _streaming_rows(env, HEALTHY_ID)
    assert rows and all(r[0] == 1 for r in rows)
    assert rows[0][1] == "partial"
    agent.force_interrupt_stuck_stream.assert_not_awaited()


# ── force_interrupt_stuck_stream（recovery）─────────────────


@pytest.mark.asyncio
async def test_force_interrupt_not_processing_noop():
    """非 PROCESSING → False，不取消任何 task."""
    from hiveweave.agents.recovery import force_interrupt_stuck_stream
    from hiveweave.agents.types import AgentState

    agent = SimpleNamespace(
        id="a1", status=AgentState.IDLE, _llm_task=None, _cancel_reason=None)
    ok = await force_interrupt_stuck_stream(agent, reason_detail="test")
    assert ok is False
    assert agent._cancel_reason is None


@pytest.mark.asyncio
async def test_force_interrupt_cancels_live_task_as_safety_timeout():
    """PROCESSING + 活 LLM task → cancel_reason=safety_timeout + cancel."""
    from hiveweave.agents.recovery import force_interrupt_stuck_stream
    from hiveweave.agents.types import AgentState

    async def _hang():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_hang())
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=task, _cancel_reason=None)
    try:
        ok = await force_interrupt_stuck_stream(
            agent, reason_detail="streaming quiet 6min")
        assert ok is True
        assert agent._cancel_reason == "safety_timeout"
        assert task.cancelled or task.cancelling()
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_force_interrupt_desync_goes_through_safety_recovery(monkeypatch):
    """PROCESSING 但无活 LLM task（状态脱钩）→ 直接走 safety_timeout 恢复."""
    from hiveweave.agents import recovery
    from hiveweave.agents.types import AgentState

    mock_recovery = AsyncMock()
    monkeypatch.setattr(recovery, "handle_safety_timeout", mock_recovery)
    agent = SimpleNamespace(
        id="a1", status=AgentState.PROCESSING,
        _llm_task=None, _cancel_reason=None)
    ok = await recovery.force_interrupt_stuck_stream(agent, reason_detail="t")
    assert ok is True
    assert agent._cancel_reason == "safety_timeout"
    mock_recovery.assert_awaited_once()


# ── 看门狗：streaming 僵尸不再被 PROCESSING 豁免 ────────────


async def _insert_agent(env, agent_id, name, parent_id=None, created_at=None):
    conn = await ensure_project_db(env["workspace_path"])
    ts = created_at if created_at is not None else _now_ms()
    await conn.execute(
        "INSERT INTO agents (id, project_id, name, role, parent_id, status, "
        "created_at, updated_at) VALUES (?, ?, ?, 'executor', ?, 'active', ?, ?)",
        [agent_id, PROJECT_ID, name, parent_id, ts, ts])
    await conn.commit()


async def _insert_open_task(env, assignee_id):
    """落账一条 running 任务（让沉默检测有义务可查；对齐 test_silence_watchdog）."""
    from hiveweave.services import task as task_mod

    task_mod._migrated.discard(PROJECT_ID)
    await task_mod._ensure_schema(PROJECT_ID)
    old = _now_ms() - 40 * 60 * 1000
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, description, status, "
        "progress, creator_id, assignee_id, created_at, updated_at, "
        "is_archived) VALUES (?, ?, ?, ?, 'running', 20, ?, ?, ?, ?, 0)",
        [str(uuid.uuid4()), PROJECT_ID, "duty", "keep on duty",
         CEO_ID, assignee_id, old, old])
    await conn.commit()


@pytest.mark.asyncio
async def test_watchdog_does_not_exempt_streaming_zombie(env, monkeypatch):
    """PROCESSING 但流式 6min 无事件 + 沉默 40min → 不豁免：唤醒 + 红框."""
    now = _now_ms()
    zombie = SimpleNamespace(
        id=STUCK_ID,
        disposition="runnable",
        _last_stream_activity_at=now - 6 * 60_000,
        _active_tools={},
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(STUCK_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: zombie)

    old = now - 40 * 60 * 1000
    await _insert_agent(env, STUCK_ID, "A037", parent_id=CEO_ID, created_at=old)
    await _insert_open_task(env, STUCK_ID)
    game_time._states[PROJECT_ID] = {"project_id": PROJECT_ID}

    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    with patch("hiveweave.db.meta.query_one",
               new=AsyncMock(return_value={"is_started": 1})), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", AsyncMock()):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    triggered = [c.args[0] for c in mock_trigger.await_args_list]
    assert STUCK_ID in triggered
    errors = [c.args[1] for c in mock_bus.await_args_list
              if c.args[1].get("type") == "agent_health"
              and c.args[1].get("health") == "error"]
    assert any(e["agentId"] == STUCK_ID for e in errors)


@pytest.mark.asyncio
async def test_watchdog_still_exempts_healthy_processing(env, monkeypatch):
    """PROCESSING 且流式活动新鲜 → 仍豁免（不误伤正常长流）."""
    now = _now_ms()
    healthy = SimpleNamespace(
        id=HEALTHY_ID,
        disposition="runnable",
        _last_stream_activity_at=now - 30_000,  # 30s 前有事件
        _active_tools={},
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.list_processing",
        lambda: [(HEALTHY_ID, PROJECT_ID)])
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda aid: healthy)

    old = now - 60 * 60 * 1000
    await _insert_agent(env, HEALTHY_ID, "健", created_at=old)
    await _insert_open_task(env, HEALTHY_ID)
    game_time._states[PROJECT_ID] = {"project_id": PROJECT_ID}

    mock_trigger = AsyncMock()
    mock_bus = AsyncMock()
    mock_inbox = AsyncMock()
    with patch("hiveweave.db.meta.query_one",
               new=AsyncMock(return_value={"is_started": 1})), \
         patch("hiveweave.agents.trigger.trigger_subordinate", mock_trigger), \
         patch.object(status_event_bus, "publish_stream_event", mock_bus), \
         patch("hiveweave.services.inbox.InboxService.send_message", mock_inbox):
        await GameTimeService()._check_silent_agents(PROJECT_ID)

    assert mock_trigger.await_count == 0
    assert mock_bus.await_count == 0
    assert mock_inbox.await_count == 0
