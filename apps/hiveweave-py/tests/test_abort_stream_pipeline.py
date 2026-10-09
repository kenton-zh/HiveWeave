"""N3 批28-1 施工2：abort 信号管道（抄 pi options.signal / DSH 替换结果）。

上游参照（pi，最新 HEAD 已核验）：
- ``packages/ai/src/api/openai-completions.ts:371/688/718`` —— signal 全
  API 层透传，abort 时 ``stopReason="aborted"`` **而非 error**；
- DSH timeout-policy —— 超时时替换结果而非杀进程，结构化码可路由。

本仓形态（同步 httpx 线程 + queue 事件泵，signal 传不进去）：
模块级注册表（abort_source）+ 请求循环周期检查（每 chunk + 2s 心动
poller）⇒ 关 client 断流 + 归类 ``aborted``（非 error，run 走 interrupted
语义）。平台侧入口 ``abort_stream(agent_id, run_id, reason)``，run_id
做陈旧判废。用户主动停止仍走 task.cancel() → CancelledError（不双轨）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hiveweave.llm.streamer.abort_source import (
    abort_stream,
    active_abort_sources,
    register_abort_source,
    reset_abort_registry,
    unregister_abort_source,
)
from hiveweave.llm.streamer.http_stream import HttpStreamMixin
from hiveweave.llm.retry import RetryHandler
from hiveweave.llm.streamer.sse import sse_to_chunks


# ── SSE / 传输替身（形态抄 test_first_byte_total_cap.py）──────────


def _sse_text(content: str) -> bytes:
    return (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": content}}]})
        + "\n\n"
    ).encode()


def _sse_finish(reason: str = "stop") -> bytes:
    return (
        "data: "
        + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]})
        + "\n\n"
    ).encode()


_SSE_DONE = b"data: [DONE]\n\n"


class _FakeProvider:
    """ProviderConfig 替身：真实 SSE→chunk 解析（sse_to_chunks）。"""

    model_name = "fake-model"

    def build_url(self) -> str:
        return "http://fake"

    def build_headers(self, session_id: str | None = None) -> dict:
        return {}

    def build_body(self, messages=None, stream=True, **kwargs) -> dict:
        return {"messages": messages or [], "stream": stream}

    def extract_usage(self, event):
        return None

    def parse_stream_chunk(self, event):
        return sse_to_chunks(event)


class _ScriptedResponse:
    """按给定字节序列 yield 的 200 响应替身（可选事件间隔）。"""

    status_code = 200
    headers: dict = {}

    def __init__(self, chunks: list[bytes], *, interval_s: float = 0.0) -> None:
        self._chunks = chunks
        self._interval = interval_s
        self._release = threading.Event()

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            if self._release.is_set():
                return
            if self._interval:
                time.sleep(self._interval)
            yield chunk

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_ScriptedResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._release.set()


class _ChunkThenHangResponse:
    """yield 若干 chunk 后挂死（静默流）：close() 唤醒线程退出。"""

    status_code = 200
    headers: dict = {}

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._release = threading.Event()

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            if self._release.is_set():
                return
            yield chunk
        self._release.wait(timeout=30)

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_ChunkThenHangResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._release.set()


_STATE: dict = {}


class _ScriptedClient:
    """httpx.Client 替身：单行为应答（本文件每测试只发一次请求）。"""

    def __init__(self, timeout: object = None) -> None:
        self.timeout = timeout

    def stream(self, *args: object, **kwargs: object) -> object:
        _STATE["attempts"] += 1
        behavior = _STATE.get("behavior") or _ScriptedResponse([])
        _STATE["current"] = behavior
        return behavior

    def close(self) -> None:
        current = _STATE.get("current")
        if current is not None and hasattr(current, "close"):
            current.close()


def _reset_state(behavior) -> None:
    _STATE.clear()
    _STATE.update({"attempts": 0, "behavior": behavior, "current": None})


def _mk_mixin() -> HttpStreamMixin:
    mixin = HttpStreamMixin()
    mixin._circuit_breaker = SimpleNamespace(
        report_success=AsyncMock(),
        report_failure=AsyncMock(),
        open_for=AsyncMock(),
    )
    mixin._retry_handler = RetryHandler()
    mixin._fire_delta = AsyncMock()
    return mixin


async def _do_stream(mixin: HttpStreamMixin, agent_id: str = "a1"):
    return await mixin._do_streaming_request(
        agent_id=agent_id,
        provider=_FakeProvider(),
        url="http://fake",
        headers={},
        body={"messages": [], "stream": True},
        on_delta=None,
        delta_id="d1",
        round_num=1,
        first_byte_budget=None,
    )


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """全新 LLM 信号量（跨 loop 安全）+ abort 注册表测试后清空。"""
    import hiveweave.llm.streamer.http_stream as hs
    import hiveweave.llm.streamer.oneshot as os_

    monkeypatch.setattr(hs, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))
    monkeypatch.setattr(os_, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))
    reset_abort_registry()
    yield
    reset_abort_registry()
    # 挂死替身兜底释放（防御断言先炸导致线程悬挂）
    current = _STATE.get("current")
    if current is not None and hasattr(current, "close"):
        current.close()


# ── ① 注册表单元：登记 / 置位 / 注销 / 陈旧判废 ──────────────────


async def test_registry_register_abort_unregister_lifecycle():
    src = register_abort_source("ag1", run_id="r1", round_num=3)
    assert active_abort_sources()["ag1"] is src
    assert not src.event.is_set()
    # 无 run_id 匹配要求时（None）按 agent 掐
    assert abort_stream("ag1", reason="detector") is True
    assert src.event.is_set()
    assert src.reason == "detector"
    # 重复置位幂等
    assert abort_stream("ag1", reason="again") is True
    assert src.reason == "detector"
    assert unregister_abort_source("ag1", src) is True
    assert "ag1" not in active_abort_sources()
    # 注销后掐不到
    assert abort_stream("ag1") is False


async def test_registry_stale_run_abort_rejected():
    src = register_abort_source("ag2", run_id="run-new")
    # 老 run 的迟到 abort：不掐新 run 的流
    assert abort_stream("ag2", run_id="run-old", reason="late") is False
    assert not src.event.is_set()
    # run_id 匹配才生效
    assert abort_stream("ag2", run_id="run-new", reason="on-time") is True
    assert src.event.is_set()
    unregister_abort_source("ag2", src)


async def test_registry_unbound_run_id_is_permissive():
    """未接线 run_id 的旁路流（meetings/subagent）：按 agent 掐（best-effort）。"""
    src = register_abort_source("ag3", run_id=None)
    assert abort_stream("ag3", run_id="whatever", reason="x") is True
    assert src.event.is_set()
    unregister_abort_source("ag3", src)


async def test_registry_no_active_source_is_fast_miss():
    reset_abort_registry()
    assert abort_stream("ghost", run_id="r", reason="x") is False
    assert active_abort_sources() == {}


# ── ② 流中触发 abort：请求终止、分类 aborted（非 error）──────────


async def test_abort_during_active_stream_classifies_aborted(monkeypatch):
    """活跃流（chunk 持续到达）：per-chunk 快速检查掐流。"""
    import hiveweave.llm.streamer.http_stream as hs

    # 心动 poller 兜底也压快（本用例主要走 per-chunk 检查）
    monkeypatch.setattr(hs, "ABORT_POLL_S", 0.05)
    chunks = [_sse_text("hello ") for _ in range(200)]  # 0.02s×200 ≈ 4s 慢流
    _reset_state(_ScriptedResponse(chunks, interval_s=0.02))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()

    async def _abort_later() -> None:
        await asyncio.sleep(0.15)
        assert abort_stream("a1", reason="lease_exceeded") is True

    aborter = asyncio.ensure_future(_abort_later())
    t0 = time.monotonic()
    result = await _do_stream(mixin, agent_id="a1")
    elapsed = time.monotonic() - t0
    await aborter

    # 分类 aborted：非 error（status ok + finish_reason="aborted" + 事实位）
    assert result["status"] == "ok"
    assert result["aborted"] is True
    assert result["finish_reason"] == "aborted"
    assert result["abort_reason"] == "lease_exceeded"
    # 已收的部分文本保留；不完整 tool_calls 丢弃（空）
    assert 0 < len(result["text"]) < len("hello ") * 200
    assert result["tool_calls"] == []
    # 掐得够快（远短于整流时长；远早于 idle/首块看门狗）
    assert elapsed < 2.0
    # 单次请求，无重试（aborted 不进退避）
    assert _STATE["attempts"] == 1
    # 请求结束注销
    assert active_abort_sources() == {}


async def test_abort_during_silent_stream_polled_by_heartbeat(monkeypatch):
    """静默流（首 chunk 后长时间无事件）：2s 心动 poller 是唯一掐流时钟。"""
    import hiveweave.llm.streamer.http_stream as hs

    monkeypatch.setattr(hs, "ABORT_POLL_S", 0.05)
    monkeypatch.setattr(
        hs, "stream_chunk_wait_s", lambda *, got_event: 30.0
    )  # 压长 idle 窗，证明掐流不依赖 wait 超时
    _reset_state(_ChunkThenHangResponse([_sse_text("first")]))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()

    async def _abort_later() -> None:
        await asyncio.sleep(0.2)
        assert abort_stream("a1", reason="silence_6min") is True

    aborter = asyncio.ensure_future(_abort_later())
    t0 = time.monotonic()
    result = await _do_stream(mixin, agent_id="a1")
    elapsed = time.monotonic() - t0
    await aborter

    assert result["status"] == "ok"
    assert result["aborted"] is True
    assert result["finish_reason"] == "aborted"
    assert result["abort_reason"] == "silence_6min"
    assert result["text"] == "first"
    # poller 心动掐流（≪ idle 窗 30s）
    assert elapsed < 5.0
    assert active_abort_sources() == {}


async def test_stale_run_abort_does_not_kill_active_stream(monkeypatch):
    """陈旧 run 的 abort 被拒：在途流完整跑完（分类照常 ok/stop）。"""
    chunks = [_sse_text("data "), _sse_finish("stop"), _SSE_DONE]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    mixin._run_id = "run-live"

    async def _stale_abort_later() -> None:
        await asyncio.sleep(0.05)
        # 老 run 的 abort：必须被拒
        assert abort_stream("a1", run_id="run-dead", reason="late") is False

    aborter = asyncio.ensure_future(_stale_abort_later())
    result = await _do_stream(mixin, agent_id="a1")
    await aborter

    assert result["status"] == "ok"
    assert "aborted" not in result
    assert result["finish_reason"] == "stop"
    assert result["text"] == "data "
    assert active_abort_sources() == {}


# ── ③ 未触发零开销 + 正常完成注销 ──────────────────────────────


async def test_normal_completion_unregisters_no_abort_flags(monkeypatch):
    """无人掐流：流正常完成、注册表注销、结果不带 abort 位。"""
    chunks = [_sse_text("ok text"), _sse_finish("stop"), _SSE_DONE]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    # 请求进行中注册表有条目（可被观测）
    assert active_abort_sources() == {}
    result = await _do_stream(mixin, agent_id="a1")
    assert result["status"] == "ok"
    assert result["text"] == "ok text"
    assert result["finish_reason"] == "stop"
    assert "aborted" not in result
    # 正常完成 ⇒ 注销
    assert active_abort_sources() == {}


# ── ④ CancelledError 语义不回归（用户停止仍走 task.cancel 路径）────


async def test_task_cancel_still_raises_cancelled_error(monkeypatch):
    """用户主动停止（task.cancel）⇒ CancelledError 照常抛穿，不被 abort
    管道吞掉或改分类（用户停止与平台 abort 不双轨：各走各的既有语义）。"""
    _reset_state(_ChunkThenHangResponse([_sse_text("x")]))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    outer = asyncio.ensure_future(_do_stream(mixin, agent_id="a9"))
    await asyncio.sleep(0.1)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    # 取消路径同样注销（finally 兜底）
    assert active_abort_sources() == {}


# ── ⑤ streamer 中游：aborted 不进空响应退避、不冒充链成功 ────────


async def test_aborted_result_not_retried_as_empty():
    """aborted 结果（可能无文本）不被当空响应退避重试。"""
    mixin = _mk_mixin()
    aborted_result = {
        "status": "ok",
        "text": "",
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "aborted",
        "usage": None,
        "aborted": True,
        "abort_reason": "lease_exceeded",
    }
    mixin._stream_single_round = AsyncMock(return_value=aborted_result)
    result = await mixin._stream_with_empty_retry(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=0,
    )
    assert mixin._stream_single_round.await_count == 1
    assert result["aborted"] is True


async def test_aborted_result_skips_success_reporting():
    """aborted 不冒充链成功：不喂熔断器 report_success（人为掐 ≠ 上游健康
    证据）。"""
    mixin = _mk_mixin()
    aborted_result = {
        "status": "ok",
        "text": "partial",
        "thinking": "",
        "tool_calls": [],
        "finish_reason": "aborted",
        "usage": None,
        "aborted": True,
        "abort_reason": "x",
    }
    mixin._retry_handler = MagicMock(
        with_retry=AsyncMock(return_value=aborted_result)
    )
    result = await mixin._run_round_chain(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=0,
        delta_id="d1",
    )
    assert result["aborted"] is True
    mixin._circuit_breaker.report_success.assert_not_awaited()


# ── ⑥ tool_loop：aborted 轮 ⇒ interrupted 语义收口 ───────────────


class _FakeLoopProvider(_FakeProvider):
    provider_type = "fake"
    supports_thinking = False
    fallback = None
    max_output_tokens = 4096
    context_window = 128_000


async def test_tool_loop_aborted_round_interrupted_semantics():
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeLoopProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(
        return_value=MagicMock(allowed=True, fallback=None)
    )
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

    calls = {"stream": 0}

    async def fake_stream(**kwargs):
        calls["stream"] += 1
        return {
            "status": "ok",
            "text": "被掐前的部分输出",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": "aborted",
            "usage": None,
            "aborted": True,
            "abort_reason": "lease_exceeded",
        }

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]

    result = await streamer._run_tool_loop(
        agent_id="a1",
        provider=_FakeLoopProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )
    # 单轮即收口（不再开新轮）
    assert calls["stream"] == 1
    # interrupted 语义：status ok（非 error）+ aborted 事实位 + 稳定归因
    assert result["status"] == "ok"
    assert result["aborted"] is True
    assert result["abort_reason"] == "lease_exceeded"
    assert result["steering_reason"] == "stream_aborted"
    # 产出保留 + 收口说明可辨识
    assert "被掐前的部分输出" in result["content"]
    assert "[STREAM ABORTED]" in result["content"]
    assert "lease_exceeded" in result["content"]


# ── ⑦ completion：aborted ⇒ run interrupted（非 error / 非自然完成）──


def _completion_agent(run_id: str | None = "run-ab-1"):
    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent.id = "test-abort-agent"
    agent.project_id = "test-abort-project"
    agent.config = {"name": "ab", "role": "executor"}
    agent.disposition = "runnable"
    agent.pending_inbox_msg_ids = None
    agent.current_job = {"started_at": 0}
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._TASK_REMINDER_MAX = 2
    agent._unreplied_asks_streak = 0
    agent._no_progress_streak = 0
    agent._progress_fingerprint = None
    agent._empty_done_slice_streak = 0
    agent._slice_budget = 0
    agent._current_run_id = run_id
    agent._streaming_msg_id = None
    agent._stream_timeout_streak = 0
    agent._rate_limit_streak = 0
    agent._resume_cooldown_until = 0.0
    agent._run_ledger = MagicMock(
        complete_run=AsyncMock(), interrupt_run=AsyncMock()
    )
    agent._work_log = MagicMock(write_work_log=AsyncMock())
    agent._chat_msg = MagicMock(save_message=AsyncMock())
    agent._conversation = MagicMock(append_turn=AsyncMock())
    agent._cancel_safety_timer = MagicMock()
    agent._broadcast_stream_event = MagicMock()
    agent._go_idle = AsyncMock()
    agent._broadcast_agent_health = MagicMock()
    agent._drain_message_queue = AsyncMock()
    agent._clear_resume_suppressed = MagicMock()
    agent._maybe_self_retrigger = AsyncMock()
    agent._retrigger_for_open_tasks = AsyncMock()
    return agent


async def test_completion_aborted_interrupts_run(monkeypatch):
    """平台掐流的 run：interrupt_run（reason=stream_aborted）而非
    complete/error；不自动续跑（abort 发起方拥有后续处置权）。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "turn_exit_mode", "legacy")

    import hiveweave.agents.completion as completion_mod
    from hiveweave.agents.completion import ExitGateFacts, handle_completion

    async def fake_gather(ag, tool_calls):
        return ExitGateFacts(
            agent_id=ag.id,
            project_id=ag.project_id,
            tool_calls=[],
            open_obligations=[],
        )

    monkeypatch.setattr(
        completion_mod, "gather_exit_gate_facts", fake_gather
    )

    agent = _completion_agent(run_id="run-ab-1")
    await handle_completion(
        agent,
        {
            "status": "ok",
            "content": "部分产出保留",
            "thinking": "",
            "tool_calls": [],
            "tool_turn_messages": [],
            "rounds": 2,
            "aborted": True,
            "abort_reason": "lease_exceeded",
        },
        "hi",
        {"trigger": False},
    )
    # run 标 interrupted（非 error、非正常 completed）
    agent._run_ledger.interrupt_run.assert_awaited_once()
    args = agent._run_ledger.interrupt_run.await_args
    assert args.args[0] == agent.id
    assert args.args[1] == "run-ab-1"
    assert args.kwargs.get("reason") == "stream_aborted"
    # 不自动续跑：不武装预算延续唤醒 / 不 retrigger
    assert not getattr(agent, "_budget_continue_armed_run", None)
    agent._maybe_self_retrigger.assert_not_awaited()
    agent._retrigger_for_open_tasks.assert_not_awaited()
    assert agent.disposition == "runnable"
