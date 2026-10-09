"""N3 批28-1 施工1：finish=length 分类恢复（对齐 pi compact-and-retry-once）。

上游参照（pi，最新 HEAD 已核验）：
- ``packages/ai/src/utils/overflow.ts:179`` ``isRecoverableLength`` ——
  length 停止可做一次有界恢复；
- ``packages/coding-agent/src/core/agent-session.ts:2936-2955`` —— 恢复
  **恰好一次**（``_overflowRecoveryAttempted`` 旗标，第二次直接报错不
  循环）；:1120 正常响应即复位旗标。

本仓最小形态（与 pi 的差异见施工报告）：
- 分类 ``output_length_truncated``（非 error/PermanentError，稳定码随行）；
- 第一次 length：截断本轮 assistant 输出（保尾 LENGTH_RECOVERY_KEEP_CHARS）
  + 注入 ``[OUTPUT TRUNCATED]`` 系统提示重问一轮（同 run）；
- 第二次 length：interrupted/budget 型终态（budget_exhausted 收口），
  不再重试；
- 正常轮（finish != length）复位恢复旗标。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hiveweave.llm.streamer.constants import (
    LENGTH_RECOVERY_KEEP_CHARS,
    OUTPUT_LENGTH_TRUNCATED_CODE,
)
from hiveweave.llm.streamer.core import Streamer
from hiveweave.llm.streamer.tool_loop import _tail_keep


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


def _length_round(text: str, thinking: str = "") -> dict:
    """烧满输出上限的纯文本轮：finish_reason=length。"""
    return {
        "status": "ok",
        "text": text,
        "thinking": thinking,
        "tool_calls": [],
        "finish_reason": "length",
        "usage": None,
    }


def _text_round(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": None,
    }


def _good_tool_round() -> dict:
    return {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": [
            {"id": "g1", "name": "bash", "arguments": '{"command": "echo ok"}'},
        ],
        "finish_reason": "tool_calls",
        "usage": None,
    }


async def _run(streamer: Streamer, rounds, execute=None):
    """rounds: 每轮 _stream_with_empty_retry 的返回值序列。"""
    calls = {"stream": 0, "executed": [], "messages_seen": []}
    seq = list(rounds)

    async def fake_stream(**kwargs):
        idx = min(calls["stream"], len(seq) - 1)
        calls["stream"] += 1
        calls["messages_seen"].append(kwargs.get("messages") or [])
        return seq[idx]

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    if execute is None:
        async def execute(*, tool_calls, **kwargs):  # type: ignore[misc]
            calls["executed"].extend(tc["id"] for tc in tool_calls)
            results = [
                {"role": "tool", "content": "ok", "tool_call_id": tc["id"]}
                for tc in tool_calls
            ]
            return results, set(), set(), set(), False

    streamer._execute_tools = execute  # type: ignore[method-assign]
    streamer._make_max_rounds_summary = AsyncMock(return_value="summary")

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )
    return result, calls


# ── 单元：_tail_keep 机械尾部截断 ──────────────────────────────


def test_tail_keep_short_text_untouched():
    assert _tail_keep("short", 100) == "short"
    assert _tail_keep("", 100) == ""


def test_tail_keep_long_text_keeps_tail_with_marker():
    text = "A" * 5000 + "B" * 5000
    kept = _tail_keep(text, LENGTH_RECOVERY_KEEP_CHARS)
    # 短文本不动；超长保尾 + 截断标记（标记行不计入 cap）
    assert kept.startswith("[... truncated")
    assert kept.endswith("B" * 5000)
    assert len(kept) <= LENGTH_RECOVERY_KEEP_CHARS + 100


# ── ① 第一次 length：截断 + 注入 [OUTPUT TRUNCATED] 重问一轮 ─────


async def test_length_first_round_truncates_and_reasks_once():
    streamer = _make_streamer()
    long_text = "开头叙述。" + "烧满预算的长输出。" * 3000  # 远超 keep 帽
    result, calls = await _run(
        streamer,
        [_length_round(long_text), _text_round("结论：已完成 A 与 B。")],
    )
    # 恰好一次恢复重问：第二次请求发生，run 以重问轮的精简结论收口
    assert calls["stream"] == 2
    assert result["status"] == "ok"
    assert result["content"] == "结论：已完成 A 与 B。"
    # 重问请求里：截断后的 assistant 消息（保尾，不超帽）+ 系统提示
    round2_msgs = calls["messages_seen"][1]
    truncated_assistant = [
        m for m in round2_msgs
        if m.get("role") == "assistant"
    ]
    assert truncated_assistant, "truncated assistant msg missing"
    kept_content = truncated_assistant[-1]["content"]
    assert kept_content.startswith("[... truncated")
    assert len(kept_content) <= LENGTH_RECOVERY_KEEP_CHARS + 100
    hint_msgs = [
        m for m in round2_msgs
        if m.get("role") == "system" and "[OUTPUT TRUNCATED]" in m.get("content", "")
    ]
    assert hint_msgs, "[OUTPUT TRUNCATED] hint missing from re-ask request"
    assert "精简结论" in hint_msgs[0]["content"]
    # 持久化历史（tool_turn_messages）里也有截断消息 —— 历史不撒谎
    persisted = [
        m for m in result["tool_turn_messages"]
        if m.get("role") == "assistant" and str(m.get("content", "")).startswith(
            "[... truncated"
        )
    ]
    assert persisted


# ── ② 连续两次 length：第二次直接终态，不再重试 ──────────────────


async def test_length_twice_closes_as_terminal_no_third_round():
    streamer = _make_streamer()
    result, calls = await _run(
        streamer,
        [_length_round("第一次撞上限的输出"), _length_round("第二次撞上限的输出")],
    )
    # 恰好两次请求（第一次恢复 + 第二次终态），没有第三次
    assert calls["stream"] == 2
    # interrupted/budget 型终态：status ok + budget_exhausted + 稳定分类位
    assert result["status"] == "ok"
    assert result["budget_exhausted"] is True
    assert result["steering_reason"] == OUTPUT_LENGTH_TRUNCATED_CODE
    assert result["output_length_truncated"] is True
    assert result["error_code"] == OUTPUT_LENGTH_TRUNCATED_CODE
    # 兼容既有截断告警措辞（不静默换文案）
    assert "⚠️ 回复被截断" in result["content"]
    assert "第二次撞上限的输出" in result["content"]


# ── ③ 正常轮复位旗标：length→正常→length 允许再恢复一次 ──────────


async def test_length_recovery_flag_resets_after_normal_round():
    streamer = _make_streamer()
    result, calls = await _run(
        streamer,
        [
            _length_round("第一次撞上限"),   # 恢复 #1
            _good_tool_round(),            # 正常轮（执行工具继续）⇒ 复位
            _length_round("第二次撞上限"),   # 恢复 #2（复位后允许）
            _length_round("第三次撞上限"),   # 终态
        ],
    )
    assert calls["stream"] == 4
    assert calls["executed"] == ["g1"]
    # 两次恢复重问的请求里各有一条 [OUTPUT TRUNCATED] 提示
    for idx in (1, 3):
        hints = [
            m for m in calls["messages_seen"][idx]
            if m.get("role") == "system"
            and "[OUTPUT TRUNCATED]" in m.get("content", "")
        ]
        assert hints, f"recovery hint missing from round-{idx} request"
    # 最终终态分类正确
    assert result["output_length_truncated"] is True
    assert result["error_code"] == OUTPUT_LENGTH_TRUNCATED_CODE


# ── ④ length + tool_calls 形态：分类位随行（恢复暂缓，行为不变）────


async def test_length_with_tool_calls_carries_classification():
    streamer = _make_streamer()
    mixed = {
        "status": "ok",
        "text": "分析到一半",
        "thinking": "",
        "tool_calls": [
            {"id": "t1", "name": "bash", "arguments": '{"command": "ls"}'},
        ],
        "finish_reason": "length",
        "usage": None,
    }
    result, calls = await _run(streamer, [mixed])
    # 行为不变：一次请求收口（截断 tool_calls 丢弃，不做恢复重问）
    assert calls["stream"] == 1
    assert result["status"] == "ok"
    # 分类位随行（可机检）
    assert result["output_length_truncated"] is True
    assert result["error_code"] == OUTPUT_LENGTH_TRUNCATED_CODE
