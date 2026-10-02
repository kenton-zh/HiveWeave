"""T3'（TEST_DSH_78）：pre-first-chunk 悬挂逃逸看门狗 —— 零输出总墙钟帽。

事故形态：首轮 kickoff 请求已发、上游（longcat）一个 chunk 都不来、悬挂
11 分钟（``llm_start``/``llm_queue``/流事件全零、``is_streaming`` 未置位、
interrupt note="request started but zero chunks arrived"）。根因：既有三层
守卫（FIRST_CHUNK 90s / IDLE 150s / PING 15s）全是**单次尝试**语义，
``RetryableError`` 重试链的**总墙钟**无人负责 —— 90s × 重试 + 退避可合法
拉到 8-11 分钟。

本文件钉住：
1. 零 chunk 慢死流（永不产数的 body）⇒ 总帽到点
   ``PermanentError(first_byte_total_timeout)``，总耗时 ≤ cap + 余量，
   重试被帽打断的次数有界（attempt 边界 + 流内双检查点）；
2. 只认上游真实字节续命：首字节到达即溶解（跨重试永久）—— 健康慢流
   （首块快到、之后正常长流）回归零变化，可合法长过 cap；
3. 空流终态校验：传输正常关闭但从未见到终止事件
   （message_stop / finish / [DONE]）⇒ ``RetryableError(stream_no_terminator)``
   （对齐 pi「ended without message_stop」先例），不许当成功返回；
4. httpx 全量四元组（connect/read/write/pool）显式齐全（主链路 + oneshot）；
5. ``llm_retry_scheduled`` 结构化日志（attempt/max_retries/delay_ms/reason）。

快测缩放：全 mock 传输（无真实网络），cap / 等待窗用 monkeypatch 压到
亚秒级；挂死响应由 close()（看门狗主动断流）唤醒线程，不留悬挂线程。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from structlog.testing import capture_logs

from hiveweave.llm.retry import (
    MAX_RETRIES,
    PermanentError,
    RetryHandler,
    RetryableError,
)
from hiveweave.llm.streamer.constants import (
    STREAM_SOCKET_CONNECT_TIMEOUT_S,
    STREAM_SOCKET_POOL_TIMEOUT_S,
    STREAM_SOCKET_READ_TIMEOUT_S,
    STREAM_SOCKET_WRITE_TIMEOUT_S,
)
from hiveweave.llm.streamer.http_stream import HttpStreamMixin
from hiveweave.llm.streamer.oneshot import (
    OneshotFirstByteTotalTimeout,
    stream_oneshot,
    stream_oneshot_with_retry,
)
from hiveweave.llm.streamer.sse import sse_to_chunks

_FB_CODE = "first_byte_total_timeout"
_NO_TERM_CODE = "stream_no_terminator"


# ── SSE / 传输替身 ──────────────────────────────────────────


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


class _HangResponse:
    """永不产数的 200 响应：连接打开、零字节，close() 唤醒线程退出。"""

    status_code = 200
    headers: dict = {}

    def __init__(self) -> None:
        self._release = threading.Event()

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        self._release.wait(timeout=30)
        return
        yield b""  # pragma: no cover — 声明为生成器

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_HangResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._release.set()


class _ScriptedResponse:
    """按给定字节序列 yield 的 200 响应替身（可选事件间隔）。"""

    status_code = 200
    headers: dict = {}

    def __init__(
        self, chunks: list[bytes], *, interval_s: float = 0.0
    ) -> None:
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


_STATE: dict = {}


class _ScriptedClient:
    """httpx.Client 替身：按 behaviors 顺序应答；耗尽后回退挂死响应。"""

    def __init__(self, timeout: object = None) -> None:
        self.timeout = timeout

    def stream(self, *args: object, **kwargs: object) -> object:
        _STATE["attempts"] += 1
        if _STATE["behaviors"]:
            behavior = _STATE["behaviors"].pop(0)
        else:
            behavior = _HangResponse()
        _STATE["current"] = behavior
        return behavior

    def close(self) -> None:
        current = _STATE.get("current")
        if current is not None and hasattr(current, "close"):
            current.close()


def _reset_state(behaviors: list) -> None:
    _STATE.clear()
    _STATE.update({"attempts": 0, "behaviors": behaviors, "current": None})


def _mk_mixin() -> HttpStreamMixin:
    """带真实 RetryHandler 的 mixin（重试链真实发生，帽才被打断）。"""
    mixin = HttpStreamMixin()
    mixin._circuit_breaker = SimpleNamespace(
        report_success=AsyncMock(),
        report_failure=AsyncMock(),
        open_for=AsyncMock(),
    )
    mixin._retry_handler = RetryHandler()
    mixin._fire_delta = AsyncMock()
    return mixin


def _stream_round(mixin: HttpStreamMixin, **kwargs: object):
    return mixin._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _fresh_semaphore(monkeypatch):
    """每次测试换全新 LLM 信号量 —— 不触碰进程级全局（跨 loop 安全）。"""
    import hiveweave.llm.streamer.http_stream as hs
    import hiveweave.llm.streamer.oneshot as os_

    monkeypatch.setattr(hs, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))
    monkeypatch.setattr(os_, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))


# ── 1. 零 chunk 慢死流 ⇒ 总帽到点 PermanentError ─────────────


@pytest.mark.asyncio
async def test_zero_chunk_hang_trips_first_byte_total_cap(monkeypatch):
    """永不产数的 body：总帽到点 ⇒ PermanentError(first_byte_total_timeout)，
    总耗时 ≤ cap + 余量，重试被帽打断、次数有界（无无限重试）。"""
    import hiveweave.llm.streamer.http_stream as hs

    cap = 1.0
    monkeypatch.setattr(hs, "FIRST_BYTE_TOTAL_CAP_S", cap)
    # 单次尝试的首 chunk 判定窗压到 0.3s（< cap）：先合法吃 first-chunk
    # Retryable，重试链随后被总帽截断
    monkeypatch.setattr(hs, "stream_chunk_wait_s", lambda *, got_event: 0.3)
    monkeypatch.setattr(
        "hiveweave.llm.retry.compute_backoff", lambda *a, **k: 50
    )
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    t0 = time.monotonic()
    result = await _stream_round(mixin)
    elapsed = time.monotonic() - t0

    assert result["status"] == "error"
    assert result["error_code"] == _FB_CODE
    assert result["error_status"] is None
    assert "zero output" in str(result["error"])
    # DSH 不变量：总墙钟被帽住（事故形态是 11 分钟，此处 ≤ cap+余量）
    assert elapsed <= cap + 3.0
    # 重试被帽打断：尝试次数有界（不会烧满 5 次重试预算后再死）
    assert 2 <= _STATE["attempts"] <= 4
    # 终态码不许触发快熔（status=None 的 PermanentError 不是上游死亡）
    mixin._circuit_breaker.open_for.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cap_enforced_at_attempt_boundary_over_backoff_sleep(monkeypatch):
    """退避 sleep 计入墙钟：attempt 1 烧掉大半帽 + 退避越线 ⇒ 下一次
    attempt 在入口即被拒（不再发起新请求）。

    数值：首 chunk 窗 0.25s < cap−0.05（attempt 1 流内不触发帽），
    退避 0.15s ⇒ attempt 2 入口 ≈0.40s ≥ cap(0.4)。
    """
    import hiveweave.llm.streamer.http_stream as hs

    cap = 0.4
    monkeypatch.setattr(hs, "FIRST_BYTE_TOTAL_CAP_S", cap)
    monkeypatch.setattr(hs, "stream_chunk_wait_s", lambda *, got_event: 0.25)
    monkeypatch.setattr(
        "hiveweave.llm.retry.compute_backoff", lambda *a, **k: 150
    )
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    result = await _stream_round(mixin)

    assert result["status"] == "error"
    assert result["error_code"] == _FB_CODE
    # 重试确实被调度过（with_retry 想重试），但退避越线后 attempt 2 在
    # do_request 入口即被拒 —— 第二次 HTTP 请求从未发起
    fired = [c.args[1] for c in mixin._fire_delta.await_args_list if c.args]
    assert any(e.get("type") == "retry" for e in fired)
    assert _STATE["attempts"] == 1


@pytest.mark.asyncio
async def test_queue_wait_clamped_by_first_byte_cap(monkeypatch):
    """信号量排队同属零输出墙钟：被帽钳住，到点判死而非无限排队。"""
    import hiveweave.llm.streamer.http_stream as hs

    cap = 0.3
    monkeypatch.setattr(hs, "FIRST_BYTE_TOTAL_CAP_S", cap)
    monkeypatch.setattr(hs, "LLM_QUEUE_PING_S", 15.0)
    # 永不可获取的信号量：排队贯穿整个测试
    monkeypatch.setattr(hs, "_get_llm_semaphore", lambda: asyncio.Semaphore(0))
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    t0 = time.monotonic()
    result = await _stream_round(mixin)
    elapsed = time.monotonic() - t0

    assert result["status"] == "error"
    assert result["error_code"] == _FB_CODE
    assert elapsed <= cap + 2.0
    # 排队期间未满 15s ping 周期 ⇒ 不应有 llm_queue 事件
    fired = [c.args[1] for c in mixin._fire_delta.await_args_list if c.args]
    assert not any(e.get("type") == "llm_queue" for e in fired)


# ── 2. 只认真实字节续命：首字节溶解帽（健康慢流回归零变化）──


@pytest.mark.asyncio
async def test_slow_healthy_stream_outlives_cap_after_first_byte(monkeypatch):
    """健康慢流回归零变化：首字节 cap 内到达 ⇒ 帽溶解，流合法长过 cap。"""
    import hiveweave.llm.streamer.http_stream as hs

    cap = 1.0
    monkeypatch.setattr(hs, "FIRST_BYTE_TOTAL_CAP_S", cap)
    monkeypatch.setattr(
        hs,
        "stream_chunk_wait_s",
        lambda *, got_event: 5.0 if not got_event else 0.5,
    )
    n = 30
    chunks = [_sse_text("x") for _ in range(n)] + [_sse_finish("stop")]
    # 总时长 ~1.5s > cap —— 若帽未溶解必被误杀
    _reset_state(behaviors=[_ScriptedResponse(chunks, interval_s=0.05)])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    result = await _stream_round(mixin)

    assert result["status"] == "ok"
    assert result["text"] == "x" * n
    assert result["finish_reason"] == "stop"
    assert _STATE["attempts"] == 1


@pytest.mark.asyncio
async def test_cap_voided_after_first_byte_across_retries(monkeypatch):
    """attempt1 见首字节后流中瞬态死亡 ⇒ 帽跨重试永久溶解；后续零 chunk
    重试走既有 first-chunk/通用重试语义，耗尽 ⇒ RetryableError（非 fb 码）。"""
    import hiveweave.llm.streamer.http_stream as hs

    cap = 0.8
    monkeypatch.setattr(hs, "FIRST_BYTE_TOTAL_CAP_S", cap)
    monkeypatch.setattr(
        hs,
        "stream_chunk_wait_s",
        lambda *, got_event: 0.2 if not got_event else 0.3,
    )
    monkeypatch.setattr(
        "hiveweave.llm.retry.compute_backoff", lambda *a, **k: 20
    )
    # attempt1：一个 text 事件后流中 500 错误（可重试 → 通用重试链）
    err_chunk = (
        "data: " + json.dumps({"error": {"message": "upstream server error"}})
        + "\n\n"
    ).encode()
    first = _ScriptedResponse(
        [_sse_text("y"), err_chunk], interval_s=0.02
    )
    _reset_state(behaviors=[first])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    t0 = time.monotonic()
    result = await _stream_round(mixin)
    elapsed = time.monotonic() - t0

    assert result["status"] == "error"
    # 帽已溶解：耗尽态是既有通用重试语义，不是零输出帽
    assert result["error_code"] != _FB_CODE
    # 证据 1：总墙钟超过 cap 仍走完了完整通用重试预算
    assert elapsed > cap
    assert _STATE["attempts"] == 1 + MAX_RETRIES


# ── 3. 空流终态校验（终止事件证据位）─────────────────────────


def _direct_stream(mixin: HttpStreamMixin):
    return mixin._do_streaming_request(
        agent_id="a1",
        provider=_FakeProvider(),
        url="http://example.invalid",
        headers={},
        body={},
        on_delta=None,
        delta_id="d1",
        round_num=1,
        budget_deadline=None,
    )


@pytest.mark.asyncio
async def test_stream_without_terminator_is_retryable_not_ok(monkeypatch):
    """有内容但传输正常关闭、无任何终止事件 ⇒ RetryableError(stream_no_terminator)。"""
    monkeypatch.setattr(
        httpx, "Client", lambda timeout=None: _ScriptedClient(timeout)
    )
    _reset_state(behaviors=[_ScriptedResponse([_sse_text("partial")])])

    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    with pytest.raises(RetryableError) as ei:
        await _direct_stream(mixin)
    assert ei.value.error_code == _NO_TERM_CODE


@pytest.mark.asyncio
async def test_zero_byte_eof_stream_is_not_success(monkeypatch):
    """200 + 零事件立即 EOF：旧行为当成功空响应（滑进空响应重试），
    现按截断流抛 RetryableError。"""
    monkeypatch.setattr(
        httpx, "Client", lambda timeout=None: _ScriptedClient(timeout)
    )
    _reset_state(behaviors=[_ScriptedResponse([])])

    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    with pytest.raises(RetryableError, match="terminator"):
        await _direct_stream(mixin)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks,provider",
    [
        pytest.param([_sse_text("a"), _SSE_DONE], "openai_done", id="done"),
        pytest.param(
            [_sse_text("b"), _sse_finish("stop")], "openai_finish", id="finish"
        ),
    ],
)
async def test_terminated_streams_still_return_ok(monkeypatch, chunks, provider):
    """终止证据齐全（[DONE] / finish）的流维持既有 ok 语义 —— 回归零变化。"""
    monkeypatch.setattr(
        httpx, "Client", lambda timeout=None: _ScriptedClient(timeout)
    )
    _reset_state(behaviors=[_ScriptedResponse(chunks)])

    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    result = await _direct_stream(mixin)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_message_stop_counts_as_terminator(monkeypatch):
    """Anthropic message_stop chunk 是终止证据。"""

    def _parse(event):
        if event.get("type") == "message_stop":
            return [{"type": "message_stop"}]
        return sse_to_chunks(event)

    monkeypatch.setattr(
        httpx, "Client", lambda timeout=None: _ScriptedClient(timeout)
    )
    msg_stop = (
        'data: {"type": "message_stop"}\n\n'.encode()
    )
    _reset_state(behaviors=[_ScriptedResponse([_sse_text("c"), msg_stop])])

    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    provider = SimpleNamespace(
        extract_usage=lambda _e: None,
        parse_stream_chunk=_parse,
    )
    result = await mixin._do_streaming_request(
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
    assert result["status"] == "ok"


# ── 4. httpx 全量四元组 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_httpx_timeout_four_tuple_main_link(monkeypatch):
    """主链路 httpx.Timeout 四项显式齐全：connect=10 / read=idle+30 /
    write=60 / pool=60（read 覆盖「等响应头」阶段）。"""
    captured: dict = {}

    class _CaptureClient:
        def __init__(self, timeout: object = None) -> None:
            captured["timeout"] = timeout

        def stream(self, *args: object, **kwargs: object) -> object:
            return _ScriptedResponse([_sse_text("z"), _SSE_DONE])

        def close(self) -> None:
            pass

    monkeypatch.setattr(httpx, "Client", _CaptureClient)

    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    result = await _direct_stream(mixin)
    assert result["status"] == "ok"

    to = captured["timeout"]
    assert to.read == STREAM_SOCKET_READ_TIMEOUT_S
    assert to.connect == STREAM_SOCKET_CONNECT_TIMEOUT_S == 10.0
    assert to.write == STREAM_SOCKET_WRITE_TIMEOUT_S == 60.0
    assert to.pool == STREAM_SOCKET_POOL_TIMEOUT_S == 60.0


@pytest.mark.asyncio
async def test_httpx_timeout_four_tuple_oneshot(monkeypatch):
    """oneshot 客户端同查：同一常量源的四元组。"""
    import hiveweave.llm.streamer.oneshot as os_

    from hiveweave.llm.streamer.constants import IDLE_TIMEOUT_S

    captured: dict = {}

    class _CaptureClient:
        def __init__(self, timeout: object = None) -> None:
            captured["timeout"] = timeout

        def stream(self, *args: object, **kwargs: object) -> object:
            return _ScriptedResponse([_sse_text("z"), _SSE_DONE])

        def close(self) -> None:
            pass

    monkeypatch.setattr(httpx, "Client", _CaptureClient)

    result = await stream_oneshot(
        _FakeProvider(), [{"role": "user", "content": "hi"}]
    )
    assert result["text"] == "z"

    to = captured["timeout"]
    assert to.read == IDLE_TIMEOUT_S + 30.0
    assert to.connect == STREAM_SOCKET_CONNECT_TIMEOUT_S
    assert to.write == STREAM_SOCKET_WRITE_TIMEOUT_S
    assert to.pool == STREAM_SOCKET_POOL_TIMEOUT_S


# ── 5. llm_retry_scheduled 结构化日志 ────────────────────────


@pytest.mark.asyncio
async def test_llm_retry_scheduled_structured_log_fields():
    """重试被调度 ⇒ 一条 llm_retry_scheduled（attempt/max_retries/delay_ms/
    reason），供 UI/观测接。"""
    mixin = HttpStreamMixin()
    mixin._fire_delta = AsyncMock()
    mixin._retry_handler = SimpleNamespace(max_retries=5)

    cb = mixin._persist_retry("a1", "fake", None)
    with capture_logs() as logs:
        await cb(1, 500, RetryableError("HTTP 429: rate limit", status=429), False)

    entries = [e for e in logs if e.get("event") == "llm_retry_scheduled"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["attempt"] == 1
    assert entry["max_retries"] == 5
    assert entry["delay_ms"] == 500
    assert "429" in entry["reason"]
    assert entry["agent_id"] == "a1"

    # 耗尽（exhausted=True）不打本日志 —— 耗尽走 llm_retry_exhausted 事件
    with capture_logs() as logs2:
        await cb(6, 0, RetryableError("HTTP 429", status=429), True)
    assert not any(
        e.get("event") == "llm_retry_scheduled" for e in logs2
    )


# ── 6. oneshot 同款零输出帽 ──────────────────────────────────


@pytest.mark.asyncio
async def test_oneshot_zero_byte_stream_trips_first_byte_cap(monkeypatch):
    """oneshot 挂死流：零输出总帽到点 ⇒ OneshotFirstByteTotalTimeout
    （timeout_layer="first_byte_total"），总耗时 ≤ cap + 余量。"""
    import hiveweave.llm.streamer.oneshot as os_

    cap = 0.5
    monkeypatch.setattr(os_, "FIRST_BYTE_TOTAL_CAP_S", cap)
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    t0 = time.monotonic()
    with pytest.raises(OneshotFirstByteTotalTimeout) as ei:
        await stream_oneshot(
            _FakeProvider(), [{"role": "user", "content": "hi"}]
        )
    elapsed = time.monotonic() - t0

    assert ei.value.timeout_layer == "first_byte_total"
    assert elapsed <= cap + 2.0


@pytest.mark.asyncio
async def test_oneshot_first_byte_cap_not_retried(monkeypatch):
    """零输出帽判死不可重试（非连接失败类）：attempts == 1。"""
    import hiveweave.llm.streamer.oneshot as os_

    monkeypatch.setattr(os_, "FIRST_BYTE_TOTAL_CAP_S", 0.4)
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    with pytest.raises(OneshotFirstByteTotalTimeout):
        await stream_oneshot_with_retry(
            _FakeProvider(), [{"role": "user", "content": "hi"}]
        )
    assert _STATE["attempts"] == 1


@pytest.mark.asyncio
async def test_oneshot_first_chunk_attribution_preserved_under_default_cap(monkeypatch):
    """默认 cap(300) 远大于压紧的首 chunk 窗：沉默判死仍归 first_chunk
    （既有 P2-2 归因零变化）。"""
    import hiveweave.llm.streamer.oneshot as os_

    from hiveweave.llm.streamer.oneshot import OneshotIdleTimeout

    monkeypatch.setattr(os_, "FIRST_BYTE_TOTAL_CAP_S", 300.0)
    _reset_state(behaviors=[])
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    with pytest.raises(OneshotIdleTimeout) as ei:
        await stream_oneshot(
            _FakeProvider(),
            [{"role": "user", "content": "hi"}],
            first_chunk_timeout_s=0.3,
        )
    assert ei.value.timeout_layer == "first_chunk"


@pytest.mark.asyncio
async def test_oneshot_slow_healthy_stream_outlives_cap(monkeypatch):
    """oneshot 健康慢流：首字节到达后流合法长过零输出帽。"""
    import hiveweave.llm.streamer.oneshot as os_

    cap = 0.5
    monkeypatch.setattr(os_, "FIRST_BYTE_TOTAL_CAP_S", cap)
    n = 20
    chunks = [_sse_text("x") for _ in range(n)] + [_SSE_DONE]
    _reset_state(
        behaviors=[_ScriptedResponse(chunks, interval_s=0.05)]
    )  # ~1.05s > cap
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    result = await stream_oneshot(
        _FakeProvider(), [{"role": "user", "content": "hi"}]
    )
    assert result["text"] == "x" * n


# ── 契约常量 ─────────────────────────────────────────────────


def test_contract_constants():
    """稳定错误码 + 帽为正（错误码是纯字面量，与 env 无关；默认 300 由
    constants.py 源面 `"300" or "300"` 保证并经 OCR 评审，此处不 reload
    模块 —— reload 会让其他模块的 from-import 绑定失效，得不偿失）。"""
    from hiveweave.llm.streamer.constants import (
        FIRST_BYTE_TOTAL_CAP_S,
        FIRST_BYTE_TOTAL_TIMEOUT_ERROR_CODE,
        STREAM_NO_TERMINATOR_ERROR_CODE,
    )

    assert FIRST_BYTE_TOTAL_CAP_S > 0
    assert FIRST_BYTE_TOTAL_TIMEOUT_ERROR_CODE == _FB_CODE
    assert STREAM_NO_TERMINATOR_ERROR_CODE == _NO_TERM_CODE


def test_fb_cap_error_is_not_upstream_death():
    """零输出帽终态不触发 60s 快熔（is_upstream_death 保持 False）。"""
    from hiveweave.llm.retry import is_upstream_death

    err = PermanentError(
        "Upstream produced zero output for 300s total wall clock",
        status=None,
        error_code=_FB_CODE,
    )
    assert is_upstream_death(err) is False
