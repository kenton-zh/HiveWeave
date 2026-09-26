"""批 C 第1步②：出口门禁同 run 补步 —— 不再重跑整轮。

审计背景：turn_exit 触发的 run 占四项目 Σrun 的 15-18% —— MISSING_COMMIT_TURN
的「补一步」被实现成 run 关闭后 ``_retrigger_for_turn_gate`` 重开一个完整
回合（新 run）。上游依据（DSH @ 477b4f420）：

- ``docs/agent-lifecycle.md:51`` — "retry in the open step: prepare and
  reconcile the same rendered assembly without repeating pre-step or users"；
- ``docs/agent-lifecycle.md:85`` — "Recovery runs within the open step …
  without repeating assembly, pre-step, or user admission"；
- ``agent.ts:389/402/407`` — ``firstAttempt`` 只在首次尝试追加 user 消息，
  重试发生在同一 session 的未关闭 step 内。

本仓等价物：无独立 step 边界 ⇒ 「run 关闭前的 tool loop 继续」。本文件钉住：

① 违规 → 同 run 补步：hint 作为一条 user 消息进当前 run 的请求上下文与
  持久化时间线（tool_turn_messages），模型补 commit_turn（end_turn）后 run
  正常关闭；全程只有一个 run。
② 补步预算（``_TURN_GATE_MAX``=1/run）耗尽 ⇒ 判据返回 None，run 收口；
  收口仍违规由 completion 显式记录（GATE UNRESOLVED），跨 run 重触生产者
  已删除。
③ 无违规/已提交路径零变化：end_turn 短路时回调零调用（已提交 run 零开销）；
  回调返回 None 时行为与旧实现一致。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hiveweave.llm.streamer.core import Streamer
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)


class _FakeProvider:
    provider_type = "fake"
    model_name = "fake-model"
    fallback = None
    max_output_tokens = 4096
    supports_thinking = False
    context_window = 128_000


def _make_streamer() -> Streamer:
    from unittest.mock import MagicMock

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


def _text_round(text: str) -> dict:
    return {
        "status": "ok",
        "text": text,
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": None,
    }


def _tool_round(tools: list[dict]) -> dict:
    return {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": tools,
        "finish_reason": "tool_calls",
        "usage": None,
    }


def _commit_round() -> dict:
    return _tool_round([
        {
            "id": "c1",
            "name": "commit_turn",
            "arguments": '{"phase": "done_slice", "summary": "wrapped up"}',
        }
    ])


def _stub_agent(turn_gate_count: int = 0, turn_gate_max: int = 1):
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = "a1"
    agent.project_id = "p1"
    agent._turn_gate_count = turn_gate_count
    agent._TURN_GATE_MAX = turn_gate_max
    return agent


# ── ① 违规 → 同 run 补步成功（唯一 run，hint 进上下文，commit 落地）──────


@pytest.mark.asyncio
async def test_missing_commit_supplemented_in_same_run():
    streamer = _make_streamer()
    requests: list[list[dict]] = []
    hint_text = (
        "[TURN EXIT BLOCKED]\n每一轮必须像函数一样返回 TurnResult。"
        "GATE=MISSING_COMMIT_TURN REF=- MISSING=commit_turn(...)"
    )

    async def fake_stream(*, messages, **kwargs):
        requests.append([dict(m) for m in messages])
        if len(requests) == 1:
            return _text_round("work is done")
        return _commit_round()

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = [
            {
                "role": "tool",
                "content": "turn committed",
                "tool_call_id": tc["id"],
            }
            for tc in tool_calls
        ]
        return results, set(), set(), set(), True  # end_turn

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

    gate_calls: list[list] = []

    async def gate_check(tool_history):
        gate_calls.append(list(tool_history))
        return hint_text

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check,
    )

    # 同一个 run 内两轮请求 —— 没有第二个 run / 第二次 loop 调用
    assert len(requests) == 2
    assert len(gate_calls) == 1
    # 门禁在「run 关闭前」（末轮收尾文本出口）被问询
    assert gate_calls[0] == []
    # hint 作为 user 消息进入补步轮请求（同 run 同上下文，DSH
    # session.append('user/message') 的等价物）
    round2 = requests[1]
    assert round2[-1] == {"role": "user", "content": hint_text}
    assert round2[-2]["role"] == "assistant"
    assert round2[-2]["content"] == "work is done"
    # hint 持久化进整轮时间线（completion 按 tool_turn_messages 落库，
    # strip_round_annotations 只剥 "round" 侧信道键）
    user_turns = [
        m for m in result["tool_turn_messages"] if m.get("role") == "user"
    ]
    assert hint_text in [m.get("content") for m in user_turns]
    # 模型补上 commit_turn → end_turn → run 正常关闭
    assert result["end_turn"] is True
    assert result["rounds"] == 2
    # P2-2 账本：原收尾文本经注入分支 _acc 落持久化累积器，与
    # result.content 一致且恰好一块（无丢失/无重复）
    assert result["content"] == "work is done"
    asst_texts = [
        m.get("content")
        for m in result["tool_turn_messages"]
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]
    assert asst_texts.count("work is done") == 1


@pytest.mark.asyncio
async def test_supplement_round_text_final_keeps_original_text_persisted():
    """P2-2 账本缺口回归：补步轮再次文本收尾 ⇒ 原收尾文本不丢、无重复。

    注入分支 messages.append(final_msg) 必须同时 _acc(final_msg) —— 否则
    补步轮以文本收口（预算耗尽判 None）时，原收尾文本从 tool_turn_acc
    （→ conversation_turns / metadata.segments）与 result.content 双双
    丢失（只活在直播流，done 后 reload 消失），且 segments 与 content
    失配会触发展示层全量兜底复读。
    """
    streamer = _make_streamer()
    requests: list[list[dict]] = []

    async def fake_stream(*, messages, **kwargs):
        requests.append([dict(m) for m in messages])
        if len(requests) == 1:
            return _text_round("work is done")
        return _text_round("still wrapping up")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    gate_calls = 0

    async def gate_check(tool_history):
        nonlocal gate_calls
        gate_calls += 1
        # 首次违规发 hint；补步轮再收尾时预算耗尽 ⇒ None（判据层短路已
        # 单测，此处造的是「hint 注入后模型仍只产文本」的形态）
        return "[TURN EXIT BLOCKED] commit now" if gate_calls == 1 else None

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check,
    )

    assert len(requests) == 2
    asst_texts = [
        m.get("content")
        for m in result["tool_turn_messages"]
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]
    # 原收尾文本与补步轮新文本都进持久化累积器，各一块、无重复
    assert asst_texts == ["work is done", "still wrapping up"]
    # result.content = 补步轮文本（最终轮口径）；原文本在时间线里不丢
    assert result["content"] == "still wrapping up"
    # hint 仍在时间线（夹在两段 assistant 文本之间；_acc 块带 "round"
    # 展示侧信道键，按 role+content 断言）
    assert "[TURN EXIT BLOCKED] commit now" in [
        m.get("content")
        for m in result["tool_turn_messages"]
        if m.get("role") == "user"
    ]


# ── ② 补步预算耗尽：run 收口 + 显式记录 + 无新 run ─────────────────────


@pytest.mark.asyncio
async def test_supplement_verdict_none_closes_run_without_hint():
    """判据 None（预算耗尽/无违规）⇒ tool loop 按原样收口：无第二轮请求、
    无 hint 落时间线、无新 run。预算耗尽时「不评估」的短路在判据层测
    （见 test_supplement_budget_bumps_once_per_run 的 fact_calls 钉子）。"""
    streamer = _make_streamer()
    calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal calls
        calls += 1
        return _text_round("still not committing")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    gate_calls: list[list] = []

    async def gate_check(tool_history):
        gate_calls.append(list(tool_history))
        return None

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check,
    )

    assert calls == 1
    assert len(gate_calls) == 1
    assert result["status"] == "ok"
    assert result["content"] == "still not committing"
    assert "end_turn" not in result
    user_turns = [
        m for m in result["tool_turn_messages"] if m.get("role") == "user"
    ]
    assert user_turns == []


@pytest.mark.asyncio
async def test_supplement_budget_bumps_once_per_run(monkeypatch):
    """build_gate_supplement_hint：首违规发 hint 并计 1；同 run 第二次 None。"""
    from hiveweave.agents import completion as completion_mod

    clear_pending_turn_result("a1")  # 无 TurnResult ⇒ MISSING_COMMIT_TURN
    agent = _stub_agent(turn_gate_count=0, turn_gate_max=1)
    fact_calls = 0

    async def _fake_facts(a, tool_calls):
        nonlocal fact_calls
        fact_calls += 1
        return completion_mod.ExitGateFacts(
            agent_id=a.id, project_id=a.project_id, tool_calls=tool_calls
        )

    monkeypatch.setattr(completion_mod, "gather_exit_gate_facts", _fake_facts)

    hint = await completion_mod.build_gate_supplement_hint(agent, [])
    assert hint is not None
    assert "commit_turn" in hint
    assert agent._turn_gate_count == 1
    assert fact_calls == 1

    # 同 run 第二次违规：预算（_TURN_GATE_MAX=1）耗尽 ⇒ None，且不再评估
    assert await completion_mod.build_gate_supplement_hint(agent, []) is None
    assert fact_calls == 1
    assert agent._turn_gate_count == 1


@pytest.mark.asyncio
async def test_completion_records_unresolved_gate_without_new_run():
    """收口仍违规 ⇒ 显式记录（GATE UNRESOLVED）；跨 run 重触分支不存在。"""
    import inspect

    from pathlib import Path

    from hiveweave.agents import completion as completion_mod

    here = Path(__file__).resolve()
    src = (
        here.parents[1] / "src" / "hiveweave" / "agents" / "completion.py"
    ).read_text(encoding="utf-8")
    # 显式记录分支存在
    assert "TURN EXIT BLOCKED — GATE UNRESOLVED" in src
    assert "turn_exit_gate_unresolved" in src
    # 跨 run 重触调用点不存在（handle_completion 不再 scheduling 修复 run；
    # 注释里允许出现退役路径的名字作说明）
    assert "await agent._retrigger_for_turn_gate" not in src
    assert "gate_retrigger_hint" not in src
    assert "elif gate_retrigger_hint" not in src
    handle = inspect.getsource(completion_mod.handle_completion)
    assert '"source": "turn_exit_gate"' not in handle
    assert "budget_closed" in src  # 预算墙收口不再重触


# ── ③ 无违规路径零变化（控制组）───────────────────────────────────────


@pytest.mark.asyncio
async def test_committed_run_never_calls_gate_check():
    """已提交的 run（end_turn 短路）不经过补步出口 ⇒ 回调零调用、零开销。"""
    streamer = _make_streamer()
    calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal calls
        calls += 1
        return _commit_round()

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def fake_execute(*, tool_calls, **kwargs):
        results = [
            {
                "role": "tool",
                "content": "turn committed",
                "tool_call_id": tc["id"],
            }
            for tc in tool_calls
        ]
        return results, set(), set(), set(), True

    streamer._execute_tools = fake_execute  # type: ignore[method-assign]

    gate_calls: list[list] = []

    async def gate_check(tool_history):
        gate_calls.append(list(tool_history))
        return "should not be injected"

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check,
    )

    assert calls == 1
    assert gate_calls == []
    assert result["end_turn"] is True
    assert result["rounds"] == 1
    user_turns = [
        m for m in result["tool_turn_messages"] if m.get("role") == "user"
    ]
    assert user_turns == []


@pytest.mark.asyncio
async def test_clean_text_exit_with_none_verdict_is_unchanged():
    """回调 None（无违规/不可修复）⇒ 与旧行为逐字节一致的单轮收口。"""
    streamer = _make_streamer()
    calls = 0

    async def fake_stream(*, messages, **kwargs):
        nonlocal calls
        calls += 1
        return _text_round("final answer")

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    async def gate_check(tool_history):
        return None

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
        exit_gate_check=gate_check,
    )

    assert calls == 1
    assert result["status"] == "ok"
    assert result["content"] == "final answer"
    assert "end_turn" not in result
    user_turns = [
        m for m in result["tool_turn_messages"] if m.get("role") == "user"
    ]
    assert user_turns == []


@pytest.mark.asyncio
async def test_supplement_none_when_only_park_violation(monkeypatch):
    """控制组：仅 park 类违规（OPEN_TASKS_UNDECLARED）⇒ 不补步。

    P2-1 修正：义务 leftover 评估只在 done_slice/waiting 执行
    （turn_exit.py：``phase in ("done_slice", "waiting")`` 分支）——旧写法
    用 in_progress + 义务实际走 exit-ok 路径，build_gate_supplement_hint
    的 park 半边（not should_repair）零覆盖。现用 done_slice + 一条不落
    任何 repair 码的 leftover 义务（reviewer/verifying 不进 elif 链、
    也不在 done_slice 豁免组合里）制造真 should_park=True。
    """
    from hiveweave.agents import completion as completion_mod

    clear_pending_turn_result("a1")
    set_pending_turn_result(
        "a1",
        {
            "schema_version": 1,
            "phase": "done_slice",
            "summary": "wrapped up",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    try:
        agent = _stub_agent()

        async def _fake_facts(a, tool_calls):
            return completion_mod.ExitGateFacts(
                agent_id=a.id,
                project_id=a.project_id,
                tool_calls=tool_calls,
                open_obligations=[
                    {
                        "id": "t1",
                        "role_hint": "reviewer",
                        "status": "verifying",
                    },
                ],
            )

        monkeypatch.setattr(
            completion_mod, "gather_exit_gate_facts", _fake_facts
        )

        # 钉住夹具真产生 park 判定（防测试再次静默降级成 exit-ok）
        from hiveweave.services.turn_exit import evaluate_turn_exit

        facts = await _fake_facts(agent, [])
        fixture_decision = evaluate_turn_exit(
            facts.to_context(), emit_telemetry=False
        )
        assert not fixture_decision.ok
        assert fixture_decision.should_park
        assert not fixture_decision.should_repair
        assert fixture_decision.violations == ["OPEN_TASKS_UNDECLARED"]

        hint = await completion_mod.build_gate_supplement_hint(agent, [])
        assert hint is None
        assert agent._turn_gate_count == 0
    finally:
        clear_pending_turn_result("a1")


@pytest.mark.asyncio
async def test_supplement_none_when_exit_ok(monkeypatch):
    """控制组：有效 commit + 无义务 ⇒ exit ok ⇒ 不补步、计数不动。"""
    from hiveweave.agents import completion as completion_mod

    clear_pending_turn_result("a1")
    set_pending_turn_result(
        "a1",
        {
            "schema_version": 1,
            "phase": "in_progress",
            "summary": "committed properly",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )
    try:
        agent = _stub_agent()

        async def _fake_facts(a, tool_calls):
            return completion_mod.ExitGateFacts(
                agent_id=a.id, project_id=a.project_id, tool_calls=tool_calls
            )

        monkeypatch.setattr(
            completion_mod, "gather_exit_gate_facts", _fake_facts
        )

        hint = await completion_mod.build_gate_supplement_hint(agent, [])
        assert hint is None
        assert agent._turn_gate_count == 0
    finally:
        clear_pending_turn_result("a1")


# ── ④ 跨 run 重触生产者已删除（验收：turn_exit_gate 新 run 占比 → 0）──


def test_no_cross_run_gate_retrigger_producer():
    """agent.py / completion.py 不再有任何 source=turn_exit_gate 的新 run。"""
    from hiveweave.agents.agent import Agent

    here = Path(__file__).resolve()
    root = here.parents[1] / "src" / "hiveweave"
    agent_src = (root / "agents" / "agent.py").read_text(encoding="utf-8")
    completion_src = (root / "agents" / "completion.py").read_text(
        encoding="utf-8"
    )
    tool_loop_src = (root / "llm" / "streamer" / "tool_loop.py").read_text(
        encoding="utf-8"
    )
    # 跨 run 重触方法已删除（残留文本只允许出现在注释/文档串里）
    assert not hasattr(Agent, "_retrigger_for_turn_gate")
    assert "def _retrigger_for_turn_gate" not in agent_src
    assert '"source": "turn_exit_gate"' not in agent_src
    assert '"source": "turn_exit_gate"' not in completion_src
    # 接缝已接入主链路：chat() 传 exit_gate_check，loop 在收口前调用
    assert "exit_gate_check=_gate_supplement_check" in agent_src
    assert "exit_gate_check" in tool_loop_src
    # completion 收口路径显式记录（不再重跑整轮）
    assert "build_gate_supplement_hint" in completion_src
    assert "TURN EXIT BLOCKED — GATE UNRESOLVED" in completion_src
