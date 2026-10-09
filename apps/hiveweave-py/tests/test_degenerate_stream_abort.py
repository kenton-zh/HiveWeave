"""N3 批28-2 施工1 接线级回归：退化检测器 ⇒ 批28-1 abort 管道。

复用批28-1（test_abort_stream_pipeline）的传输替身基建：模拟流 delta
序列（健康暖机 → 退化形态），断言：
  ⑤ 流内命中 ⇒ 请求立即以 aborted 分类收口（reason=degenerate_output:loop
    / tag_leak），不进重试、注册表注销；
  ⑤' env 总开关关闭 ⇒ 同一条退化流完整跑完（零干预）；
  ⑤'' tool_loop 收口说明：degenerate reason ⇒ [STREAM ABORTED] 附带
    「输出退化…请直接给出精简结论」的 steering 文案（非 degenerate 的
    abort 文案不受影响）。
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
    active_abort_sources,
    reset_abort_registry,
)
from hiveweave.llm.streamer.http_stream import HttpStreamMixin
from hiveweave.llm.retry import RetryHandler
from hiveweave.llm.streamer.sse import sse_to_chunks


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
    """按给定字节序列 yield 的 200 响应替身。"""

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


async def _do_stream(mixin: HttpStreamMixin, agent_id: str = "d1"):
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


def _healthy_warmup_sse(n: int = 110) -> list[bytes]:
    """健康暖机 delta（≥4k、内容有变化）—— 本身不得触发检测。"""
    lines = [
        f"note {i}: module_{i} checked, state {i % 7}, next {i * 3 % 11}"
        for i in range(n)
    ]
    return [_sse_text(line + "\n") for line in lines]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    import hiveweave.llm.streamer.http_stream as hs
    import hiveweave.llm.streamer.oneshot as os_

    monkeypatch.setattr(hs, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))
    monkeypatch.setattr(os_, "_get_llm_semaphore", lambda: asyncio.Semaphore(8))
    monkeypatch.delenv("HIVEWEAVE_DEGENERATE_DETECT", raising=False)
    reset_abort_registry()
    yield
    reset_abort_registry()
    current = _STATE.get("current")
    if current is not None and hasattr(current, "close"):
        current.close()


# ── ⑤ 流内退化命中 ⇒ abort 管道掐流 ─────────────────────────────────


async def test_degenerate_loop_triggers_abort_mid_stream(monkeypatch):
    """暖机后进入「Hmm. 」循环 ⇒ 本请求以 aborted 收口
    （finish_reason=aborted、reason=degenerate_output:loop、单次请求不重试）。"""
    chunks = _healthy_warmup_sse()
    chunks += [_sse_text("Hmm. " * 25) for _ in range(40)]  # 40 片 ×125 字符
    chunks += [_sse_finish("stop"), _SSE_DONE]  # 正常收尾（不该走到）
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    t0 = time.monotonic()
    result = await _do_stream(_mk_mixin(), agent_id="d1")

    assert result["status"] == "ok"
    assert result["aborted"] is True
    assert result["finish_reason"] == "aborted"
    assert result["abort_reason"] == "degenerate_output:loop"
    # 掐在烧满之前：暖机全收 + 少量循环片，远小于全流
    assert 4000 < len(result["text"]) < 4000 + 125 * 40
    assert result["tool_calls"] == []
    assert _STATE["attempts"] == 1  # aborted 不进退避重试
    assert active_abort_sources() == {}  # 请求结束注销
    assert time.monotonic() - t0 < 5.0


async def test_degenerate_tag_leak_triggers_abort_mid_stream(monkeypatch):
    """正文连吐 </longcat_tool_call> 标签原文 ⇒ aborted +
    reason=degenerate_output:tag_leak。"""
    chunks = _healthy_warmup_sse()
    chunks += [
        _sse_text("先给结论。</longcat_tool_call> 继续"),
        _sse_text("再补一段。</longcat_tool_call> 收尾"),
        _sse_finish("stop"),
        _SSE_DONE,
    ]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    result = await _do_stream(_mk_mixin(), agent_id="d2")

    assert result["aborted"] is True
    assert result["abort_reason"] == "degenerate_output:tag_leak"
    assert "</longcat_tool_call>" in result["text"]  # 已收文本保留
    assert _STATE["attempts"] == 1
    assert active_abort_sources() == {}


async def test_degenerate_abort_uses_registry_with_run_id(monkeypatch):
    """接线走注册表入口：检测器命中时置位的是本 run 的 abort 源
    （run_id 匹配 —— 批28-1 陈旧判废语义照常可用）。"""
    from hiveweave.llm.streamer.abort_source import register_abort_source

    chunks = _healthy_warmup_sse()
    chunks += [_sse_text("Hmm. " * 25) for _ in range(40)]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    mixin = _mk_mixin()
    mixin._run_id = "run-live-282"
    result = await _do_stream(mixin, agent_id="d3")
    assert result["aborted"] is True
    assert result["abort_reason"] == "degenerate_output:loop"
    # 注册表在请求开始时按 _run_id 登记（批28-1 语义），请求结束注销
    assert active_abort_sources() == {}


# ── ⑤' env 关闭 ⇒ 零干预（同一条退化流完整跑完）────────────────────


async def test_env_disabled_degenerate_stream_completes(monkeypatch):
    """HIVEWEAVE_DEGENERATE_DETECT=0：退化流不掐，完整收口 stop/正常文本。"""
    monkeypatch.setenv("HIVEWEAVE_DEGENERATE_DETECT", "0")
    chunks = _healthy_warmup_sse()
    chunks += [_sse_text("Hmm. " * 25) for _ in range(60)]
    chunks += [_sse_finish("stop"), _SSE_DONE]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    result = await _do_stream(_mk_mixin(), agent_id="d4")

    assert result["status"] == "ok"
    assert "aborted" not in result
    assert result["finish_reason"] == "stop"
    assert result["text"].endswith("Hmm. " * 25)
    assert _STATE["attempts"] == 1


async def test_healthy_stream_untouched_by_detector(monkeypatch):
    """健康长流（暖机后继续有变化的内容）全程零干预。"""
    chunks = _healthy_warmup_sse(160)
    chunks += [_sse_text("结论：全部模块核对完毕，收工。\n")]
    chunks += [_sse_finish("stop"), _SSE_DONE]
    _reset_state(_ScriptedResponse(chunks))
    monkeypatch.setattr(httpx, "Client", _ScriptedClient)

    result = await _do_stream(_mk_mixin(), agent_id="d5")

    assert result["status"] == "ok"
    assert "aborted" not in result
    assert result["finish_reason"] == "stop"
    assert result["text"].startswith("note 0:")


# ── ⑤'' tool_loop 收口说明：degenerate 专属 steering 文案 ───────────


class _FakeLoopProvider(_FakeProvider):
    provider_type = "fake"
    supports_thinking = False
    fallback = None
    max_output_tokens = 4096
    context_window = 128_000


def _mk_streamer():
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeLoopProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(
        return_value=MagicMock(allowed=True, fallback=None)
    )
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


async def _run_tool_loop_with_abort_reason(reason: str) -> dict:
    streamer = _mk_streamer()

    async def fake_stream(**kwargs):
        return {
            "status": "ok",
            "text": "被掐前的部分输出",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": "aborted",
            "usage": None,
            "aborted": True,
            "abort_reason": reason,
        }

    streamer._stream_with_empty_retry = fake_stream  # type: ignore[method-assign]
    return await streamer._run_tool_loop(
        agent_id="d6",
        provider=_FakeLoopProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        on_tool_call=AsyncMock(),
        max_tool_rounds=10,
    )


async def test_tool_loop_degenerate_abort_note_steers_concise():
    """degenerate_output:loop 的 abort ⇒ [STREAM ABORTED] 附「输出退化
    （短语循环）已掐断——请直接给出精简结论」steering（spec 文案）。"""
    result = await _run_tool_loop_with_abort_reason("degenerate_output:loop")
    assert result["aborted"] is True
    assert result["abort_reason"] == "degenerate_output:loop"
    assert result["steering_reason"] == "stream_aborted"
    assert "[STREAM ABORTED]" in result["content"]
    assert "输出退化" in result["content"]
    assert "短语循环" in result["content"]
    assert "请直接给出精简结论" in result["content"]
    assert "被掐前的部分输出" in result["content"]  # 产出保留


async def test_tool_loop_degenerate_tag_leak_note_kind():
    result = await _run_tool_loop_with_abort_reason("degenerate_output:tag_leak")
    assert "工具标签泄漏" in result["content"]
    assert "请直接给出精简结论" in result["content"]


async def test_tool_loop_non_degenerate_abort_note_unchanged():
    """非退化 abort（lease_expired 等）：文案保持批28-1 原状，不带退化
    steering —— 分支互不污染。"""
    result = await _run_tool_loop_with_abort_reason("lease_expired")
    assert "[STREAM ABORTED]" in result["content"]
    assert "lease_expired" in result["content"]
    assert "输出退化" not in result["content"]
    assert "请直接给出精简结论" not in result["content"]
