"""TEST_DSH_63 方案 B+：403 RegionError 确定性上游死亡 → 熔断器快熔。

63 实测：30 秒上游抖动窗（23:14:16-22）内 4 个在跑流被打断 + 2 个新 run
即刻撞墙，各自烧满 300s 重试预算才死。B+ 方案三件事（本文件全部钉住）：

1. 403/RegionError（判据：``llm/retry.is_upstream_death``，跨组契约的
   **唯一判据**，run 级恢复=组4 与 llm 层熔断=组3 共用）首次命中即
   ``CircuitBreaker.open_for`` —— 不等 FAIL_THRESHOLD=5 次累计；
2. 开启期请求在 check() 处秒败，错误文案带 ``UPSTREAM_BREAKER_MARKER``
   + provider 名 + 剩余冷却秒（组4 与日志回归靠这个 token 识别快败）；
3. 冷却（``UPSTREAM_BREAKER_COOLDOWN_S``=60s，快熔单次覆盖可长于管理器
   默认 COOLDOWN_MS）过后 half_open 探针自动放行，探针成功自动 closed
   —— 无需手动复位。

5xx 重试耗尽维持既有 report_failure 语义（推动计数，阈值到即熔断）；
AUTH 401 / 参数 400 等客户端类永久错误**不喂**熔断（一并钉住防回归）。
"""

from __future__ import annotations

import asyncio
import structlog
from unittest.mock import MagicMock

import pytest

from hiveweave.llm.circuit_breaker import (
    UPSTREAM_BREAKER_COOLDOWN_S,
    CircuitBreaker,
    CircuitState,
)
from hiveweave.llm.retry import (
    UPSTREAM_BREAKER_MARKER,
    PermanentError,
    RetryHandler,
    RetryableError,
    is_upstream_death,
)
from hiveweave.llm.streamer.core import Streamer


class _FakeProvider:
    """Minimal ProviderConfig stand-in（对齐 test_no_model_fallback_contract）。"""

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


class RegionError(Exception):
    """模拟异常类名即 RegionError 的形态（判据按类型名命中）。"""


def _mk_streamer(breaker, retry_handler: RetryHandler | None = None):
    provider_factory = MagicMock()
    provider_factory.create = MagicMock(return_value=_FakeProvider())
    return Streamer(
        provider_factory_inst=provider_factory,
        circuit_breaker_inst=breaker,
        retry_handler=retry_handler or RetryHandler(max_retries=0),
    )


async def _stream_once(streamer: Streamer, provider_name: str = "primary") -> dict:
    return await streamer.stream(
        agent_id="a-upstream-death",
        messages=[{"role": "user", "content": "hi"}],
        model_config={"name": provider_name, "model_id": "m1"},
        tools=None,
    )


# ── 跨组契约钉子（组4 对齐用，改这里 = 双方都要评审）─────────────────


def test_contract_pins():
    """契约常量定值：组4 按这两个名字 import，改值必须双方同步。"""
    assert UPSTREAM_BREAKER_MARKER == "upstream_breaker_open"
    assert UPSTREAM_BREAKER_COOLDOWN_S == 60


# ── is_upstream_death 判据表 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        # 1. PermanentError && status==403（classify_http_error 地域 fast-fail 主形态）
        (PermanentError("HTTP 403: RegionError: denied", status=403), True),
        # 非 region 文案的 403 也算（403 即上游地域/权限确定性拒绝）
        (PermanentError("HTTP 403: forbidden", status=403), True),
        # 2. 地域文案族（含 transport_raw 无状态码 PermanentError 形态）
        (PermanentError("RegionError: quota", status=None), True),
        (PermanentError("This model is not available in your country."), True),
        ("RegionError from gateway", True),
        ("model not available in your region", True),
        (RegionError("denied"), True),  # 异常类型名形态
        # 3. 熔断快败标记
        (UPSTREAM_BREAKER_MARKER, True),
        (
            RuntimeError(
                f"Circuit breaker open for provider 'p' "
                f"[{UPSTREAM_BREAKER_MARKER} cooldown_left_s=37]"
            ),
            True,
        ),
        # 4. RetryableError 且 status 为 5xx（重试耗尽后的最终错误）
        (RetryableError("HTTP 503: Service Unavailable", status=503), True),
        (RetryableError("HTTP 500: internal error", status=500), True),
        # ── False 侧：宁窄勿宽 ──
        (RetryableError("HTTP 429: rate limit", status=429), False),
        (RetryableError("First chunk timeout (30s)", status=None), False),
        (RetryableError("Connection error: reset by peer"), False),
        (PermanentError("HTTP 401: unauthorized", status=401), False),
        (PermanentError("HTTP 400: invalid_request_error", status=400), False),
        (PermanentError("HTTP 402: Insufficient Balance", status=402), False),
        (PermanentError("HTTP 404: not found", status=404), False),
        (RuntimeError("unrelated agent bug"), False),
        ("plain network flake", False),
        (None, False),
        ("", False),
    ],
)
def test_is_upstream_death_table(err, expected):
    assert is_upstream_death(err) is expected


