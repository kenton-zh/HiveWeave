"""H3: 平台护栏拒绝（blocked）与模型空转分流回归测试。

问题：tool_exec._execute_tools 把所有 success=False（含护栏拒绝
"Command blocked"、沙箱违规、权限拒绝）归入 error_ids →
round_made_progress 判无进展 → TOOL_LOOP_STALL_LIMIT=2 连续 2 轮即
[STALL BREAK] 强制收口。护栏拒绝是平台拒环境，不是模型空转，被误杀
（slack-clone_03 实测 A044 被误杀 1 次）。

修复：ToolResult.blocked() 标记 + blocked_ids 分流 —— 全 blocked 轮走
独立 blocked_stall_count（上限 BLOCKED_STALL_LIMIT=3），不累计普通
stall_count；混有非 blocked 错误仍按原 stall 逻辑；blocked-only 连
续 3 轮仍收口兜底。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.llm.streamer.core import Streamer
from hiveweave.tools.result import ToolResult


def test_tool_result_extra_cannot_override_blocked():
    d = ToolResult.blocked_err("no", blocked=False).to_dict()
    assert d["blocked"] is True
    d2 = ToolResult.err("x", blocked=True).to_dict()
    assert d2["blocked"] is False


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


def _tool_round(tools: list[dict]) -> dict:
    return {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": tools,
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


def _blocked_bash_round(round_num: int) -> dict:
    return _tool_round([
        {"id": f"b{round_num}", "name": "bash",
         "arguments": f'{{"command": "echo {round_num}"}}'}
    ])


def _mixed_round(round_num: int) -> dict:
    return _tool_round([
        {"id": f"b{round_num}", "name": "bash",
         "arguments": f'{{"command": "echo {round_num}"}}'},
        {"id": f"e{round_num}", "name": "write_file",
         "arguments": f'{{"path": "f{round_num}.txt"}}'},
    ])


def _error_round(round_num: int) -> dict:
    return _tool_round([
        {"id": f"e{round_num}", "name": "write_file",
         "arguments": f'{{"path": "f{round_num}.txt"}}'},
    ])


# ── ① 连续 2 轮全 blocked → 不触发 STALL BREAK（修复前会误杀）───────


async def test_two_blocked_rounds_do_not_stall_break():
    streamer = _make_streamer()
    stream_calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls <= 2:
            return _blocked_bash_round(stream_calls)
        return _text_round("all calls blocked, reporting status")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._make_max_rounds_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="summary"
    )

    async def fake_execute(*, tool_calls, **kwargs):
        results = []
        ids = set()
        for tc in tool_calls:
            results.append({
                "role": "tool",
                "content": "Error: Command blocked: self-destructive pattern",
                "tool_call_id": tc["id"],
            })
            ids.add(tc["id"])
        return results, ids, ids, set(), False

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

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

    # 第 3 轮仍被允许继续（未被 STALL BREAK 截断）→ 模型拿到正常收口机会
    assert stream_calls == 3
    assert result["status"] == "ok"
    assert result.get("stall_break") is not True
    assert "all calls blocked, reporting status" in result["content"]


# ── ② 混合 blocked + 真实错误 → 仍按原 stall 逻辑（2 轮即收口）──────


async def test_mixed_blocked_and_real_error_still_stalls():
    streamer = _make_streamer()
    stream_calls = 0
    summary_messages: list[dict] = []

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return _mixed_round(stream_calls)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_summary(*args, **kwargs):
        summary_messages.extend(args[2])
        return "stall summary"

    streamer._make_max_rounds_summary = fake_summary  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = []
        error_ids = set()
        blocked_ids = set()
        for tc in tool_calls:
            if tc["name"] == "bash":
                content = "Error: Command blocked: self-destructive pattern"
                blocked_ids.add(tc["id"])
            else:
                content = "Error: boom"
            results.append({
                "role": "tool",
                "content": content,
                "tool_call_id": tc["id"],
            })
            error_ids.add(tc["id"])
        return results, error_ids, blocked_ids, set(), False

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

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

    # 混有真实错误 → 原 stall 逻辑：2 轮即 STALL BREAK
    assert stream_calls == 2
    assert result["stall_break"] is True
    sys_msgs = [
        m for m in summary_messages
        if m.get("role") == "system" and "[STALL BREAK]" in (m.get("content") or "")
    ]
    assert len(sys_msgs) == 1
    assert "no progress" in sys_msgs[0]["content"]
    assert "platform guards" not in sys_msgs[0]["content"]


# ── ③ blocked-only 连续 3 轮 → 仍会收口（兜底在）────────────────────


async def test_three_blocked_rounds_still_break():
    streamer = _make_streamer()
    stream_calls = 0
    summary_messages: list[dict] = []

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return _blocked_bash_round(stream_calls)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_summary(*args, **kwargs):
        summary_messages.extend(args[2])
        return "blocked summary"

    streamer._make_max_rounds_summary = fake_summary  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = []
        ids = set()
        for tc in tool_calls:
            results.append({
                "role": "tool",
                "content": "Error: Command blocked: self-destructive pattern",
                "tool_call_id": tc["id"],
            })
            ids.add(tc["id"])
        return results, ids, ids, set(), False

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

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

    # 3 轮全被护栏拒绝 → blocked_stall_count=3 触发独立兜底收口
    assert stream_calls == 3
    assert result["stall_break"] is True
    assert result["content"] == "blocked summary"
    sys_msgs = [
        m for m in summary_messages
        if m.get("role") == "system" and "[STALL BREAK]" in (m.get("content") or "")
    ]
    assert len(sys_msgs) == 1
    assert "platform guards" in sys_msgs[0]["content"]
    assert "3 consecutive" in sys_msgs[0]["content"]


# ── ④ ToolResult.blocked 标记序列化/传播 ────────────────────────────


def test_tool_result_blocked_serialization():
    r = ToolResult.blocked_err("Error: Command blocked: x")
    assert r.success is False
    assert r.blocked is True
    assert r.to_dict()["success"] is False
    assert r.to_dict()["blocked"] is True
    assert r.to_dict()["error"] == "Error: Command blocked: x"

    assert ToolResult.ok("fine").to_dict()["blocked"] is False
    assert ToolResult.err("boom").to_dict()["blocked"] is False
    assert ToolResult(success=False, error="x").blocked is False


async def test_execute_tools_collects_blocked_ids_separately():
    streamer = _make_streamer()
    calls = [
        {"id": "b1", "name": "bash", "arguments": "{}"},
        {"id": "e1", "name": "write_file", "arguments": "{}"},
        {"id": "d1", "name": "commit_turn", "arguments": "{}"},
    ]

    async def on_tool_call(name, args, tid):
        if tid == "b1":
            return {
                "success": False, "output": "",
                "error": "Error: Command blocked: x", "blocked": True,
            }
        if tid == "e1":
            return {"success": False, "output": "", "error": "boom"}
        return {"success": True, "output": "ok", "error": None}

    tool_results, error_ids, blocked_ids, duplicate_ids, end_turn = (
        await streamer._execute_tools(
            agent_id="a1",
            tool_calls=calls,
            on_tool_call=on_tool_call,
            on_delta=None,
        )
    )

    assert error_ids == {"b1", "e1"}
    assert blocked_ids == {"b1"}
    assert duplicate_ids == set()
    assert end_turn is False
    assert len(tool_results) == 3
    assert [m["tool_call_id"] for m in tool_results] == ["b1", "e1", "d1"]


# ── ⑤ [error, blocked, error] 跨轮交替 → 仍须 STALL BREAK（防清零逃逸）
# blocked 分支若清零 stall_count，[e,b,e,b,…] 两计数永远 0↔1 → 永不触顶。
# 必须真交替（整轮 error-only / blocked-only），不能用同轮 mixed。


async def test_alternating_blocked_and_error_still_breaks():
    streamer = _make_streamer()
    stream_calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls % 2 == 1:
            return _error_round(stream_calls)
        return _blocked_bash_round(stream_calls)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    streamer._make_max_rounds_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="summary"
    )

    async def fake_execute(*, tool_calls, **kwargs):
        results = []
        error_ids = set()
        blocked_ids = set()
        for tc in tool_calls:
            if tc["name"] == "bash":
                content = "Error: Command blocked: self-destructive pattern"
                blocked_ids.add(tc["id"])
            else:
                content = "Error: boom"
            results.append({
                "role": "tool",
                "content": content,
                "tool_call_id": tc["id"],
            })
            error_ids.add(tc["id"])
        return results, error_ids, blocked_ids, set(), False

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

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

    # e(stall=1) → b(stall 保持 1) → e(stall=2) 触顶 TOOL_LOOP_STALL_LIMIT
    # 若 blocked 轮清零 stall_count，会打满 max_tool_rounds
    assert stream_calls == 3
    assert result["stall_break"] is True


# ── ⑥ P0 集成：blocked 标记经 agents/streaming.on_tool_call 透传 ──
# 2026-08-13 审计 P0：streaming.py 的 dict 重建曾丢弃 blocked 字段 →
# tool_exec 的 blocked_ids 恒空 → 生产路径 blocked 分流是死代码。
# 本测试走真实 on_tool_call（agents/streaming.py），验证标记到达
# _execute_tools 的 blocked_ids。


def test_streaming_bridge_preserves_blocked_flag():
    from hiveweave.agents import streaming as streaming_mod
    from unittest.mock import patch

    class _FakeAgent:
        project_id = "proj-1"
        id = "agent-1"
        _tool_executor = None
        _run_ledger = None
        _current_run_id = None
        _run_step_counter = 0

        def _get_workspace_path(self):
            import asyncio

            return asyncio.sleep(0) or "ws"

        def _stop_heartbeat(self):
            pass

        async def _tool_executor_execute(self, **kwargs):
            return {
                "success": False,
                "output": "",
                "error": "Error: Command blocked: self-destructive",
                "blocked": True,
            }

    agent = _FakeAgent()

    class _Exec:
        async def execute(self, **kwargs):
            return {
                "success": False,
                "output": "",
                "error": "Error: Command blocked: self-destructive",
                "blocked": True,
            }

    with (
        patch.object(agent, "_tool_executor", _Exec()),
        patch.object(agent, "_run_ledger", None),
        patch.object(agent, "_current_run_id", None),
        patch.object(agent, "_run_step_counter", 0),
        patch(
            "hiveweave.agents.streaming.meta_db.get_project_workspace",
            new=AsyncMock(return_value="ws"),
        ),
        patch(
            "hiveweave.agents.streaming.broadcast_stream_event",
            # 生产代码是同步调用（streaming.py on_tool_call），AsyncMock 会产生
            # "coroutine never awaited" 告警 —— 用 MagicMock 保真。
            new=MagicMock(),
        ),
    ):
        import asyncio

        result = asyncio.run(
            streaming_mod.on_tool_call(agent, "bash", "{}", "t1")
        )

    assert result["blocked"] is True
    assert result["success"] is False


@pytest.mark.asyncio
async def test_subagent_callback_preserves_blocked_flag():
    from hiveweave.tools.subagent import _subagent_on_tool_call

    class _Exec:
        async def execute(self, *a, **k):
            return {
                "success": False,
                "output": "",
                "error": "Permission denied",
                "blocked": True,
            }

    class _Parent:
        id = "p1"

    cb = _subagent_on_tool_call(_Parent(), _Exec(), "/ws", None)
    out = await cb("bash", "{}", "c1")
    assert out["blocked"] is True
    assert out["tool_call_id"] == "c1"
