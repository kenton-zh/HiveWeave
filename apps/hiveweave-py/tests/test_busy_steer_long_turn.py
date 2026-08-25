"""长 turn（近似无限）+ 可唤醒消息插队（busy steer）验收。

配套改动：
1. streamer/constants: HARD 断言天花板从写死 600 → env 可调
   HIVEWEAVE_STREAM_AGENT_CEILING_S（30min turn 由此解锁）；
2. agents/trigger: busy 路径把可唤醒 inbox 经 agent.steer 注入
   运行中 turn 的 next-step 窗口（限频 60s，enqueue_wake 兜底保留）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hiveweave.agents import trigger as trig


@pytest.fixture(autouse=True)
def _clean_steer_state():
    """steer 限频表/DB 桩隔离，避免跨用例污染。"""
    trig._steer_inbox_last.clear()


@pytest.fixture
def _fake_inbox(monkeypatch):
    def _install(pending: list[dict] | None = None, background: list[dict] | None = None):
        svc = AsyncMock()
        svc.get_pending_messages = AsyncMock(return_value=list(pending or []))
        svc.get_undelivered_background = AsyncMock(return_value=list(background or []))
        monkeypatch.setattr(trig, "_inbox_service", svc)
        return svc

    return _install


# ── 常数：30min turn 配置可被接受 ──────────────────────────────


def test_constants_accept_30min_turn_env(monkeypatch):
    import importlib

    import hiveweave.llm.streamer.constants as c

    monkeypatch.setenv("HIVEWEAVE_STREAM_HARD_TIMEOUT_S", "1710")
    monkeypatch.setenv("HIVEWEAVE_STREAM_AGENT_CEILING_S", "1800")
    monkeypatch.setenv("HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S", "1500")
    importlib.reload(c)
    try:
        assert c.HARD_TOTAL_TIMEOUT_S == 1710.0
        assert c.AGENT_SAFETY_CEILING_S == 1800.0
        assert c.HARD_TOTAL_TIMEOUT_S < c.AGENT_SAFETY_CEILING_S
    finally:
        monkeypatch.delenv("HIVEWEAVE_STREAM_HARD_TIMEOUT_S", raising=False)
        monkeypatch.delenv("HIVEWEAVE_STREAM_AGENT_CEILING_S", raising=False)
        monkeypatch.delenv("HIVEWEAVE_STREAM_TOTAL_TIMEOUT_S", raising=False)
        importlib.reload(c)


def test_constants_rejects_hard_at_or_above_ceiling(monkeypatch):
    """结构断言：HARD >= 天花板必须 import 失败（防静默击穿）。"""
    import importlib

    import hiveweave.llm.streamer.constants as c

    monkeypatch.setenv("HIVEWEAVE_STREAM_HARD_TIMEOUT_S", "1800")
    monkeypatch.setenv("HIVEWEAVE_STREAM_AGENT_CEILING_S", "1800")
    with pytest.raises(AssertionError):
        importlib.reload(c)
    monkeypatch.delenv("HIVEWEAVE_STREAM_HARD_TIMEOUT_S", raising=False)
    monkeypatch.delenv("HIVEWEAVE_STREAM_AGENT_CEILING_S", raising=False)
    importlib.reload(c)


# ── busy 插话辅助 ─────────────────────────────────────────────


class _FakeAgent:
    def __init__(self, status: str = "processing", queue: bool = True):
        self.status = type("S", (), {"value": status})()
        self._steer_q = asyncio.Queue() if queue else None
        self.steer = AsyncMock(return_value={"steer": True})


def _wake_msg(content: str, from_id: str = "boss") -> dict:
    return {"wake": 1, "from_agent_id": from_id, "message_type": "normal",
            "message": content}


@pytest.mark.asyncio
async def test_steer_busy_inbox_injects_when_processing(_fake_inbox):
    _fake_inbox(
        pending=[_wake_msg("把 E2 复测一下"), {"wake": 1, "message_type": "task_event",
                                            "message": "task.x approved"}],
        background=[],
    )
    agent = _FakeAgent()
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is True
    agent.steer.assert_awaited_once()
    text = agent.steer.await_args.args[0]
    assert "[INBOX]" in text and "E2" in text
    assert "task.x approved" not in text


@pytest.mark.asyncio
async def test_steer_busy_inbox_rate_limited(_fake_inbox):
    _fake_inbox(pending=[_wake_msg("再来一条")])
    agent = _FakeAgent()
    assert await trig._try_steer_busy_inbox(agent, "a1") is True
    # 60s 窗口内第二次 → 限频跳过（守卫先于 DB 查询 → 零查询）
    assert await trig._try_steer_busy_inbox(agent, "a1") is False
    assert agent.steer.await_count == 1
    # 限频窗口内不再触发 DB 查询
    assert trig._inbox_service.get_pending_messages.await_count == 1


@pytest.mark.asyncio
async def test_steer_busy_inbox_skips_when_not_processing(_fake_inbox):
    _fake_inbox(pending=[_wake_msg("x")])
    agent = _FakeAgent(status="idle")
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is False  # 不 processing → 不插话（enqueue_wake 兜底）
    trig._inbox_service.get_pending_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_busy_inbox_fyi_only_noop(_fake_inbox):
    _fake_inbox(pending=[{"wake": 1, "message_type": "task_event", "message": "x"}])
    agent = _FakeAgent()
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is False
    agent.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_busy_inbox_no_queue_noop(_fake_inbox):
    _fake_inbox(pending=[_wake_msg("x")])
    agent = _FakeAgent(queue=False)  # 无活跃 turn 队列 → 跳过
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is False
    agent.steer.assert_not_awaited()
    trig._inbox_service.get_pending_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_busy_inbox_fallback_to_queue_returns_false(_fake_inbox):
    """steer 未命中（退化 chat/排队）→ 视为未插话，不记限频。"""
    _fake_inbox(pending=[_wake_msg("x")])
    agent = _FakeAgent()
    agent.steer = AsyncMock(return_value={"ok": True, "queued": True})
    ok = await trig._try_steer_busy_inbox(agent, "a1")
    assert ok is False
    assert "a1" not in trig._steer_inbox_last