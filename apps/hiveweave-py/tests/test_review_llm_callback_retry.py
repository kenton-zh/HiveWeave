"""review LLM 回调重试语义（批 B 流式化后）回归测试。

背景变迁：_review_llm_callback 原为单发 httpx POST 无重试，上游瞬时断连
（RemoteProtocolError）直接炸掉 review/run_tests → 引入
_review_llm_post_with_retry（预算帽 110s）。批 B（审计 P0·L6）把 oneshot
链路整体流式化（llm/streamer/oneshot），旧函数删除，重试语义**重定义**：

- 重试只对「连接失败 / 立即拒绝」（HTTP 429/5xx、connect 类异常、流未开口）
  生效，最多额外 1 次；429/503 尊重 Retry-After（帽 MAX_DELAY_MS=30s）；
- 退避会顶破剩余总时长预算 → 放弃重试直接上抛（本文件钉住）；
- idle / 首 chunk / 总时长判死与流开口后的死亡一律不重试
  （行为回归在 test_oneshot_stream.py）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import hiveweave.llm.streamer.oneshot as oneshot_mod
from hiveweave.llm.provider import provider_factory
from hiveweave.llm.retry import PermanentError, RetryableError
from hiveweave.llm.streamer.oneshot import stream_oneshot_with_retry

MODEL_CFG = {"base_url": "https://gw.fake/v1", "api_key": "sk-test", "model_id": "m"}


class _ErrResponse:
    """非 200 响应替身（带头），走 stream_oneshot 的 classify 分支。

    不继承 httpx.Response：httpx.Response.__init__ 在无 content 时会内部
    调用 self.read()，覆写的 read 依赖后赋值的属性会炸。
    """

    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_ErrResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        return iter(())


class _OkResponse:
    """200 流式响应替身：一个 text 事件 + DONE。"""

    status_code = 200
    headers: dict = {}

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_OkResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    def close(self) -> None:
        pass


class _FakeClient:
    def __init__(self, behaviors: list) -> None:  # type: ignore[type-arg]
        self._behaviors = behaviors
        self.posts: list[str] = []

    def stream(self, method: str, url: str, headers: object = None,
               content: object = None):  # noqa: ARG002
        self.posts.append(url)
        item = self._behaviors.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_429_retry_after_exceeding_budget_gives_up_without_retry():
    """429 + Retry-After(帽 30s) 超出剩余总时长预算 → 放弃重试直接上抛。"""
    client = _FakeClient([
        _ErrResponse(429, b'{"error":{"message":"rate limited"}}',
                     {"retry-after": "60"}),
        _OkResponse(),
    ])
    with (
        patch("httpx.Client", lambda timeout=None: client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
        pytest.raises(RetryableError) as exc_info,
    ):
        await stream_oneshot_with_retry(
            provider_factory.create(MODEL_CFG),
            [{"role": "user", "content": "x"}],
            agent_id="agent-r",
            total_timeout_s=5.0,  # Retry-After 30s >> 剩余预算 → 放弃
        )
    assert exc_info.value.status == 429
    assert len(client.posts) == 1  # 只尝试了一次
    fake_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_503_retry_after_within_budget_retries():
    """503 + Retry-After=1s 在预算内 → 按 Retry-After 退避后重试成功。"""
    client = _FakeClient([
        _ErrResponse(503, b'{"error":{"message":"overloaded"}}',
                     {"retry-after": "1"}),
        _OkResponse(),
    ])
    with (
        patch("httpx.Client", lambda timeout=None: client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        result = await stream_oneshot_with_retry(
            provider_factory.create(MODEL_CFG),
            [{"role": "user", "content": "x"}],
            agent_id="agent-r",
            total_timeout_s=60.0,
        )
    assert result["text"] == "ok"
    assert len(client.posts) == 2
    fake_sleep.assert_awaited_once()
    assert fake_sleep.await_args.args[0] == 1.0


@pytest.mark.asyncio
async def test_400_permanent_error_no_retry():
    """400 客户端错误 → PermanentError，不重试不睡眠。"""
    client = _FakeClient([
        _ErrResponse(400, b'{"error":{"message":"bad request"}}'),
    ])
    with (
        patch("httpx.Client", lambda timeout=None: client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
        pytest.raises(PermanentError) as exc_info,
    ):
        await stream_oneshot_with_retry(
            provider_factory.create(MODEL_CFG),
            [{"role": "user", "content": "x"}],
            agent_id="agent-r",
        )
    assert exc_info.value.status == 400
    assert len(client.posts) == 1
    fake_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_layer_error_chunk_400_no_retry():
    """HTTP 200 但 body 内包 400 错误 chunk（内容层错误）→ 不重试。"""
    class _ErrorChunkResponse(_OkResponse):
        def iter_bytes(self):  # type: ignore[no-untyped-def]
            yield b'data: {"error":{"message":"bad request","code":"400"}}\n\n'

    client = _FakeClient([_ErrorChunkResponse()])
    with (
        patch("httpx.Client", lambda timeout=None: client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
        pytest.raises((PermanentError, Exception)) as exc_info,
    ):
        await stream_oneshot_with_retry(
            provider_factory.create(MODEL_CFG),
            [{"role": "user", "content": "x"}],
            agent_id="agent-r",
        )
    # 流已开口（error chunk 之前无事件也算「未开口」，但错误 chunk 走
    # classify 分支携带状态/文本判据）——断言只尝试一次即上抛。
    assert len(client.posts) == 1
    fake_sleep.assert_not_awaited()
    assert not isinstance(exc_info.value, oneshot_mod.OneshotIdleTimeout)


@pytest.mark.asyncio
async def test_remote_protocol_error_before_stream_retries():
    """RemoteProtocolError「Server disconnected without sending a response」
    （本重试机制的原始动机，流未开口）→ 小退避后重试成功。"""
    client = _FakeClient([
        httpx.RemoteProtocolError("Server disconnected without sending a response"),
        _OkResponse(),
    ])
    with (
        patch("httpx.Client", lambda timeout=None: client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        result = await stream_oneshot_with_retry(
            provider_factory.create(MODEL_CFG),
            [{"role": "user", "content": "x"}],
            agent_id="agent-r",
        )
    assert result["text"] == "ok"
    assert len(client.posts) == 2
    fake_sleep.assert_awaited_once()
    delay = fake_sleep.await_args.args[0]
    assert 0.5 <= delay <= 1.0
