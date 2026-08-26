"""自动模型/provider 切换已移除后的契约（对标 DSH，2026-08-26）。

原 E6 复盘为「熔断时无 fallback」加了三条通道：熔断递归切换、Agent 同 tier
failover、Vision 主备切换。这些已全部删除，理由：

1. 换 model = 换缓存域 —— provider 前缀缓存整条作废。TEST_DSH_29 实测
   1736 次请求命中率 86.26%，其中 228 次零命中烧掉 90% 的全价 input token。
2. 切换在无核算的情况下静默改变模型身份，把故障归因到错误的模型上。
3. 实测该项目 fallback 触发 0 次（主备同 API key 被 same-key 守卫全挡），
   即这套机制的复杂度从未换来收益。

可重试错误由 llm/retry.py 在 HTTP 层处理（退避 + Retry-After）；重试耗尽后
交由 park / 容量治理，不再偷换模型。本测试锁定"不切换"这个契约。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hiveweave.llm.circuit_breaker import CheckResult
from hiveweave.llm.streamer.core import Streamer


class _FakeProvider:
    """Minimal ProviderConfig stand-in（对齐 test_balance_exhaustion_p0）。"""

    provider_type = "fake"
    model_name = "fake-model"
    max_output_tokens = 4096
    supports_thinking = True
    context_window = 128_000

    def build_url(self) -> str:
        return "http://fake"

    def build_headers(self) -> dict:
        return {}

    def build_body(self, messages=None, stream=True, tools=None) -> dict:
        return {"messages": messages or [], "stream": stream}


def _run_sync(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mk_streamer(check_result):
    provider_factory = MagicMock()
    provider_factory.create = MagicMock(return_value=_FakeProvider())
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=check_result)
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
    )
    return streamer, provider_factory, breaker


def test_breaker_open_returns_503_without_switching():
    """熔断打开 → 503 error result，不抛异常、不切换模型。"""
    streamer, factory, _ = _mk_streamer(CheckResult.fallback_to(None))
    result = _run_sync(
        streamer.stream(
            agent_id="a1",
            messages=[{"role": "user", "content": "hi"}],
            model_config={"name": "primary", "model_id": "m1"},
            tools=None,
        )
    )
    assert result["status"] == "error"
    assert result["error_status"] == 503
    assert factory.create.call_count == 1


def test_breaker_open_ignores_configured_fallback():
    """即使熔断器仍带 fallback 名，也不再递归切换（字段已成惰性遗留）。"""
    streamer, factory, breaker = _mk_streamer(CheckResult.fallback_to("backup-model"))
    result = _run_sync(
        streamer.stream(
            agent_id="a1",
            messages=[{"role": "user", "content": "hi"}],
            model_config={"name": "A", "model_id": "m1", "tier": "executor"},
            tools=None,
        )
    )
    assert result["status"] == "error"
    assert result["error_status"] == 503
    # 关键断言：不因 fallback 名存在而二次 create（旧行为会是 2）
    assert factory.create.call_count == 1
    assert breaker.check.await_count == 1


def test_register_called_without_fallback_arg():
    """register 不再注入 fallback —— 自动切换来源被断在注册期。"""
    streamer, _, breaker = _mk_streamer(CheckResult.ok())
    try:
        _run_sync(
            asyncio.wait_for(
                streamer.stream(
                    agent_id="a1",
                    messages=[{"role": "user", "content": "hi"}],
                    model_config={"name": "A", "model_id": "m1", "tier": "executor"},
                    tools=None,
                ),
                timeout=1.0,
            )
        )
    except Exception:
        pass
    assert breaker.register.await_count >= 1
    for call in breaker.register.await_args_list:
        assert "fallback" not in call.kwargs
        assert len(call.args) == 1


def test_agent_has_no_failover_resolver():
    """Agent 层同 tier failover 已删除（方法不复存在）。"""
    from hiveweave.agents.agent import Agent

    assert not hasattr(Agent, "_resolve_failover_backup")


def test_streamer_has_no_fallback_resolver():
    """Streamer 的 fallback 名推导已删除。"""
    assert not hasattr(Streamer, "_resolve_fallback_name")


def test_vision_has_no_backup_helper():
    """Vision 主备切换已删除。"""
    from hiveweave.tools import vision_tools

    assert not hasattr(vision_tools, "_try_vision_backup")
