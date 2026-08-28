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
    # DSH_33 归因：失败 id 有非 blocked 成分（write_file "Error: boom"）→
    # 工具层失败，不得报成模型空转，也不得报成护栏拒绝。
    assert result["stall_reason"] == "tool_failed"
    assert "工具调用失败（非模型空转）" in sys_msgs[0]["content"]
    assert "no progress" not in sys_msgs[0]["content"]
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
    assert result["stall_reason"] == "blocked"
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


# ── ⑦ DSH_33 归因：工具执行失败 ≠ 模型空转 ─────────────────────────
# 实测 52 次 stall 中 47 次（90.4%）末尾两轮 status=failed，护栏判定正确
# 但文案报成「模型无进展」→ 模型反省自己空转，而真正该改的是工具用法。


def test_classify_stall_round_orders_tool_failure_before_denial():
    """归因顺序：工具失败 > 护栏拒绝 > 只读 > 空转（DSH runner-outranks-denial）。"""
    from hiveweave.llm.streamer.doom_loop import classify_stall_round

    calls = [{"id": "1", "name": "write_file"}]
    # 纯工具失败
    assert classify_stall_round(calls, error_ids={"1"}) == "tool_failed"
    # 纯护栏拒绝
    assert (
        classify_stall_round(calls, error_ids={"1"}, blocked_ids={"1"}) == "blocked"
    )
    # 混合：有非 blocked 失败成分 → 工具失败优先，不得报 blocked
    mixed = [{"id": "1", "name": "write_file"}, {"id": "2", "name": "bash"}]
    assert (
        classify_stall_round(mixed, error_ids={"1", "2"}, blocked_ids={"2"})
        == "tool_failed"
    )
    # 有进展 → None
    assert classify_stall_round(calls) is None
    # 只读轮询
    assert classify_stall_round([{"id": "1", "name": "get_tasks"}]) == "readonly"
    # 空轮（无 tool_calls）走只读口径，与既有 round_was_readonly_only 一致
    assert classify_stall_round([]) == "readonly"


async def test_tool_failure_stall_text_blames_tools_not_model():
    """全工具失败轮收口：文案必须写「工具调用失败（非模型空转）」。"""
    streamer = _make_streamer()
    stream_calls = 0
    summary_messages: list[dict] = []

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return _error_round(stream_calls)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_summary(*args, **kwargs):
        summary_messages.extend(args[2])
        return "tool failure summary"

    streamer._make_max_rounds_summary = fake_summary  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = []
        ids = set()
        for tc in tool_calls:
            results.append({
                "role": "tool",
                "content": "[Tool Error] Unknown tool 'self.bash'.",
                "tool_call_id": tc["id"],
            })
            ids.add(tc["id"])
        # blocked_ids 为空 —— 工具层失败，不是护栏拒绝
        return results, ids, set(), set(), False

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

    # 收口时机与修复前一致（2 轮），只有归因/文案变了
    assert stream_calls == 2
    assert result["stall_break"] is True
    assert result["stall_reason"] == "tool_failed"
    sys_msgs = [
        m for m in summary_messages
        if m.get("role") == "system" and "[STALL BREAK]" in (m.get("content") or "")
    ]
    assert len(sys_msgs) == 1
    text = sys_msgs[0]["content"]
    assert "工具调用失败（非模型空转）" in text
    # 禁止空转类措辞
    assert "no progress" not in text
    assert "空转" not in text.replace("非模型空转", "")
    assert "platform guards" not in text


async def test_mixed_noprogress_then_tool_failure_reports_both_counts():
    """[无进展, 工具失败] 混合序列：归因归工具层，但轮数须如实。

    该序列由普通 stall_count=2 触发闸口，而 tool_fail_stall_count 只有 1 ——
    文案若只写「连续 1 轮」会让模型误判收口时机，必须补报总轮数。
    """
    streamer = _make_streamer()
    stream_calls = 0
    summary_messages: list[dict] = []

    async def fake_stream(*, messages, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return _error_round(stream_calls)

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_summary(*args, **kwargs):
        summary_messages.extend(args[2])
        return "mixed summary"

    streamer._make_max_rounds_summary = fake_summary  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = [
            {"role": "tool", "content": "x", "tool_call_id": t["id"]}
            for t in tool_calls
        ]
        ids = {t["id"] for t in tool_calls}
        if stream_calls == 1:
            # 第 1 轮：duplicate（无进展，但不是工具失败）
            return results, set(), set(), ids, False
        # 第 2 轮：真实工具失败
        return results, ids, set(), set(), False

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

    assert result["stall_break"] is True
    # 末轮是工具失败 → 归因工具层，不得报模型空转
    assert result["stall_reason"] == "tool_failed"
    text = next(
        m["content"] for m in summary_messages
        if m.get("role") == "system" and "[STALL BREAK]" in (m.get("content") or "")
    )
    assert "连续 1 轮工具调用失败" in text
    # 触发闸口的是 stall_count=2 → 必须补报，不能让模型以为 1 轮就收口
    assert "最近 2 轮均无进展" in text


def test_summary_fallback_reflects_tool_failure_attribution():
    """预算不足跳过总结时的 fallback 也不得统一说 "stalled"。"""
    from hiveweave.llm.streamer.context import ContextMixin

    mixin = ContextMixin()
    tool_fail = mixin._summary_fallback("stall_break", "tool_failed")
    assert "tool-call failures" in tool_fail
    assert "not model idling" in tool_fail

    runner = mixin._summary_fallback("stall_break", "runner_failed")
    assert "runner failure" in runner

    # 无归因 / 真空转仍保留既有文案（既有测试钉住 "stalled"）
    assert "stalled" in mixin._summary_fallback("stall_break")
    assert "stalled" in mixin._summary_fallback("stall_break", "no_progress")
