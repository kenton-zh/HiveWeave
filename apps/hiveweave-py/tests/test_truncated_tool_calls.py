"""截断 tool_calls 防御回归测试（TEST_DSH_16 实证）。

根因：opencode zen go 网关（Console Go）偶发提前断 SSE（丢
response.completed，finish=None）→ tool_call arguments 只收到半截
JSON → 畸形调用写进 assistant(tool_calls) 历史回传 → 网关 400
"`arguments` must be valid JSON" 杀死整个 turn（CEO 首轮即 [ERROR]）。

修复：tool_loop 在消费 round tool_calls 前校验 arguments JSON 合法性；
畸形丢弃 + 注入 [STREAM TRUNCATED] 提示让模型重发；连续
TRUNCATED_TOOL_CALL_ROUNDS_LIMIT 轮畸形优雅收口。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from hiveweave.llm.streamer.core import Streamer
from hiveweave.llm.streamer.tool_loop import _is_valid_json_arguments


def test_is_valid_json_arguments():
    assert _is_valid_json_arguments(None) is True
    assert _is_valid_json_arguments("") is True  # 无参调用
    assert _is_valid_json_arguments('{"command": "ls"}') is True
    assert _is_valid_json_arguments({"command": "ls"}) is True  # 已结构化
    # 半截 JSON（流截断）
    assert _is_valid_json_arguments('{"command": "write_file("') is False
    assert _is_valid_json_arguments('{"path": "src/a') is False
    assert _is_valid_json_arguments("{") is False


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


def _truncated_round(text: str = "") -> dict:
    """复现 round 1：finish=None + 半截 arguments。"""
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [
            {"id": "t1", "name": "hire_agent",
             "arguments": '{"role": "frontend", "backstory": "负责实现可互动的 Python 学习网'},  # noqa: E501
        ],
        "finish_reason": None,
        "usage": None,
    }


def _good_round() -> dict:
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


def _text_round(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
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


# ── ① 全畸形轮：丢弃 + 提示注入 + 继续循环让模型重发 ────────────────


async def test_all_truncated_discarded_and_loop_continues():
    streamer = _make_streamer()
    result, calls = await _run(
        streamer,
        [
            _truncated_round("我正在招聘前端工程师"),
            _good_round(),
            _text_round("done"),
        ],
    )
    # 畸形 t1 未执行；合法 g1 执行；循环跑到文本轮收口
    assert "t1" not in calls["executed"]
    assert "g1" in calls["executed"]
    assert calls["stream"] == 3
    assert result["status"] == "ok"
    assert "done" in result["content"]
    assert result["rounds"] >= 2
    # [STREAM TRUNCATED] 提示确实进入下一轮请求的 messages
    round2_msgs = calls["messages_seen"][1]
    hint_msgs = [
        m for m in round2_msgs
        if m.get("role") == "system"
        and "[STREAM TRUNCATED]" in (m.get("content") or "")
    ]
    assert hint_msgs, "truncation hint missing from next-round request"
    assert "hire_agent" in hint_msgs[0]["content"]


# ── ② 部分畸形：畸形丢弃、合法照常执行 ────────────────────────────


async def test_partial_truncated_only_bad_dropped():
    streamer = _make_streamer()
    mixed = {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": [
            {"id": "bad", "name": "send_message", "arguments": '{"to": "A14'},
            {"id": "good", "name": "bash",
             "arguments": '{"command": "echo hi"}'},
        ],
        "finish_reason": "tool_calls",
        "usage": None,
    }
    result, calls = await _run(streamer, [mixed, _text_round("fin")])
    assert "bad" not in calls["executed"]
    assert "good" in calls["executed"]
    assert result["status"] == "ok"
    # tool_history 只含合法调用
    hist_names = [t["function"]["name"] for t in result["tool_calls"]]
    assert "send_message" not in hist_names
    assert "bash" in hist_names


# ── ③ 连续 3 轮全畸形 → 优雅收口（不再无限重发）──────────────────


async def test_consecutive_truncated_rounds_close_turn():
    streamer = _make_streamer()
    result, calls = await _run(
        streamer,
        [
            _truncated_round("first"),
            _truncated_round("second"),
            _truncated_round("third"),
        ],
    )
    assert calls["stream"] == 3
    assert result["status"] == "ok"
    assert "截断" in result["content"]
    assert calls["executed"] == []
    # FIX(text-acc)：收口只用末轮文本 —— first/second 已作为中间轮
    # per-round 消息入历史，不得在最终 content 里重复拼接。
    assert "third" in result["content"]
    assert result["content"].count("first") == 0
    assert result["content"].count("second") == 0


# ── ④ 畸形轮后恢复正常 → 计数重置（不累积误杀）──────────────────


async def test_truncated_counter_resets_after_clean_round():
    streamer = _make_streamer()
    result, calls = await _run(
        streamer,
        [
            _truncated_round("t1"),
            _truncated_round("t2"),
            _good_round(),   # 清洁轮：计数重置
            _truncated_round("t3"),
            _good_round(),
            _text_round("done"),
        ],
    )
    assert result["status"] == "ok"
    assert "done" in result["content"]
    assert calls["stream"] == 6


# ── ⑤ 回传网关的历史里不再有畸形 arguments ───────────────────────


async def test_no_malformed_arguments_in_history():
    """工具轮历史（tool_turn_messages）中 assistant(tool_calls) 的
    arguments 必须全部是合法 JSON —— 回传网关不再触发 400。"""
    streamer = _make_streamer()
    mixed = {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": [
            {"id": "bad", "name": "write_file", "arguments": '{"path": "src'},
            {"id": "good", "name": "bash",
             "arguments": '{"command": "echo hi"}'},
        ],
        "finish_reason": "tool_calls",
        "usage": None,
    }
    result, _ = await _run(streamer, [mixed, _text_round("fin")])
    for m in result["tool_turn_messages"]:
        for tc in m.get("tool_calls") or []:
            args = tc["function"]["arguments"]
            assert _is_valid_json_arguments(args), (
                f"malformed arguments leaked into history: {args!r}"
            )
            json.loads(args)  # 硬校验
