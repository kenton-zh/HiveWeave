"""批25 修复1（A 桶分段预算）：墙前收口提示 + auto 绝对墙软着陆。

病灶（第四轮 A 桶实测）：33+10 条 run 全部死在 core.py 外层
``wait_for(HARD+30)`` ≈ 1740s 硬墙 —— auto 模式下 tool_loop 不武装
hard_deadline，模型对绝对墙完全盲视，run 被整杀 error → 下个 run
从头再来（跨 run 重读税，烧时 957+290min）。

修法（两段式疏导，钦定方向 = 分段/疏导，不是提墙）：
a) 墙前收口提示：轮边界 elapsed ≥ 85% 墙（``WALL_APPROACH_PCT``）⇒
   注入一次 ``[WALL APPROACHING]`` —— **auto 与 legacy 都注入**
   （安全疏导非回合预算；legacy 的 E14/[TURN BUDGET] 全挂在
   _turn_budget_armed 下）。提示只进请求副本不落库（无 _acc，
   同 [TURN BUDGET] 纪律）。
b) 到顶软着陆：auto 轮边界到 100% 墙优先 budget_exhausted 优雅收口
   （completion._t16_budget_stop → interrupted + 延续唤醒自动续跑），
   赶在外层整杀前返回；legacy 的 100% 路径逐字节不动（既有
   test_turn_budget_pacing 回归覆盖）。

约束：不改 HARD_TOTAL_TIMEOUT_S 值；不在流中途打断 LLM 请求（只在轮边界）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import hiveweave.llm.streamer.tool_loop as tool_loop_module
from hiveweave.config import settings
from hiveweave.llm.streamer.core import Streamer

WALL_TOKEN = "[WALL APPROACHING]"


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")
    return "legacy"


@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setattr(settings, "turn_exit_mode", "auto")
    return "auto"


class _FakeClock:
    """工具可控的 monotonic 时钟（只替换 tool_loop 模块命名空间里的 time）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeProvider:
    provider_type = "fake"
    model_name = "fake-model"
    fallback = None
    max_output_tokens = 4096
    supports_thinking = False
    context_window = 128_000


def _make_streamer() -> Streamer:
    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=MagicMock(allowed=True, fallback=None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=MagicMock(),
    )
    streamer._trim_context_if_needed = lambda messages, provider: messages  # type: ignore[method-assign]

    async def _ident_pressure(messages, provider, **kwargs):
        return messages

    streamer._pressure_compact_if_needed = _ident_pressure  # type: ignore[method-assign]
    streamer._maybe_inject_mid_round_reminder = (  # type: ignore[method-assign]
        lambda messages, round_num, cap: messages
    )
    return streamer


def _tool_round_result(name: str = "write_file") -> dict:
    return {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": [{"id": "tc-1", "name": name, "arguments": "{}"}],
        "finish_reason": "tool_calls",
        "usage": None,
    }


def _text_round_result(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": None,
    }


