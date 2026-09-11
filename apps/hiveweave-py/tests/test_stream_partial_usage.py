"""批次 2b「usage 断流保账」：raise 路径已收 usage 不再随异常丢弃。

45 轮 P1：idle 超时/socket 超时/流内 error chunk 的 raise 路径把当轮已收
usage 一起丢掉（budget_cut 返回路径此前已保）——SSL 风暴期这批丢失正好
集中在要归因的重试窗口。改动：_do_streaming_request 的 except 闸口把
partial_usage 挂异常 → _stream_single_round 错误收口带出 → tool_loop
并入 usage_rounds + usage_sink。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.llm.streamer.http_stream import HttpStreamMixin, PermanentError


class _FakeProvider:
    """Minimal ProviderConfig stand-in for streamer tests."""

    provider_type = "fake"
    model_name = "fake-model"
    fallback = None
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


def _run_sync(coro):
    import asyncio

    return asyncio.run(coro)


def _mk_streamer(exc: BaseException):
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(
        return_value=MagicMock(allowed=True, fallback=None)
    )
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    retry_handler = MagicMock()
    retry_handler.with_retry = AsyncMock(side_effect=exc)
    return Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=retry_handler,
    )


@pytest.mark.asyncio
async def test_error_dict_carries_partial_usage():
    """异常对象上的 partial_usage → _stream_single_round 错误结果带出。"""
    exc = PermanentError("Stream idle timeout (75s)")
    exc.partial_usage = {"input": 120, "output": 3, "cache_read": 80}

    streamer = HttpStreamMixin()
    streamer._circuit_breaker = MagicMock()
    streamer._retry_handler = MagicMock()
    streamer._retry_handler.with_retry = AsyncMock(side_effect=exc)
    result = await streamer._stream_single_round(
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
    assert result["partial_usage"] == {
        "input": 120,
        "output": 3,
        "cache_read": 80,
    }


@pytest.mark.asyncio
async def test_no_partial_usage_key_when_absent():
    """无 partial_usage（普通 4xx）→ 错误结果不带该键值（None 即可）。"""
    streamer = HttpStreamMixin()
    streamer._circuit_breaker = MagicMock()
    streamer._retry_handler = MagicMock()
    streamer._retry_handler.with_retry = AsyncMock(
        side_effect=PermanentError("HTTP 400: bad request", status=400)
    )
    result = await streamer._stream_single_round(
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
    assert not result.get("partial_usage")


@pytest.mark.asyncio
async def test_stream_merges_partial_usage_into_rounds_and_sink():
    """端到端：stream() 错误收口把 partial usage 并入 sink。

    L4（2026-09-11）：断言从 `result["usage_rounds"]` 改为 **sink** ——
    result 携带 usage 的通道已退役（两个权威源会分叉），sink 是唯一权威源。
    """
    exc = PermanentError("Stream idle timeout (75s)")
    exc.partial_usage = {"input": 200, "output": 10, "cache_read": 150}
    streamer = _mk_streamer(exc)

    sunk: list[dict] = []
    result = await streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": "fake", "model_id": "m1"},
        tools=None,
        usage_sink=sunk.append,
    )
    assert result["status"] == "error"
    assert "usage_rounds" not in result, (
        "L4：result 不得再携带 usage（sink 是唯一权威源）"
    )
    assert sunk, "partial usage 必须进 sink（此前整轮丢弃）"
    entry = sunk[-1]
    assert entry.get("partial") is True
    # 平台口径：input = 总 prompt 剥离命中后的未命中部分（200-150=50）
    assert entry.get("input") == 50
    assert entry.get("cache_read") == 150
    assert entry.get("ts") > 0