# ── open_for 状态机（快熔 + 单次冷却覆盖 + half_open 自动恢复）────────


@pytest.mark.asyncio
async def test_open_for_immediately_opens_and_reports_cooldown():
    breaker = CircuitBreaker()
    await breaker.register("p")
    with structlog.testing.capture_logs() as logs:
        await breaker.open_for("p", cooldown_s=UPSTREAM_BREAKER_COOLDOWN_S)
    assert await breaker.get_state("p") is CircuitState.OPEN
    # 快熔不依赖连续计数：fail_count 保持现状
    assert await breaker.get_fail_count("p") == 0
    # 整秒向下取整，刚 open 时可能已跨过 1 个秒界（60 或 59 都对）
    assert 59 <= breaker.cooldown_left_s("p") <= UPSTREAM_BREAKER_COOLDOWN_S
    snap = {s["provider"]: s for s in breaker.snapshot()}["p"]
    assert snap["state"] == "open"
    assert 0 < snap["cooldown_left_s"] <= UPSTREAM_BREAKER_COOLDOWN_S
    opened = [e for e in logs if e["event"] == "circuit_force_opened"]
    assert opened and opened[0]["provider"] == "p"
    assert opened[0]["cooldown_s"] == UPSTREAM_BREAKER_COOLDOWN_S


@pytest.mark.asyncio
async def test_open_for_auto_registers_unknown_provider():
    breaker = CircuitBreaker()
    await breaker.open_for("ghost")
    assert await breaker.get_state("ghost") is CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_period_check_rejects():
    breaker = CircuitBreaker()
    await breaker.register("p")
    await breaker.open_for("p", cooldown_s=60)
    res = await breaker.check("p")
    assert res.allowed is False
    assert res.fallback is None  # 无 fallback（自动切换已移除）


@pytest.mark.asyncio
async def test_open_for_cooldown_expiry_half_open_probe_recovers():
    breaker = CircuitBreaker(cooldown_ms=50, probe_timeout_ms=5_000)
    await breaker.register("p")
    await breaker.open_for("p", cooldown_s=0.05)
    assert (await breaker.check("p")).allowed is False
    await asyncio.sleep(0.08)
    # 冷却过后 → half_open，放行探针
    assert (await breaker.check("p")).allowed is True
    assert await breaker.get_state("p") is CircuitState.HALF_OPEN
    await breaker.report_success("p")
    assert await breaker.get_state("p") is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_open_for_probe_failure_reopens():
    breaker = CircuitBreaker(cooldown_ms=20, probe_timeout_ms=5_000)
    await breaker.register("p")
    await breaker.open_for("p", cooldown_s=0.02)
    await asyncio.sleep(0.05)
    assert (await breaker.check("p")).allowed is True
    await breaker.report_failure("p", error_code="SERVER")
    assert await breaker.get_state("p") is CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_for_cooldown_overrides_instance_default():
    """快熔的单次冷却覆盖生效：不等管理器级默认（本例 10s）。"""
    breaker = CircuitBreaker(cooldown_ms=10_000, probe_timeout_ms=5_000)
    await breaker.register("p")
    await breaker.open_for("p", cooldown_s=0.05)
    await asyncio.sleep(0.08)
    assert (await breaker.check("p")).allowed is True