def _patch_clock(monkeypatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(
        tool_loop_module, "time", SimpleNamespace(monotonic=clock.monotonic)
    )


def _wall_messages(captured: list[list[dict]]) -> list[dict]:
    return [
        m
        for call in captured
        for m in call
        if m.get("role") == "system" and WALL_TOKEN in (m.get("content") or "")
    ]


# ── a) 墙前收口提示 ─────────────────────────────────────────────────


async def test_wall_hint_injected_past_85pct_legacy(monkeypatch, legacy_mode):
    """① legacy：elapsed ≥85% 墙 ⇒ 下一轮请求注入 [WALL APPROACHING]。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            # 阈值由常量推导（env 可调，不写死）。
            clock.advance(
                tool_loop_module.HARD_TOTAL_TIMEOUT_S
                * tool_loop_module.WALL_APPROACH_PCT
                + 30
            )
            return _tool_round_result()
        return _text_round_result("slice done")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert result["status"] == "ok"
    hits = _wall_messages(captured)
    assert len(hits) == 1, (
        f"85% 墙后应恰注入一次 [WALL APPROACHING]，got {len(hits)}"
    )
    assert "自动续跑" in hits[0]["content"]
    assert "剩余" in hits[0]["content"]


async def test_wall_hint_injected_in_auto_mode(monkeypatch, auto_mode):
    """① auto：同样注入 —— 此前 auto 对绝对墙完全盲视（E14/预算提示
    全部退役），这是 A 桶 1740s 整杀的主修补。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            clock.advance(
                tool_loop_module.HARD_TOTAL_TIMEOUT_S
                * tool_loop_module.WALL_APPROACH_PCT
                + 30
            )
            return _tool_round_result()
        return _text_round_result("auto slice done")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert result["status"] == "ok"
    assert len(_wall_messages(captured)) == 1


async def test_wall_hint_not_injected_below_threshold(monkeypatch, legacy_mode):
    """② 未到阈值零注入。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            clock.advance(10)  # 远离 85% 阈值
            return _tool_round_result()
        return _text_round_result("quick done")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert result["status"] == "ok"
    assert _wall_messages(captured) == []


async def test_wall_hint_injected_once_only(monkeypatch, legacy_mode):
    """提示一次性：跨 85% 后的多轮不重复注入（同 pacing 纪律）。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            # 首轮就跨过 85%（后续轮都在阈值之上，验证不重复注入）
            clock.advance(
                tool_loop_module.HARD_TOTAL_TIMEOUT_S
                * tool_loop_module.WALL_APPROACH_PCT
                + 10
            )
            return _tool_round_result()
        if len(captured) <= 4:
            clock.advance(30)
            return _tool_round_result()
        return _text_round_result("done")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert result["status"] == "ok"
    assert len(captured) >= 3
    # messages 原地 append ⇒ 后续轮的快照里重复看到同一条提示；
    # 「一次性」的判据 = 任何一轮的请求里都不出现第 2 条 WALL 提示，
    # 且至少一轮（首次注入后）恰有一条。
    per_round = [len([m for m in call if _is_wall(m)]) for call in captured]
    assert max(per_round) == 1, per_round
    assert 1 in per_round, per_round


def _is_wall(m: dict) -> bool:
    return m.get("role") == "system" and WALL_TOKEN in (m.get("content") or "")


async def test_wall_hint_not_persisted_to_tool_turn_messages(
    monkeypatch, legacy_mode
):
    """提示只进请求副本不落库：result.tool_turn_messages 无 WALL 文案
    （同 [TURN BUDGET] 不落库纪律）。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured.append([dict(m) for m in messages])
        if len(captured) == 1:
            clock.advance(
                tool_loop_module.HARD_TOTAL_TIMEOUT_S
                * tool_loop_module.WALL_APPROACH_PCT
                + 30
            )
            return _tool_round_result()
        return _text_round_result("slice done")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert result["status"] == "ok"
    assert len(_wall_messages(captured)) == 1
    for m in result["tool_turn_messages"]:
        assert WALL_TOKEN not in (m.get("content") or "")
    assert WALL_TOKEN not in (result.get("content") or "")


# ── b) auto 绝对墙软着陆（100%）────────────────────────────────────


async def test_auto_wall_soft_landing_at_100pct(monkeypatch, auto_mode):
    """③ auto：轮边界到 100% 墙 ⇒ budget_exhausted 优雅收口（interrupted +
    自动续跑语义），而非外层 wait_for 整杀 error。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    calls = 0

    async def fake_stream(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(tool_loop_module.HARD_TOTAL_TIMEOUT_S + 20)
            return _tool_round_result()
        raise AssertionError("100% 墙软着陆后不得再发起新 LLM 请求")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert calls == 1
    assert result["status"] == "ok", (
        "auto 100% 墙必须软着陆（ok + budget_exhausted），不是 error"
    )
    assert result.get("budget_exhausted") is True
    assert result.get("steering_reason") == "hard_budget"
    # tool 产出保留（软着陆 vs 整杀的核心差异）
    assert len(result["tool_calls"]) == 1
    # auto 收口文案：interrupted + 自动续跑指引（_budget_exhausted_result auto 分支）
    assert "interrupted" in result["content"]
    assert "continue" in result["content"]


async def test_legacy_100pct_path_unchanged(monkeypatch, legacy_mode):
    """③ 兼容：legacy 的 100% 路径行为不变（tool_history ⇒ 优雅收口，
    auto 软着陆块不拦截 legacy —— hard_deadline 非 None 短路）。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    calls = 0

    async def fake_stream(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(tool_loop_module.HARD_TOTAL_TIMEOUT_S + 20)
            return _tool_round_result()
        raise AssertionError("legacy 100% 墙后不得再发起新 LLM 请求")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )
    )

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=provider,
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )

    assert calls == 1
    assert result["status"] == "ok"
    assert result.get("budget_exhausted") is True
    assert result.get("steering_reason") == "hard_budget"
    # legacy 收口文案走 commit_turn 指引分支（非 auto 的 interrupted 分支）
    assert "commit_turn" in result["content"]
