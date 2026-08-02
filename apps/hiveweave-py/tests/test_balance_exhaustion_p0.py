"""TEST19 P0-2 regression: HTTP 402 must trigger a global wake-stop.

TEST19 实测：10 个 error run 全部 402 Insufficient Balance，每个 agent
各自撞满 4 次才 give up，watchdog 还把 escalation 投给同样已死的收件人
→ 2 条永久未读。修复后：402 立即全局停唤醒 + 通知用户；429 仍走
项目级降速（语义不同，402 重试必败）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.agents.helpers.rate_limit import (
    balance_exhausted_remaining,
    broadcast_balance_exhausted,
    is_balance_error,
)
from hiveweave.llm.retry import PermanentError, RetryableError


# ── is_balance_error 判定 ──────────────────────────────────────


def test_is_balance_error_402_permanent():
    assert is_balance_error(PermanentError("HTTP 402: Insufficient Balance", status=402)) is True


def test_is_balance_error_message_needles():
    for msg in (
        "insufficient balance",
        "Insufficient Balance",
        "HTTP 402 payment required",
        "balance exhausted",
        "http 402: account empty",
        "status 402 from gateway",
    ):
        assert is_balance_error(ValueError(msg)) is True, msg


def test_is_balance_error_bare_402_not_enough():
    """Bare '402' substring must NOT arm the global breaker (ports / codes)."""
    for msg in (
        "connection refused on port 4021",
        "error code 1402",
        "HTTP 2402 weird",
        "timeout after 402ms",
        "402",
        "error 4021",
        "http 402ms timeout",
    ):
        assert is_balance_error(ValueError(msg)) is False, msg


def test_is_balance_error_429_is_not_balance():
    assert is_balance_error(RetryableError("HTTP 429: rate limit", status=429)) is False
    assert is_balance_error(ValueError("too many requests")) is False
    # OpenAI 429 insufficient_quota 消息含 "billing" 但语义是配额 429，
    # 不是余额耗尽 —— 绝不能触发全局 402 熔断（复审 P1-1）。
    assert is_balance_error(ValueError(
        "You exceeded your current quota, please check your plan and billing details."
    )) is False


def test_is_balance_error_none():
    assert is_balance_error(None) is False


# ── 全局熔断状态 ───────────────────────────────────────────────


def test_balance_remaining_starts_clear():
    assert balance_exhausted_remaining() == 0.0


def test_broadcast_balance_exhausted_arms_and_cools_peers():
    """广播：arm 全局熔断 + 所有 peer 进 resume cooldown。"""
    from hiveweave.agents.helpers import rate_limit as rl

    peer = MagicMock()
    peer.id = "peer-1"
    peer.project_id = "p1"
    mgr = MagicMock()
    mgr.list_all.return_value = [peer]
    try:
        with patch(
            "hiveweave.agents.supervisor.agent_manager", mgr
        ):
            cooled = broadcast_balance_exhausted(
                duration_s=120.0, source_agent_id="self-1"
            )
        assert cooled == 1
        peer._arm_resume_cooldown.assert_called_once()
        assert balance_exhausted_remaining() > 0
    finally:
        rl._balance_exhausted_until = 0.0


def test_balance_remaining_expires():
    """到期自动解除。"""
    from hiveweave.agents.helpers import rate_limit as rl
    import time

    rl._balance_exhausted_until = time.monotonic() - 1.0
    assert balance_exhausted_remaining() == 0.0
    assert rl._balance_exhausted_until == 0.0


def test_clear_balance_exhausted():
    from hiveweave.agents.helpers import rate_limit as rl
    from hiveweave.agents.helpers.rate_limit import clear_balance_exhausted

    peer = MagicMock()
    peer.id = "peer-1"
    peer._resume_cooldown_until = rl.time.monotonic() + 999.0
    mgr = MagicMock()
    mgr.list_all.return_value = [peer]

    rl._balance_exhausted_until = rl.time.monotonic() + 999.0
    with patch("hiveweave.agents.supervisor.agent_manager", mgr):
        cleared = clear_balance_exhausted()
    assert cleared == 1
    assert peer._resume_cooldown_until == 0.0
    assert balance_exhausted_remaining() == 0.0


# ── agent 入口挡 402 ──────────────────────────────────────────


def _arm_balance(seconds: float) -> None:
    from hiveweave.agents.helpers import rate_limit as rl

    rl._balance_exhausted_until = rl.time.monotonic() + seconds


def test_in_resume_cooldown_honors_balance_breaker():
    """402 熔断期间，_in_resume_cooldown 返回 True（watcher 不会唤醒）。"""
    from hiveweave.agents.helpers import rate_limit as rl
    from hiveweave.agents.agent import Agent

    agent = Agent.__new__(Agent)
    agent._resume_cooldown_until = 0.0
    agent.project_id = "p1"

    _arm_balance(100.0)
    try:
        assert agent._in_resume_cooldown() is True
    finally:
        rl._balance_exhausted_until = 0.0
    assert agent._in_resume_cooldown() is False


def test_balance_breaker_independent_of_project_rate_limit():
    """402 全局熔断不依赖项目级 429 状态。"""
    from hiveweave.agents.helpers import rate_limit as rl

    rl._project_rate_limit_until.pop("p1", None)
    _arm_balance(100.0)
    try:
        assert balance_exhausted_remaining() > 0
    finally:
        rl._balance_exhausted_until = 0.0
    assert balance_exhausted_remaining() == 0.0


# ── http_stream PermanentError 保留 error_status ───────────────


def test_stream_error_dict_carries_status():
    """PermanentError 分支必须保留 error_status（agent 层才能识别 402）。"""
    from hiveweave.llm.streamer.http_stream import HttpStreamMixin

    streamer = HttpStreamMixin()
    streamer._circuit_breaker = MagicMock()
    streamer._retry_handler = MagicMock()
    streamer._retry_handler.with_retry = AsyncMock(
        side_effect=PermanentError("HTTP 402: Insufficient Balance", status=402)
    )
    result = _run_sync(streamer._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
    ))
    assert result["status"] == "error"
    assert result["error_status"] == 402
    assert "402" in result["error"]


def test_stream_error_dict_no_status_for_plain_permanent():
    from hiveweave.llm.streamer.http_stream import HttpStreamMixin

    streamer = HttpStreamMixin()
    streamer._circuit_breaker = MagicMock()
    streamer._retry_handler = MagicMock()
    streamer._retry_handler.with_retry = AsyncMock(
        side_effect=PermanentError("HTTP 400: bad request", status=400)
    )
    result = _run_sync(streamer._stream_single_round(
        agent_id="a1",
        provider=_FakeProvider(),
        provider_name="fake",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        on_delta=None,
        round_num=1,
        delta_id="d1",
    ))
    assert result["status"] == "error"
    assert result["error_status"] == 400


def _run_sync(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


# ── stream() 全链路透传 error_status ──────────────────────────


def test_stream_passes_error_status_through():
    """端到端：error_status 必须穿透 stream() → _run_tool_loop →
    _stream_with_empty_retry → _stream_single_round（TEST19 审计 P0：
    字段曾在 tool_loop 重建 dict 时丢失，agent 层 402 分支成为死代码）。"""
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=MagicMock(allowed=True, fallback=None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    retry_handler = MagicMock()
    retry_handler.with_retry = AsyncMock(
        side_effect=PermanentError("HTTP 402: Insufficient Balance", status=402)
    )
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=retry_handler,
    )
    result = _run_sync(streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": "fake", "model_id": "m1"},
        tools=None,
    ))
    assert result["status"] == "error"
    assert result["error_status"] == 402
    assert "402" in result["error"]


def test_stream_preserves_non_balance_error_status():
    """非 402 的 PermanentError 也透传自身 status（400）。"""
    from hiveweave.llm.streamer.core import Streamer

    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=MagicMock(allowed=True, fallback=None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    retry_handler = MagicMock()
    retry_handler.with_retry = AsyncMock(
        side_effect=PermanentError("HTTP 400: bad request", status=400)
    )
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=retry_handler,
    )
    result = _run_sync(streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": "fake", "model_id": "m1"},
        tools=None,
    ))
    assert result["status"] == "error"
    assert result["error_status"] == 400


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

    def build_headers(self) -> dict:
        return {}

    def build_body(self, messages=None, stream=True, tools=None) -> dict:
        return {"messages": messages or [], "stream": stream}