@pytest.mark.asyncio
async def test_threshold_open_still_uses_instance_default_cooldown():
    """常规阈值熔断不受快熔覆盖影响（覆盖只在 open_for 调用里给）。"""
    breaker = CircuitBreaker(cooldown_ms=10_000)
    await breaker.register("p")
    for _ in range(breaker.fail_threshold):
        await breaker.report_failure("p")
    assert await breaker.get_state("p") is CircuitState.OPEN
    # 管理器默认 10s 生效（非 60s 快熔值）
    assert breaker.cooldown_left_s("p") > 8


# ── 集成：stream() 全链路（403 → open → 开启期秒败带 marker）──────────


@pytest.mark.asyncio
async def test_region_403_stream_opens_breaker_and_logs(monkeypatch):
    breaker = CircuitBreaker()
    streamer = _mk_streamer(breaker)

    async def fake_stream_req(self, **kwargs):
        raise PermanentError(
            "HTTP 403: RegionError: This model is not available in your region.",
            status=403,
        )

    monkeypatch.setattr(Streamer, "_do_streaming_request", fake_stream_req)
    with structlog.testing.capture_logs() as logs:
        result = await _stream_once(streamer)
    assert result["status"] == "error"
    assert result["error_status"] == 403
    assert await breaker.get_state("primary") is CircuitState.OPEN
    opened = [e for e in logs if e["event"] == "upstream_breaker_opened"]
    assert opened, [e["event"] for e in logs]
    assert opened[0]["provider"] == "primary"
    assert opened[0]["cooldown_s"] == UPSTREAM_BREAKER_COOLDOWN_S


@pytest.mark.asyncio
async def test_open_period_stream_fast_fails_with_marker(monkeypatch):
    """开启期 stream() 在 check() 即被拒：503 秒败，文案带 marker+provider+剩余秒。"""
    breaker = CircuitBreaker()
    await breaker.register("primary")
    await breaker.open_for("primary", cooldown_s=UPSTREAM_BREAKER_COOLDOWN_S)
    streamer = _mk_streamer(breaker)

    called = False

    async def fake_stream_req(self, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("开启期不得真的发 HTTP 请求")

    monkeypatch.setattr(Streamer, "_do_streaming_request", fake_stream_req)
    result = await _stream_once(streamer)
    assert called is False
    assert result["status"] == "error"
    assert result["error_status"] == 503
    assert UPSTREAM_BREAKER_MARKER in result["error"]
    assert "'primary'" in result["error"]
    marker_pos = result["error"].rfind("cooldown_left_s=")
    assert marker_pos != -1
    left = int(result["error"][marker_pos + len("cooldown_left_s="):].rstrip("]"))
    assert 0 < left <= UPSTREAM_BREAKER_COOLDOWN_S


@pytest.mark.asyncio
async def test_auth_401_stream_does_not_trip_breaker(monkeypatch):
    """AUTH/参数类永久错误维持「不喂熔断」旁路（B+ 只收编上游死亡类）。"""
    breaker = CircuitBreaker(fail_threshold=1)
    streamer = _mk_streamer(breaker)

    async def fake_stream_req(self, **kwargs):
        raise PermanentError("HTTP 401: unauthorized", status=401)

    monkeypatch.setattr(Streamer, "_do_streaming_request", fake_stream_req)
    result = await _stream_once(streamer)
    assert result["status"] == "error"
    assert result["error_status"] == 401
    assert await breaker.get_state("primary") is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_5xx_retry_exhaustion_feeds_breaker_count(monkeypatch):
    """503 重试耗尽走既有 report_failure 语义：推动计数，阈值到即熔断。"""
    breaker = CircuitBreaker(fail_threshold=1)
    streamer = _mk_streamer(breaker, RetryHandler(max_retries=0))

    async def fake_stream_req(self, **kwargs):
        raise RetryableError(
            "HTTP 503: Service Unavailable", status=503, headers={},
            error_code="SERVER",
        )

    monkeypatch.setattr(Streamer, "_do_streaming_request", fake_stream_req)
    result = await _stream_once(streamer)
    assert result["status"] == "error"
    assert result["error_status"] == 503
    assert await breaker.get_state("primary") is CircuitState.OPEN
    snap = {s["provider"]: s for s in breaker.snapshot()}["primary"]
    assert snap["last_error_code"] == "SERVER"
