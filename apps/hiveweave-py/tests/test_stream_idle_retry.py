"""TEST_DSH_64 #3：首 chunk 后流空转（stream idle）改判 + 重醒 + empty_stream。

64 现场：首 chunk 后 150s 静默原抛 ``PermanentError(status=None)`` ⇒
① 不进 with_retry 重试；② 重醒门 is_upstream_death 判 False（无 region
文案无 marker）⇒ 重醒排队 0 条，两条死 run 全靠「恰好有未读 inbox」的
30s 冷却运气；③ empty_stream 无法区分「零 chunk」与「已落账后清空」，
5/5 带账 run 全污染。

终版修法（本文件逐条钉住）：
1. idle 改抛 ``RetryableError(error_code="stream_idle")``（http_stream）；
2. with_retry 对 stream_idle 专属重试恰 1 次（不经通用 5 次退避表），
   再 idle ⇒ ``PermanentError(error_code="stream_idle_exhausted")``；
3. 耗尽终态到重醒门 ⇒ durable wake 排队（60/180/600s）；
4. is_upstream_death 对 idle **仍 False**（与 60s 快熔共判据，不许连动）；
5. error_code 全链路透传（http_stream PermanentError 出口 → tool_loop
   error result → agent 重建异常）；
6. http_stream RetryableError 耗尽出口对 stream_idle 系**跳过**
   report_failure（堵「5 次累计」后门熔断）；
7. empty_stream 只有真零 chunk 才标 1。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.llm.retry import (
    STREAM_IDLE_ERROR_CODE,
    STREAM_IDLE_EXHAUSTED_ERROR_CODE,
    MAX_RETRIES,
    PermanentError,
    RetryHandler,
    RetryableError,
    is_stream_idle_error,
    is_stream_idle_exhausted,
    is_upstream_death,
)
from hiveweave.llm.streamer.http_stream import HttpStreamMixin

# ── 共用件 ───────────────────────────────────────────────────


class _FakeProvider:
    """Minimal ProviderConfig stand-in（对齐 test_stream_partial_usage）。"""

    provider_type = "fake"
    model_name = "fake-model"
    max_output_tokens = 4096
    supports_thinking = True
    context_window = 128_000

    def build_url(self) -> str:
        return "http://fake"

    def build_headers(self, session_id: str | None = None) -> dict:
        return {}

    def build_body(self, messages=None, stream=True, tools=None) -> dict:
        return {"messages": messages or [], "stream": stream}

    def extract_usage(self, event):
        return None

    def parse_stream_chunk(self, event):
        return []


_EXHAUSTED_MSG = (
    "Stream idle timeout (150.0s) — 流空转重试已耗尽（1 retry）"
)


def _idle_error() -> RetryableError:
    return RetryableError(
        "Stream idle timeout (150.0s)", error_code=STREAM_IDLE_ERROR_CODE
    )


def _exhausted_error() -> PermanentError:
    return PermanentError(
        _EXHAUSTED_MSG,
        status=None,
        error_code=STREAM_IDLE_EXHAUSTED_ERROR_CODE,
    )


# ── ① 谓词与契约常量 ─────────────────────────────────────────


def test_idle_constants_and_predicates():
    """跨组契约常量定值（组2 subagent 分类器按字面量对齐，改名=三处同改）。"""
    assert STREAM_IDLE_ERROR_CODE == "stream_idle"
    assert STREAM_IDLE_EXHAUSTED_ERROR_CODE == "stream_idle_exhausted"
    # 可重试形态：error_code 命中
    assert is_stream_idle_error(_idle_error()) is True
    # 文本兜底（无码重建形态）
    assert is_stream_idle_error(PermanentError("Stream idle timeout (150.0s)")) is True
    # 首 chunk 超时不是流空转
    assert is_stream_idle_error(RetryableError("First chunk timeout (90.0s)")) is False
    assert is_stream_idle_error(None) is False
    # 耗尽终态与可重试态互斥
    assert is_stream_idle_exhausted(_exhausted_error()) is True
    assert is_stream_idle_error(_exhausted_error()) is False
    assert is_stream_idle_exhausted(_idle_error()) is False
    assert is_stream_idle_exhausted(None) is False
    # 码被丢时文案 needle 兜底（agent 重建异常丢码的防御面）
    assert is_stream_idle_exhausted(PermanentError(_EXHAUSTED_MSG)) is True


def test_is_upstream_death_still_false_for_idle():
    """防回归钉子：idle 不许进 is_upstream_death（与 60s 快熔共判据）。"""
    assert is_upstream_death(_idle_error()) is False
    assert is_upstream_death(_exhausted_error()) is False
    # 文本形态（str(err)）同样不许命中
    assert is_upstream_death(_EXHAUSTED_MSG) is False
    assert is_upstream_death(str(_idle_error())) is False
    # 对照：既有判据不因本次改动受损
    assert is_upstream_death(PermanentError("HTTP 403: RegionError", status=403)) is True


# ── ② with_retry 空转专属重试 ────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_idle_retries_exactly_once_then_exhausted(monkeypatch):
    """同请求重试恰 1 次：第 2 次仍 idle ⇒ PermanentError(exhausted)。"""
    sleeps: list[float] = []

    async def _no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise _idle_error()

    with pytest.raises(PermanentError) as ei:
        await RetryHandler().with_retry(fn)
    assert calls["n"] == 2
    assert ei.value.error_code == STREAM_IDLE_EXHAUSTED_ERROR_CODE
    assert "Stream idle timeout" in str(ei.value)
    assert "流空转重试已耗尽" in str(ei.value)
    # 不经通用退避表：全程零 sleep（idle 判定本身已烧满 150s 窗）
    assert sleeps == []


@pytest.mark.asyncio
async def test_with_retry_idle_then_success_recovers():
    """第 1 次 idle → 重试成功 ⇒ 正常返回，无异常。"""
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _idle_error()
        return {"ok": True}

    assert await RetryHandler().with_retry(fn) == {"ok": True}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_with_retry_idle_retry_does_not_consume_generic_budget(monkeypatch):
    """idle 专属预算与通用 max_retries 完全独立：1 次 idle 重试不占通用
    attempt 名额 —— 429 仍享有完整 MAX_RETRIES 次（合计 1+1+MAX_RETRIES 次）。"""
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _idle_error()
        raise RetryableError("HTTP 429: rate limit", status=429)

    with pytest.raises(RetryableError, match="429"):
        await RetryHandler().with_retry(fn)
    assert calls["n"] == 2 + MAX_RETRIES


# ── ③ 耗尽终态 → durable 重醒排队 ────────────────────────────


def _fake_wait_contract(monkeypatch, scheduled: list):
    from hiveweave.services import wait_contract as wc

    async def fake_schedule(project_id, agent_id, *, run_id=""):
        scheduled.append((project_id, agent_id, run_id))
        return {
            "scheduled": True,
            "attempt": 1,
            "delay_ms": 60_000,
            "wake_at": 0,
            "wait_id": "w1",
        }

    monkeypatch.setattr(wc, "schedule_upstream_recovery_wait", fake_schedule)
    from hiveweave.services import health_notice as hn

    monkeypatch.setattr(hn, "notify_upstream_recovery_exhausted", AsyncMock())
    monkeypatch.setattr(hn, "notify_upstream_deaths", AsyncMock())


@pytest.mark.asyncio
async def test_exhausted_idle_reaches_reawaken_gate(monkeypatch):
    """耗尽终态到重醒门 → schedule_upstream_recovery_wait 排队。"""
    import hiveweave.agents.agent as agent_mod

    scheduled: list = []
    _fake_wait_contract(monkeypatch, scheduled)
    err = _exhausted_error()
    await agent_mod.Agent._maybe_schedule_upstream_recovery(
        SimpleNamespace(id="ag-1", project_id="p1", _current_run_id="r1"), err
    )
    assert scheduled == [("p1", "ag-1", "r1")]


@pytest.mark.asyncio
async def test_plain_idle_retryable_does_not_wake(monkeypatch):
    """可重试形态（未耗尽）不排重醒 —— 重醒只接耗尽终态窄谓词。"""
    import hiveweave.agents.agent as agent_mod

    scheduled: list = []
    _fake_wait_contract(monkeypatch, scheduled)
    await agent_mod.Agent._maybe_schedule_upstream_recovery(
        SimpleNamespace(id="ag-1", project_id="p1", _current_run_id="r1"),
        _idle_error(),
    )
    assert scheduled == []


@pytest.mark.asyncio
async def test_agent_rebuild_permanent_error_keeps_error_code(monkeypatch):
    """暗坑②：agent else 分支重建 PermanentError 必须保 error_code。

    直接驱动重建表达式的同构路径：result dict 带 stream_idle_exhausted
    ⇒ 重建异常被窄谓词认出；文案 needle 同时兜底。
    """
    import hiveweave.agents.agent as agent_mod

    # 重建形态（与 agent.py else 分支同参）
    result = {
        "error": _EXHAUSTED_MSG,
        "error_status": None,
        "error_code": STREAM_IDLE_EXHAUSTED_ERROR_CODE,
    }
    rebuilt = PermanentError(
        result["error"],
        status=result["error_status"],
        error_code=result.get("error_code"),
    )
    # 真实重醒门：monkeypatch is_upstream_death=False（组2/组4 测试同款），
    # 只靠窄谓词放行
    monkeypatch.setattr(agent_mod, "is_upstream_death", lambda err: False)
    scheduled: list = []
    _fake_wait_contract(monkeypatch, scheduled)
    await agent_mod.Agent._maybe_schedule_upstream_recovery(
        SimpleNamespace(id="ag-1", project_id="p1", _current_run_id="r1"),
        rebuilt,
    )
    assert scheduled == [("p1", "ag-1", "r1")]
    # 若重建丢码，文案 needle 仍兜底（防御面，不许静默退化）
    assert is_stream_idle_exhausted(
        PermanentError(result["error"], status=None)
    ) is True


# ── ⑥ http_stream 出口：跳过熔断 + error_code 透出 ────────────


def _mk_mixin(with_retry_exc: BaseException) -> HttpStreamMixin:
    mixin = HttpStreamMixin()
    breaker = MagicMock()
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    breaker.open_for = AsyncMock()
    mixin._circuit_breaker = breaker
    mixin._retry_handler = MagicMock()
    mixin._retry_handler.with_retry = AsyncMock(side_effect=with_retry_exc)
    return mixin


@pytest.mark.asyncio
async def test_idle_retryable_error_skips_breaker_report():
    """暗坑③：stream_idle 系 RetryableError 耗尽出口**不喂** report_failure
    （堵「5 次累计」后门熔断），但 error_code 随错误结果带出。"""
    mixin = _mk_mixin(_idle_error())
    result = await mixin._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
    )
    assert result["status"] == "error"
    assert result["error_code"] == STREAM_IDLE_ERROR_CODE
    mixin._circuit_breaker.report_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_retryable_error_still_feeds_breaker():
    """对照：非 idle 的可重试耗尽（429）维持既有 report_failure 语义。"""
    mixin = _mk_mixin(RetryableError("HTTP 429: rate limit", status=429))
    result = await mixin._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
    )
    assert result["status"] == "error"
    mixin._circuit_breaker.report_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_exhausted_permanent_error_result_carries_code_and_skips_breaker():
    """耗尽终态：PermanentError 出口透出 error_code（暗坑②上游面），
    且 is_upstream_death=False ⇒ 不开快熔（open_for 不被调）。"""
    mixin = _mk_mixin(_exhausted_error())
    result = await mixin._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
    )
    assert result["status"] == "error"
    assert result["error_code"] == STREAM_IDLE_EXHAUSTED_ERROR_CODE
    assert result["error_status"] is None
    mixin._circuit_breaker.open_for.assert_not_awaited()
    mixin._circuit_breaker.report_failure.assert_not_awaited()


# ── ⑤ tool_loop error result 透传 error_code（暗坑①）─────────


@pytest.mark.asyncio
async def test_tool_loop_error_result_passes_error_code_through(monkeypatch):
    """round_result 的 error_code 此前在 tool_loop 组 error result 时被丢；
    现经 stream() 全链路透出。"""
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create = MagicMock(return_value=_FakeProvider())
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=MagicMock(allowed=True, fallback=None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=RetryHandler(max_retries=0),
    )
    streamer._stream_with_empty_retry = AsyncMock(
        return_value={
            "status": "error",
            "text": "",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": None,
            "error": _EXHAUSTED_MSG,
            "error_status": None,
            "error_code": STREAM_IDLE_EXHAUSTED_ERROR_CODE,
        }
    )
    result = await streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": "fake", "model_id": "m1"},
        tools=None,
    )
    assert result["status"] == "error"
    assert result["error_code"] == STREAM_IDLE_EXHAUSTED_ERROR_CODE
    assert result["error_status"] is None


# ── ① http_stream 抛出点：首 chunk 后 idle → RetryableError ──


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def iter_bytes(self):
        yield b'data: {"fake": 1}\n\n'
        # 一个事件后静默 —— 模拟首 chunk 后流空转
        self._release.wait(timeout=10)
        return

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        self._release.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeClient:
    def __init__(self, timeout=None) -> None:
        self._release = threading.Event()

    def stream(self, *args, **kwargs):
        return _FakeResponse(self._release)

    def close(self) -> None:
        self._release.set()


@pytest.mark.asyncio
async def test_idle_after_first_chunk_raises_retryable_with_stream_idle_code(
    monkeypatch,
):
    """http_stream 抛出点：got_event=True 后静默 ⇒ RetryableError
    （文案保留 "Stream idle timeout"，error_code=stream_idle）。"""
    import httpx

    import hiveweave.llm.streamer.http_stream as http_stream_mod

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    # 首事件前给足时间让假响应送达；首事件后 0.05s 即判 idle
    monkeypatch.setattr(
        http_stream_mod,
        "stream_chunk_wait_s",
        lambda *, got_event: 5.0 if not got_event else 0.05,
    )
    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    provider = SimpleNamespace(
        extract_usage=lambda _e: None,
        parse_stream_chunk=lambda _e: [],
    )
    with pytest.raises(RetryableError, match="Stream idle timeout") as ei:
        await mixin._do_streaming_request(
            agent_id="a1",
            provider=provider,
            url="http://example.invalid",
            headers={},
            body={},
            on_delta=None,
            delta_id="d1",
            round_num=1,
            budget_deadline=None,
        )
    assert ei.value.error_code == STREAM_IDLE_ERROR_CODE
    # 对照钉子：首 chunk 前的超时仍是原 RetryableError 语义（无 stream_idle 码）
    assert is_stream_idle_error(
        RetryableError("First chunk timeout (90.0s)")
    ) is False


# ── ⑦ empty_stream：真零 chunk 才标 1 ────────────────────────


class _FactLedger:
    def __init__(self) -> None:
        self.facts: list[tuple] = []

    async def set_run_fact(self, agent_id, run_id, **kw) -> None:
        self.facts.append((agent_id, run_id, kw))


def _flush_agent(
    *,
    pending: list,
    saw_chunk: bool,
    run_id: str | None = "r1",
    ledger: _FactLedger | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="ag-1",
        project_id="p1",
        _pending_usage=pending,
        _current_run_id=run_id,
        _run_ledger=ledger or _FactLedger(),
        _run_saw_stream_chunk=saw_chunk,
    )


@pytest.mark.asyncio
async def test_empty_stream_true_zero_chunk_marks_one():
    """真·零 chunk（请求已发起、无任何 chunk）→ empty_stream=1。"""
    from hiveweave.agents import recovery

    ledger = _FactLedger()
    agent = _flush_agent(pending=[], saw_chunk=False, ledger=ledger)
    await recovery._flush_pending_usage(agent, reason="error")
    assert ledger.facts == [("ag-1", "r1", {"empty_stream": 1})]


@pytest.mark.asyncio
async def test_empty_stream_consumed_sink_not_marked():
    """64 污染面封堵：账已在别处落库后清空（sink 空）但 run 见过 chunk
    ⇒ **不**标 empty_stream。"""
    from hiveweave.agents import recovery

    ledger = _FactLedger()
    agent = _flush_agent(pending=[], saw_chunk=True, ledger=ledger)
    await recovery._flush_pending_usage(agent, reason="cancelled_by_user")
    assert ledger.facts == []


@pytest.mark.asyncio
async def test_empty_stream_no_run_id_is_noop():
    """无 run id → 无处落事实，也不炸。"""
    from hiveweave.agents import recovery

    ledger = _FactLedger()
    agent = _flush_agent(pending=[], saw_chunk=False, run_id=None, ledger=ledger)
    await recovery._flush_pending_usage(agent, reason="safety_timeout")
    assert ledger.facts == []


@pytest.mark.asyncio
async def test_on_delta_marks_chunk_seen_for_content_events(monkeypatch):
    """agent 侧证据位：text/thinking delta 置位；llm_queue / round_start
    不置位（都不是 chunk 到达的证据）。"""
    import hiveweave.agents.agent as agent_mod

    monkeypatch.setattr(
        agent_mod._agent_streaming, "on_delta", AsyncMock()
    )
    for ev_type, expected in (
        ("text_delta", True),
        ("thinking_delta", True),
        ("llm_queue", False),
        ("round_start", False),
    ):
        agent = SimpleNamespace(_run_saw_stream_chunk=False)
        await agent_mod.Agent._on_delta(agent, {"type": ev_type})
        assert agent._run_saw_stream_chunk is expected, ev_type
