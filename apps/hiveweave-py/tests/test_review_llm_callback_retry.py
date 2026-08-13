"""review LLM 回调受限重试（预算帽）回归测试。

背景：_review_llm_callback 原为单发 httpx POST 无重试，上游瞬时断连
（RemoteProtocolError "Server disconnected without sending a response"）
导致 review / run_tests 直接失败。修复引入 _review_llm_post_with_retry：
总窗口 45s + 最多额外重试 1 次 + Retry-After 预算检查。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import hiveweave.agents.agent as agent_mod
from hiveweave.llm.retry import RetryableError

URL = "https://gw.fake/v1/chat/completions"
BODY = {"model": "m", "messages": []}
HEADERS = {"Accept": "application/json"}


class FakeResponse:
    """httpx.Response 替身：status / raise_for_status / json / headers。"""

    def __init__(self, status=200, json_data=None, headers=None, text=""):
        self.status_code = status
        self._data = json_data or {}
        self.headers = dict(headers or {})
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", URL)
            response = httpx.Response(
                self.status_code,
                request=request,
                headers=self.headers,
                text=self._text,
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self):
        return self._data


class FakeClient:
    """按 behaviors 顺序应答 post；异常项直接抛出，耗尽后额外调用即失败。"""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.posts: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append(url)
        if not self.behaviors:
            raise AssertionError("unexpected extra post call")
        item = self.behaviors.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(content: str = "review ok") -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_retries_once_after_remote_protocol_error_then_succeeds():
    """RemoteProtocolError → 小退避后重试成功返回内容。"""
    client = FakeClient([
        httpx.RemoteProtocolError("Server disconnected without sending a response"),
        _ok("review ok after retry"),
    ])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        result = await agent_mod._review_llm_post_with_retry(
            URL, BODY, HEADERS, asyncio.Semaphore(1)
        )
    assert result == "review ok after retry"
    assert len(client.posts) == 2
    fake_sleep.assert_awaited_once()
    delay = fake_sleep.await_args.args[0]
    assert 0.5 <= delay <= 1.0


@pytest.mark.asyncio
async def test_continuous_network_failures_raise_after_retries_exhausted():
    """连续网络失败 → 重试耗尽后上抛最后一次异常，不无限循环。"""
    client = FakeClient([
        httpx.RemoteProtocolError("boom 1"),
        httpx.ConnectError("boom 2"),
    ])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        with pytest.raises(httpx.ConnectError):
            await agent_mod._review_llm_post_with_retry(
                URL, BODY, HEADERS, asyncio.Semaphore(1)
            )
    assert len(client.posts) == 2
    assert fake_sleep.await_count == 1


@pytest.mark.asyncio
async def test_429_retry_after_exceeding_budget_gives_up_without_sleep():
    """429 + Retry-After(帽 30s) 超出剩余预算 → 放弃重试直接上抛。"""
    client = FakeClient([
        FakeResponse(
            429,
            json_data={"error": {"message": "rate limited"}},
            headers={"retry-after": "60"},
            text='{"error": {"message": "rate limited"}}',
        ),
        _ok("never reached"),
    ])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        with pytest.raises(RetryableError) as exc_info:
            await agent_mod._review_llm_post_with_retry(
                URL,
                BODY,
                HEADERS,
                asyncio.Semaphore(1),
                retry_window_s=5.0,  # Retry-After 30s >> 剩余预算 → 放弃
            )
    assert exc_info.value.status == 429
    assert len(client.posts) == 1
    fake_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_503_retry_after_within_budget_retries():
    """503 + Retry-After=1s 在预算内 → 按 Retry-After 退避后重试成功。"""
    client = FakeClient([
        FakeResponse(
            503,
            json_data={"error": {"message": "overloaded"}},
            headers={"retry-after": "1"},
            text='{"error": {"message": "overloaded"}}',
        ),
        _ok("recovered"),
    ])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        result = await agent_mod._review_llm_post_with_retry(
            URL, BODY, HEADERS, asyncio.Semaphore(1)
        )
    assert result == "recovered"
    assert len(client.posts) == 2
    fake_sleep.assert_awaited_once()
    assert fake_sleep.await_args.args[0] == 1.0


@pytest.mark.asyncio
async def test_400_permanent_error_no_retry():
    """其他 4xx → 原样上抛 HTTPStatusError，不重试不睡眠。"""
    client = FakeClient([
        FakeResponse(
            400,
            json_data={"error": {"message": "bad request"}},
            text='{"error": {"message": "bad request"}}',
        ),
    ])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await agent_mod._review_llm_post_with_retry(
                URL, BODY, HEADERS, asyncio.Semaphore(1)
            )
    assert len(client.posts) == 1
    fake_sleep.assert_not_awaited()


class BadJsonResponse(FakeResponse):
    def json(self):
        raise json.JSONDecodeError("not json", "doc", 0)


@pytest.mark.asyncio
async def test_content_layer_json_error_no_retry():
    """内容层错误（JSON 解析失败）→ 直接上抛，不重试。"""
    client = FakeClient([BadJsonResponse(200)])
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(asyncio, "sleep", new=AsyncMock()) as fake_sleep,
    ):
        with pytest.raises(json.JSONDecodeError):
            await agent_mod._review_llm_post_with_retry(
                URL, BODY, HEADERS, asyncio.Semaphore(1)
            )
    assert len(client.posts) == 1
    fake_sleep.assert_not_awaited()
