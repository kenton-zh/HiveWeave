"""Turn 硬预算「疏通层」回归测试（2026-08-08）。

三道预算闸口（堵截兜底）之外，疏通层保证 agent 在撞闸【之前】就能
自我 pacing，且撞闸后能读懂原因、知道如何继续：

1. 预算过半（剩余 < BUDGET_PACING_HINT_S）时注入一次 pacing 提示 ——
   此前 agent 对 turn 预算完全盲视，直到软截止（~95%）才有唯一提示。
2. 预算耗尽收口（_budget_exhausted_result）无论有无累积文本都附
   收口说明 —— 下轮 wake 的 agent 面对的是可读懂的历史，而非突兀的
   半截文本。
3. 被 turn 预算帽收紧的工具超时，文案明确区分「命令本身慢」与
   「预算耗尽」，避免下轮 wake 重试同款长调用再次撞帽。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import hiveweave.llm.streamer.tool_loop as tool_loop_module
from hiveweave.llm.streamer.core import Streamer
from hiveweave.llm.streamer.tool_exec import ToolExecMixin


@pytest.fixture(autouse=True)
def _enable_session_wall_clock(monkeypatch):
    """These tests exercise the opt-in TOTAL/HARD gates."""
    monkeypatch.setenv("HIVEWEAVE_STREAM_SESSION_WALL_CLOCK", "1")


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
    # 上下文裁剪 / 中轮提醒与本测试无关，置为恒等避免引入依赖
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


# ── 1. pacing 提示：预算过半时注入，且只注入一次 ─────────────────


async def test_pacing_hint_injected_when_budget_half_used(monkeypatch):
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured_messages: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured_messages.append([dict(m) for m in messages])
        if len(captured_messages) == 1:
            clock.advance(280)  # 第一轮流式耗到剩余 ~290s（< 300 阈值）
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
    assert result["content"] == "slice done"
    # 第二轮请求的消息里应恰好有一条 pacing 提示
    pacing = [
        m
        for m in captured_messages[1]
        if m.get("role") == "system" and "[TURN BUDGET]" in (m.get("content") or "")
    ]
    assert len(pacing) == 1
    assert "commit_turn" in pacing[0]["content"]
    assert "heavy operations" in pacing[0]["content"]


async def test_pacing_hint_not_injected_on_short_turn(monkeypatch):
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    captured_messages: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        captured_messages.append([dict(m) for m in messages])
        if len(captured_messages) == 1:
            clock.advance(10)  # 剩余 ~560s，远离 300 阈值
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
    for call_messages in captured_messages:
        assert not any(
            m.get("role") == "system"
            and "[TURN BUDGET]" in (m.get("content") or "")
            for m in call_messages
        )


# ── 2. 预算耗尽收口：始终附说明，累积文本不丢 ────────────────────


async def test_tool_gate_exhaustion_keeps_text_and_explains(monkeypatch):
    """工具批闸门触发：累积文本保留 + 收口说明附上 + 本批工具丢弃。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    async def fake_stream(*, messages, **kwargs):
        clock.advance(560)  # 流式回来时已贴硬截止（剩 ~10s < 工具批地板）
        return {
            "status": "ok",
            "text": "partial analysis",
            "thinking": "",
            "tool_calls": [
                {"id": "tc-1", "name": "write_file", "arguments": "{}"}
            ],
            "finish_reason": "tool_calls",
            "usage": None,
        }

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    execute_mock = AsyncMock()
    streamer._execute_tools = execute_mock  # type: ignore[method-assign]

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
    assert result.get("budget_exhausted") is True
    # 累积文本保留 + 收口说明附上（疏通：下轮 wake 能读懂）
    assert "partial analysis" in result["content"]
    assert "[TURN BUDGET] Hard turn budget exhausted" in result["content"]
    assert "smaller slices" in result["content"]
    # 本批工具被丢弃（未执行、未落账）
    execute_mock.assert_not_called()
    assert result["tool_calls"] == []


def test_budget_exhausted_result_explains_even_without_text():
    streamer = _make_streamer()
    result = streamer._budget_exhausted_result(
        text_acc="",
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=[],
        round_num=1,
        last_usage=None,
        usage_rounds=[],
    )
    assert result["budget_exhausted"] is True
    assert result["content"].startswith("[TURN BUDGET] Hard turn budget exhausted")
    assert "commit_turn" in result["content"]


# ── 3. 预算帽收紧的工具超时：文案区分「预算耗尽」 ────────────────


