"""E6 复盘验收：熔断 fallback 覆盖（tier 备份推导 + 不裸抛 + 防环）。

复盘致命链二：熔断时 muse-spark-1.2 / ox-alpha-free 报 "no fallback available"。
根因：熔断器 fallback 仅依赖模型行手填 ``fallback`` 字段（两模型均为空），
而同 tier 备份机制只在 agent 层可重试分支可达——breaker-open 裸抛异常不带
err_status → is_retryable=False → 同 tier failover 永不触发。

本测试锁定修复：
- _resolve_fallback_name：模型行 fallback 为空时从同 tier 备份推导
  （skip 当前 + same-key 守卫 + tried 防环）。
- breaker-open 无有效 fallback → 返回 503 error result（不裸抛），
  agent 层可重试 → 既有同 tier failover 通道接管。
- A→B→A 环在递归层终止（不 RecursionError）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.llm.circuit_breaker import CheckResult
from hiveweave.llm.streamer.core import Streamer


class _FakeProvider:
    """Minimal ProviderConfig stand-in（对齐 test_balance_exhaustion_p0）。"""

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


def _run_sync(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── _resolve_fallback_name（fallback 覆盖推导）─────────────────


def test_fallback_uses_configured_row_fallback():
    """E6-①模型行显式 fallback 优先，且不绕过 tried 防环。"""
    provider = _FakeProvider()
    provider.fallback = "row-backup"
    config = {"name": "A", "tier": "executor", "api_key": "k", "id": "u"}
    assert _run_sync(Streamer._resolve_fallback_name(provider, config, set())) == "row-backup"
    # 已在 tried（递归防环内）→ 不再返回
    assert _run_sync(
        Streamer._resolve_fallback_name(provider, config, {"row-backup"})
    ) is None


def test_fallback_derives_tier_backup():
    """E6-②模型行 fallback 为空 → 从同 tier 备份推导（skip 当前模型）。"""
    provider = _FakeProvider()  # fallback=None
    config = {
        "name": "A",
        "model_id": "m1",
        "tier": "executor",
        "api_key": "key-A",
        "id": "uuid-A",
    }
    with patch("hiveweave.services.model.ModelService") as MS:
        MS.return_value.resolve_model = AsyncMock(
            return_value={"name": "B", "api_key": "key-B"}
        )
        fb = _run_sync(
            Streamer._resolve_fallback_name(provider, config, set())
        )
    assert fb == "B"
    MS.return_value.resolve_model.assert_awaited_once_with(
        tier="executor", skip_model_ids={"uuid-A"}
    )


def test_fallback_skips_same_key_backup():
    """E6-③同 key 备份（共享配额）→ 不推导，切换无意义。"""
    provider = _FakeProvider()
    config = {
        "name": "A",
        "tier": "executor",
        "api_key": "shared-key-1",
        "id": "uuid-A",
    }
    with patch("hiveweave.services.model.ModelService") as MS:
        MS.return_value.resolve_model = AsyncMock(
            return_value={"name": "B", "api_key": "shared-key-1"}
        )
        fb = _run_sync(Streamer._resolve_fallback_name(provider, config, set()))
    assert fb is None


def test_fallback_none_without_tier_config():
    """无 tier 且无手填 fallback → None（维持「no fallback available」语义）。"""
    provider = _FakeProvider()
    config = {"name": "A", "model_id": "m1"}
    assert _run_sync(Streamer._resolve_fallback_name(provider, config, set())) is None


# ── stream() 行为：breaker-open 不裸抛 ─────────────────────────


def test_breaker_open_no_fallback_returns_503_error_result():
    """E6-④无有效 fallback → 返回 503 error result，不抛异常。"""
    provider_factory = MagicMock()
    provider_factory.create.return_value = _FakeProvider()
    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=CheckResult.fallback_to(None))
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()
    streamer = Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
    )
    result = _run_sync(streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": "primary", "model_id": "m1"},  # 无 tier → 无推导
        tools=None,
    ))
    assert result["status"] == "error"
    assert result["error_status"] == 503
    assert "no fallback available" in result["error"]


def test_breaker_open_with_fallback_recurses_once():
    """E6-⑤有有效 fallback → 递归切到备份（并携带 tried 防环）。"""
    rows = {"A": {"fallback": "B"}, "B": {"fallback": None}}

    def _mk_provider(config):
        p = _FakeProvider()
        p.fallback = rows.get(config.get("name"), {}).get("fallback")
        return p

    provider_factory = MagicMock()
    provider_factory.create = MagicMock(side_effect=_mk_provider)

    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(side_effect=[
        CheckResult.fallback_to("B"),  # check(A) → 切 B
        CheckResult.ok(),              # check(B) → 放行（B 未熔断）
    ])
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()

    config_b = {"name": "B", "model_id": "m2", "tier": "executor", "is_active": True}

    async def _fake_get(name):
        return config_b if name == "B" else None

    with patch("hiveweave.services.model.ModelService") as MS:
        MS.return_value.get = AsyncMock(side_effect=_fake_get)
        MS.return_value.resolve_model = AsyncMock(return_value=None)
        streamer = Streamer(
            provider_factory_inst=provider_factory,
            circuit_breaker_inst=breaker,
        )
        # 递归路径会走 _run_tool_loop —— 这里只需确认递归进入二次 create(B)
        # 并最终被 breaker 放行后的流程接管；用 run 超时防悬挂。
        import asyncio as _a

        with pytest.raises(_a.TimeoutError):
            _run_sync(_a.wait_for(streamer.stream(
                agent_id="a1",
                messages=[{"role": "user", "content": "hi"}],
                model_config={"name": "A", "model_id": "m1", "tier": "executor"},
                tools=None,
            ), timeout=1.0))
    # 递归确实发生（A 和 B 都被 create 过），且没在 fallback 层死循环
    assert provider_factory.create.call_count == 2


def test_breaker_open_fallback_loop_terminates():
    """E6-⑥A→B→A 环：B 的 fallback 指向已尝试的 A → 停，不无限递归。"""
    rows = {"A": {"fallback": "B"}, "B": {"fallback": "A"}}

    def _mk_provider(config):
        p = _FakeProvider()
        p.fallback = rows.get(config.get("name"), {}).get("fallback")
        return p

    provider_factory = MagicMock()
    provider_factory.create = MagicMock(side_effect=_mk_provider)

    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(side_effect=[
        CheckResult.fallback_to("B"),  # check(A)
        CheckResult.fallback_to("A"),  # check(B) → A 已在 tried → 停
    ])
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()

    async def _fake_get(name):
        return {
            "A": {"name": "A", "model_id": "m1", "tier": "executor", "is_active": True},
            "B": {"name": "B", "model_id": "m2", "tier": "executor", "is_active": True},
        }[name]

    with patch("hiveweave.services.model.ModelService") as MS:
        MS.return_value.get = AsyncMock(side_effect=_fake_get)
        MS.return_value.resolve_model = AsyncMock(return_value=None)
        streamer = Streamer(
            provider_factory_inst=provider_factory,
            circuit_breaker_inst=breaker,
        )
        result = _run_sync(streamer.stream(
            agent_id="a1",
            messages=[{"role": "user", "content": "hi"}],
            model_config={"name": "A", "model_id": "m1", "tier": "executor"},
            tools=None,
        ))
    # 递归两次（A→B）后停止，B 的 fallback A 被 tried 拦截 → 503 error result
    assert provider_factory.create.call_count == 2
    assert result["status"] == "error"
    assert result["error_status"] == 503