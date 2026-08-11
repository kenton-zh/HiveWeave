"""llm_oneshot hardening tests: semaphore + timeout/attempts defaults.

Mock style mirrors test_skill_marketplace_routing.py / test_worktree_relocate_binding.py
(FakeClient for httpx.AsyncClient + monkeypatched lazy-imported module attrs).
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import hiveweave.llm.provider as provider_mod
import hiveweave.llm.streamer.constants as streamer_constants
import hiveweave.services.model as model_mod


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._payload = payload or {}
        self._headers = headers or {}

    @property
    def text(self) -> str:
        return str(self._payload.get("error", ""))

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, *responses: _FakeResponse, **init_kwargs: Any) -> None:
        self.responses = list(responses)
        self.init_kwargs: dict[str, Any] = init_kwargs
        self.post_calls: list[tuple[str, dict | None, dict | None]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(
        self, url: str, json: dict | None = None, headers: dict | None = None
    ) -> _FakeResponse:
        self.post_calls.append((url, json, headers))
        idx = min(len(self.post_calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


class _FakeProvider:
    def build_body(self, messages: list[dict], stream: bool = False, temperature: float = 0.3) -> dict:
        return {"messages": messages, "stream": stream}

    def build_url(self) -> str:
        return "https://fake.local/v1/chat/completions"

    def build_headers(self) -> dict:
        return {"Authorization": "Bearer k"}


class _RecordingSemaphore(asyncio.Semaphore):
    def __init__(self) -> None:
        super().__init__(8)
        self.acquired = 0

    async def acquire(self) -> Literal[True]:
        self.acquired += 1
        return await super().acquire()


def _install_env(monkeypatch: pytest.MonkeyPatch, client: _FakeClient, semaphore: asyncio.Semaphore) -> None:
    msvc = MagicMock()
    msvc.resolve_model = AsyncMock(
        return_value={"base_url": "https://fake.local", "api_key": "k", "model_id": "m"}
    )
    factory = MagicMock()
    factory.create.return_value = _FakeProvider()

    monkeypatch.setattr(model_mod, "ModelService", lambda: msvc)
    monkeypatch.setattr(provider_mod, "provider_factory", factory)
    monkeypatch.setattr(streamer_constants, "_get_llm_semaphore", lambda: semaphore)

    def _factory(**kwargs: Any) -> _FakeClient:
        client.init_kwargs = kwargs
        return client

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_semaphore_acquired_around_request(monkeypatch):
    from hiveweave.llm.oneshot import llm_oneshot

    client = _FakeClient(_FakeResponse(200, {"choices": [{"message": {"content": "audit ok"}}]}))
    sem = _RecordingSemaphore()
    _install_env(monkeypatch, client, sem)

    text = await llm_oneshot("proj", "executor", "sys", "user")

    assert text == "audit ok"
    assert sem.acquired == 1


@pytest.mark.asyncio
async def test_default_timeout_single_attempt_on_429(monkeypatch):
    from hiveweave.llm.oneshot import llm_oneshot

    client = _FakeClient(_FakeResponse(429, {"error": "rate limited"}))
    _install_env(monkeypatch, client, asyncio.Semaphore(8))

    text = await llm_oneshot("proj", "executor", "sys", "user")

    assert text is None
    assert len(client.post_calls) == 1
    assert client.init_kwargs["timeout"].read == 60.0


@pytest.mark.asyncio
async def test_explicit_max_attempts_still_retries(monkeypatch):
    from hiveweave.llm.oneshot import llm_oneshot

    client = _FakeClient(
        _FakeResponse(429, {"error": "rate limited"}, {"retry-after-ms": "0"}),
        _FakeResponse(429, {"error": "rate limited"}, {"retry-after-ms": "0"}),
    )
    _install_env(monkeypatch, client, asyncio.Semaphore(8))

    text = await llm_oneshot("proj", "executor", "sys", "user", max_attempts=2)

    assert text is None
    assert len(client.post_calls) == 2