async def test_budget_capped_timeout_message_explains_budget(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_STREAM_SESSION_WALL_CLOCK", "1")
    te = ToolExecMixin()

    async def slow_tool(name, arguments, tool_call_id):
        await asyncio.sleep(30)
        return {"content": "never"}

    result = await te._execute_single_tool(
        "a1",
        {"id": "tc-1", "name": "webfetch", "arguments": "{}"},
        slow_tool,
        budget_s=3.0,
    )
    content = result["content"]
    assert "[Tool Timeout]" in content
    assert "remaining turn budget" in content
    assert "do NOT retry the same long call" in content


async def test_natural_timeout_message_has_no_budget_note(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_STREAM_SESSION_WALL_CLOCK", "1")
    te = ToolExecMixin()

    async def slow_tool(name, arguments, tool_call_id):
        await asyncio.sleep(30)
        return {"content": "never"}

    import hiveweave.tools.timeout_policy as tp

    original = dict(tp.DECLARED_TIMEOUT_S)
    tp.DECLARED_TIMEOUT_S["webfetch"] = 0.5
    try:
        result = await te._execute_single_tool(
            "a1",
            {"id": "tc-1", "name": "webfetch", "arguments": "{}"},
            slow_tool,
            budget_s=500.0,
        )
    finally:
        tp.DECLARED_TIMEOUT_S.clear()
        tp.DECLARED_TIMEOUT_S.update(original)
    content = result["content"]
    assert "[Tool Timeout]" in content
    assert "remaining turn budget" not in content


async def test_budget_capped_timeout_message_question(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_STREAM_SESSION_WALL_CLOCK", "1")
    te = ToolExecMixin()

    async def slow_tool(name, arguments, tool_call_id):
        await asyncio.sleep(30)
        return {"content": "never"}

    q = await te._execute_single_tool(
        "a1",
        {"id": "tc-q", "name": "question", "arguments": "{}"},
        slow_tool,
        budget_s=3.0,
    )
    assert "remaining turn budget" in q["content"]
    assert "do NOT retry the same long call" in q["content"]


async def test_undeclared_bash_not_wrapped_by_streamer_timeout():
    te = ToolExecMixin()

    async def ok_tool(name, arguments, tool_call_id):
        return {"content": "ran"}

    result = await te._execute_single_tool(
        "a1",
        {"id": "tc-1", "name": "bash", "arguments": "pytest"},
        ok_tool,
        budget_s=3.0,
    )
    assert result["content"] == "ran"


# ── 4. 轮闸门：剩余预算不足不开新轮（审计补覆盖） ─────────────────


async def test_round_gate_blocks_new_round_when_budget_low(monkeypatch):
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    stream_calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        clock.advance(100)  # 流式耗 100s（剩 ~470s，工具闸门放行）
        return _tool_round_result()

    async def fake_execute(**kwargs):
        clock.advance(360)  # 工具批耗到剩余 ~110s（< MIN_ROUND_BUDGET 120）
        return (
            [{"role": "tool", "content": "ok", "tool_call_id": "tc-1"}],
            set(),
            set(),
            set(),
            False,
        )

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

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

    # 轮闸门在第 2 轮顶部触发：不再发起新 LLM 请求，优雅收口
    assert stream_calls == 1
    assert result["status"] == "ok"
    assert result.get("budget_exhausted") is True
    assert "[TURN BUDGET] Hard turn budget exhausted" in result["content"]
    # 第 1 轮的 tool 产出保留在历史里
    assert len(result["tool_calls"]) == 1


# ── 5. budget_cut 流式中途切断的收口路径（审计补覆盖） ────────────


async def test_budget_cut_stream_result_graceful_close(monkeypatch):
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)
    streamer = _make_streamer()
    provider = _FakeProvider()

    async def fake_stream(*, messages, **kwargs):
        clock.advance(555)  # 流式被预算切断：已累积文本保留、无 tool_calls
        return {
            "status": "ok",
            "text": "partial stream text",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": "budget_cut",
            "usage": None,
            "budget_cut": True,
        }

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

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
    assert result.get("budget_exhausted") is True
    assert "partial stream text" in result["content"]
    assert "[TURN BUDGET]" in result["content"]


# ── 6. 空响应预算跳过必须打 budget_cut 标记（审计应改 #2） ────────


async def test_empty_retry_budget_skip_marks_budget_cut():
    """裸 empty 会让 agent 层同 turn 整轮重试（新预算但 SAFETY 窗口不重置
    → 必然被强杀）；预算跳过重试必须标记 budget_cut 走优雅收口。"""
    streamer = _make_streamer()

    async def empty_round(**kwargs):
        return {
            "status": "ok",
            "text": "",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": None,
        }

    streamer._stream_single_round = empty_round  # type: ignore[method-assign]

    result = await streamer._stream_with_empty_retry(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=0,
        # 剩余 1s << 首次退避 5s → 触发预算跳过分支
        budget_deadline=time.monotonic() + 1.0,
    )

    assert result.get("budget_cut") is True


# ── 7. 总结调用的预算闸口（审计应改 #1） ──────────────────────────


async def test_summary_skipped_when_budget_low():
    """stall/no_text/max_rounds 的总结调用在预算不足时直接走 fallback —
    非流式总结 read 95s 无帽可冲过 HARD 直至 SAFETY 强杀。"""
    streamer = _make_streamer()
    provider = _FakeProvider()
    provider.build_client = MagicMock(side_effect=AssertionError(  # type: ignore[attr-defined]
        "预算不足时不允许发起总结 HTTP 调用"
    ))

    summary = await streamer._make_max_rounds_summary(
        "a1",
        provider,
        [{"role": "user", "content": "hi"}],
        None,
        reason="stall_break",
        budget_deadline=time.monotonic() + 10.0,  # < SUMMARY_MIN_BUDGET_S(45)
    )

    # fallback 文案如实反映 stall_break 原因
    assert "stalled" in summary
