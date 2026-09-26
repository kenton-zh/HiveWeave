"""批 G P1-7③：跨 run 续暖 cache-warmer —— 决策数学与生命周期。

决策数学**逐字照搬** pi `packages/coding-agent/src/core/cache-warmer.ts`
（HEAD 2b0a123de），本文件参数表直接对齐上游常量断言：

- ``getCacheWarmingDelayMs``（pi :29-32）："Refresh at 90% of the TTL
  while preserving at least ten seconds of margin."
- ``evaluate``（pi :378-400）：expectedSavings = p×missCost − warmCost，
  阈值 $0.05；idle 续跑概率 0.15（pi :21-26）。
- 双安全上限 1h/30min（pi :12-18）。

不做真实网络调用：``_send_warm_request`` 在生命周期测试中被替身。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from hiveweave.config import settings
from hiveweave.services import cache_warmer as cw
from hiveweave.services.cache_warmer import (
    CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS,
    DEFAULT_PRICES,
    IDLE_CONTINUATION_PROBABILITY,
    MAX_IDLE_WARMING_AGE_MS,
    MAX_WARMING_AGE_MS,
    CacheWarmer,
    compute_warm_decision,
    estimate_prompt_cache_ttl_ms,
    get_cache_warming_delay_ms,
)

MODEL = {
    "base_url": "https://gw.example/v1",
    "api_key": "k",
    "model_id": "m",
    "provider_type": "openai-compatible",
    "context_window": 128_000,
    "max_output_tokens": 8_192,
}

MESSAGES = [
    {"role": "system", "content": "identity"},
    {"role": "user", "content": "go"},
]
TOOLS = [{"type": "function", "function": {"name": "read_file"}}]


# ── TTL×90% 触发延迟（pi :29-32 参数表）────────────────────────


def test_delay_is_90pct_of_ttl_with_margin():
    # 5min TTL → 270s（90%，且 < 290s 余量线）
    assert get_cache_warming_delay_ms(5 * 60_000) == 270_000
    # 1h TTL → 54min
    assert get_cache_warming_delay_ms(60 * 60_000) == 3_240_000
    # 10.5s TTL：90%=9.45s > 余量线 0.5s ⇒ 取余量线
    assert get_cache_warming_delay_ms(10_500) == 500
    # 20s TTL：90%=18s vs 余量线 10s ⇒ 取 10s
    assert get_cache_warming_delay_ms(20_000) == 10_000


def test_delay_undefined_for_tiny_ttl():
    """TTL ≤10s ⇒ None（pi："if (ttlMs <= 10_000) return undefined"）。"""
    assert get_cache_warming_delay_ms(10_000) is None
    assert get_cache_warming_delay_ms(1_000) is None


# ── TTL 层级（pi getPromptCacheTtlMs 的协议适配版）──────────────


def test_ttl_anthropic_follows_long_ttl_switch(monkeypatch):
    monkeypatch.setattr(settings, "cache_control_long_ttl", True)
    assert estimate_prompt_cache_ttl_ms("anthropic") == 60 * 60_000
    monkeypatch.setattr(settings, "cache_control_long_ttl", False)
    assert estimate_prompt_cache_ttl_ms("anthropic") == 5 * 60_000


def test_ttl_openai_family_conservative_5min():
    for fmt in ("openai", "openai-compatible", "openai-responses"):
        assert estimate_prompt_cache_ttl_ms(fmt) == 5 * 60_000, fmt


def test_ttl_unknown_protocol_is_none():
    """无证据的协议 ⇒ None ⇒ 不续暖（pi "cache lifetime unavailable"）。"""
    assert estimate_prompt_cache_ttl_ms("google") is None
    assert estimate_prompt_cache_ttl_ms(None) is None
    assert estimate_prompt_cache_ttl_ms("") is None


# ── warm-or-stop 决策数学（pi evaluate :378-400）────────────────


def test_decision_constants_match_upstream():
    """常量必须与上游逐字一致（照搬纪律，改动 = 偏离 pi 机制）。"""
    assert CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS == 0.05
    assert IDLE_CONTINUATION_PROBABILITY == 0.15
    assert MAX_WARMING_AGE_MS == 60 * 60_000
    assert MAX_IDLE_WARMING_AGE_MS == 30 * 60_000


def test_decision_streaming_phase_warms_at_100k():
    """streaming 阶段 p=1：10 万 token 前缀 ⇒ warm。

    hand-computed（Sonnet 档价）：hit=0.03, miss=0.345,
    warm=0.030015, expected=0.314985 ≥ 0.05。
    """
    d = compute_warm_decision(100_000, phase="streaming")
    assert d["continuation_probability"] == 1.0
    assert d["action"] == "warm"
    assert d["economics_available"] is True
    assert round(d["expected_savings"], 6) == 0.314985


def test_decision_idle_phase_probability_gates_small_prompts():
    """idle 阶段 p=0.15：同一 10 万 token 前缀 ⇒ stop。

    hand-computed：expected = 0.15×0.345 − 0.030015 = 0.021735 < 0.05。
    阈值守住「idle 续跑概率低时不白烧续暖请求」——上游设计意图。
    """
    d = compute_warm_decision(100_000, phase="idle")
    assert d["continuation_probability"] == IDLE_CONTINUATION_PROBABILITY
    assert d["action"] == "stop"


def test_decision_idle_phase_warms_huge_prefix():
    """idle 阶段 30 万 token ⇒ warm（0.15×1.035 − 0.090015 = 0.065235）。"""
    d = compute_warm_decision(300_000, phase="idle")
    assert d["action"] == "warm"
    assert round(d["expected_savings"], 6) == 0.065235


def test_decision_zero_prompt_is_not_economical():
    """promptTokens=0 ⇒ economics 不可用 ⇒ stop（pi :389 同判）。"""
    d = compute_warm_decision(0, phase="streaming")
    assert d["economics_available"] is False
    assert d["action"] == "stop"


def test_decision_write_price_falls_back_to_input():
    """cache_write=0 的价表 ⇒ miss 按 input 价（pi :384 同回退）。"""
    prices = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 0.0}
    d = compute_warm_decision(100_000, prices, phase="streaming")
    # missCost = 100000 × (3.0/1e6 − 0.30/1e6) = 0.27
    assert round(d["miss_cost"], 6) == 0.27
    assert d["action"] == "warm"


def test_decision_custom_prices_override_defaults():
    d = compute_warm_decision(
        1_000_000,
        {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
        phase="idle",
    )
    assert round(d["miss_cost"], 6) == 1.0
    assert round(d["warm_cost"], 6) == 0.0
    assert round(d["expected_savings"], 6) == 0.15


def test_default_prices_documented():
    """默认价表必须存在且为 Anthropic Sonnet 档（TODO 语义锚）。"""
    assert DEFAULT_PRICES["input"] == 3.0
    assert DEFAULT_PRICES["cache_write"] == 3.75
    assert DEFAULT_PRICES["cache_read"] == 0.30
    assert DEFAULT_PRICES["output"] == 15.0


# ── 生命周期：observe → settled(armed) → 发送 → cancel ───────────


@pytest.fixture()
def fresh_warmer():
    w = CacheWarmer()
    # 隔离单例状态（测试不碰全局 cache_warmer 实例）
    return w


def _fast_ttl(monkeypatch):
    """openai 系 TTL=10.5s ⇒ 延迟 0.5s（测试可等）；其他协议走真实推档
    （保住 google→None / anthropic→1h 的协议区分，勿整函数替换）。"""
    real = cw.estimate_prompt_cache_ttl_ms
    monkeypatch.setattr(
        cw,
        "estimate_prompt_cache_ttl_ms",
        lambda fmt: (
            10_500 if str(fmt or "").startswith("openai") else real(fmt)
        ),
    )
    monkeypatch.setattr(settings, "cache_warmer_enabled", True)


@pytest.mark.asyncio
async def test_armed_warmer_sends_and_reschedules_until_cancel(fresh_warmer, monkeypatch):
    _fast_ttl(monkeypatch)
    sends: list[int] = []

    async def fake_send(run):
        sends.append(run.prompt_tokens)

    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        assert "a1" in fresh_warmer._runs
        await asyncio.sleep(1.3)
        assert sends, "到点必须发续暖请求（TTL×90%）"
        n_after_first_window = len(sends)
        fresh_warmer.cancel("a1")
        await asyncio.sleep(1.0)
        assert len(sends) == n_after_first_window, "cancel 后不得再发"
        assert "a1" not in fresh_warmer._runs


@pytest.mark.asyncio
async def test_new_activity_replaces_armed_run(fresh_warmer, monkeypatch):
    """再次武装替换旧武装（pi start "replaces any previous run"）。"""
    _fast_ttl(monkeypatch)

    async def fake_send(run):
        return None

    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        first = fresh_warmer._runs.get("a1")
        fresh_warmer.on_agent_settled("a1")
        second = fresh_warmer._runs.get("a1")
        assert first is not second, "重武装必须替换旧 run"
        await asyncio.sleep(0.05)
        assert first.task.done() or first.task.cancelled(), "旧任务被取消"
        fresh_warmer.cancel("a1")


@pytest.mark.asyncio
async def test_disabled_switch_never_arms(fresh_warmer, monkeypatch):
    _fast_ttl(monkeypatch)
    monkeypatch.setattr(settings, "cache_warmer_enabled", False)
    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    fresh_warmer.on_agent_settled("a1")
    assert "a1" not in fresh_warmer._runs


@pytest.mark.asyncio
async def test_economics_below_threshold_never_sends(fresh_warmer, monkeypatch):
    """小前缀（idle 概率门槛下）⇒ armed 但决策 stop，不发请求。"""
    _fast_ttl(monkeypatch)
    sends: list[int] = []

    async def fake_send(run):
        sends.append(run.prompt_tokens)

    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=50_000
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        await asyncio.sleep(1.0)
        assert not sends, "预期省钱 < $0.05 ⇒ 不得发送"
        assert "a1" not in fresh_warmer._runs, "决策 stop 后停止续暖"


@pytest.mark.asyncio
async def test_stale_snapshot_not_armed(fresh_warmer, monkeypatch):
    """快照过旧（>15min）⇒ 不武装（本仓适配卫兵，防 _go_idle 空转暖）。"""
    _fast_ttl(monkeypatch)
    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    # 人为把快照拨老
    import time as _time

    fresh_warmer._latest["a1"]["ts"] = _time.monotonic() - 16 * 60
    fresh_warmer.on_agent_settled("a1")
    assert "a1" not in fresh_warmer._runs


@pytest.mark.asyncio
async def test_anthropic_thinking_model_not_armed(fresh_warmer, monkeypatch):
    """anthropic + thinking ⇒ 重放不安全（pi isReplayable），不武装。"""
    _fast_ttl(monkeypatch)
    thinking_model = {
        **MODEL,
        "provider_type": "anthropic",
        "supports_thinking": True,
    }
    fresh_warmer.observe_request(
        "a1", "p1", thinking_model, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    fresh_warmer.on_agent_settled("a1")
    assert "a1" not in fresh_warmer._runs


@pytest.mark.asyncio
async def test_unknown_protocol_not_armed(fresh_warmer, monkeypatch):
    """google（无 TTL 证据）⇒ 不武装。"""
    _fast_ttl(monkeypatch)
    google_model = {**MODEL, "provider_type": "google"}
    fresh_warmer.observe_request(
        "a1", "p1", google_model, MESSAGES, TOOLS, prompt_tokens=300_000
    )
    fresh_warmer.on_agent_settled("a1")
    assert "a1" not in fresh_warmer._runs


def test_cancel_all_and_cancel_unknown_agent_are_safe(fresh_warmer):
    fresh_warmer.cancel("nonexistent")  # 不 raise
    fresh_warmer.cancel_all()
    fresh_warmer.cancel_all()  # 幂等


# ── P1-1（审计 2026-09-26）：delay > idle 上限 ⇒ 不武装（armed 即死修复）──


@pytest.mark.asyncio
async def test_long_ttl_delay_exceeding_idle_horizon_not_armed(fresh_warmer):
    """默认 long_ttl=True ⇒ anthropic TTL=1h ⇒ delay=54min > 30min idle 上限
    ⇒ 必须在武装前 skip（旧实现 armed 后首轮 age-limit 即死，零发射 +
    遥测误导）。5min 档不受影响。"""
    # anthropic 走真实推档：long_ttl 默认开 ⇒ 1h
    fresh_warmer.observe_request(
        "a-horizon", "p1",
        {**MODEL, "provider_type": "anthropic",
         "model_id": "claude-sonnet-4-5",
         "base_url": "https://api.anthropic.com"},
        MESSAGES, TOOLS, prompt_tokens=300_000,
    )
    fresh_warmer.on_agent_settled("a-horizon")
    assert "a-horizon" not in fresh_warmer._runs, (
        "1h 档 delay=54min 超过 idle 上限 ⇒ 不得武装（armed 即死是 P1-1 病灶）"
    )


@pytest.mark.asyncio
async def test_five_min_ttl_still_armed_under_horizon(fresh_warmer, monkeypatch):
    """5min 档（openai 系 / 关长 TTL 的 anthropic）delay=4.5min ≤ 30min ⇒ 正常武装。"""
    _fast_ttl(monkeypatch)
    fresh_warmer.observe_request(
        "a-ok", "p1", MODEL, MESSAGES, TOOLS, prompt_tokens=300_000,
    )
    fresh_warmer.on_agent_settled("a-ok")
    assert "a-ok" in fresh_warmer._runs
    fresh_warmer.cancel("a-ok")


# ── P1-3（审计 2026-09-26）：续暖体复刻主链路 CONTINUE_SENTINEL 哨兵规则 ──


class _FakeHttp:
    """捕获 post(url, json=, headers=) 的最小 httpx.AsyncClient 替身。"""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})

        class _Resp:
            status_code = 200

            def json(self):
                return self._d

            _d = self.response

        return _Resp()


def _make_run(agent_id: str, messages: list[dict]) -> cw._WarmRun:
    import time as _time

    return cw._WarmRun(
        agent_id=agent_id,
        project_id="p1",
        model_config=MODEL,
        messages=messages,
        tools=TOOLS,
        prompt_tokens=300_000,
        ttl_ms=10_500,
        delay_ms=500,
        started_at=_time.monotonic(),
    )


@pytest.mark.asyncio
async def test_warm_body_replicates_continue_sentinel(fresh_warmer):
    """快照尾条非 user（tool 结果，tool loop 原地 append 的常态）⇒ 续暖体
    末条必须是 user 哨兵 —— 与 http_stream 发请求前的形状逐字一致；快照
    本身不得被改写。"""
    from hiveweave.llm.streamer.constants import CONTINUE_SENTINEL

    snapshot = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "doing"},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]
    run = _make_run("a-sentinel", snapshot)
    fake = _FakeHttp({"usage": {"prompt_tokens": 100, "completion_tokens": 1}})
    with patch("httpx.AsyncClient", return_value=fake):
        await fresh_warmer._send_warm_request(run)
    body = fake.posts[0]["json"]
    sent = body["messages"]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"] == CONTINUE_SENTINEL, (
        "线上末断点条目键含哨兵块，重放不带 ⇒ 命中只到首请求条目、尾部全价"
    )
    assert sent[:-1] == snapshot
    # 快照钉住的形状不被改写（哨兵只进请求副本）
    assert run.messages[-1]["role"] == "tool"
    assert len(run.messages) == len(snapshot)


@pytest.mark.asyncio
async def test_warm_body_no_sentinel_when_tail_is_user(fresh_warmer):
    """尾条本就是 user ⇒ 不加哨兵（与 http_stream 同一条规则的正反两面）。"""
    snapshot = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "fresh turn"},
    ]
    run = _make_run("a-user-tail", snapshot)
    fake = _FakeHttp({"usage": {"prompt_tokens": 100, "completion_tokens": 1}})
    with patch("httpx.AsyncClient", return_value=fake):
        await fresh_warmer._send_warm_request(run)
    body = fake.posts[0]["json"]
    assert body["messages"] == snapshot


# ── P1-2（审计 2026-09-26）：anthropic 续暖命中的 cache_read 记账 ──────


@pytest.mark.asyncio
async def test_warm_usage_maps_anthropic_cache_read(fresh_warmer):
    """anthropic 响应的 ``cache_read_input_tokens`` 必须落进记账的
    cache_read —— 漏映射 ⇒ 续暖命中记成 0（warm 成本账全错）。"""
    from unittest.mock import AsyncMock

    from hiveweave.services.token_meter import token_meter

    anthropic_model = {
        **MODEL,
        "provider_type": "anthropic",
        "model_id": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com",
    }
    run = cw._WarmRun(
        agent_id="a-usage",
        project_id="p1",
        model_config=anthropic_model,
        messages=list(MESSAGES),
        tools=TOOLS,
        prompt_tokens=300_000,
        ttl_ms=10_500,
        delay_ms=500,
        started_at=0.0,
    )
    fake = _FakeHttp({
        "content": [{"type": "text", "text": "ok"}],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 1,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 120,
        },
    })
    captured: list[list[dict]] = []

    async def _capture(agent_id, project_id, rounds, **kwargs):
        captured.append(rounds)

    with patch("httpx.AsyncClient", return_value=fake), patch.object(
        token_meter, "record_rounds", side_effect=_capture
    ):
        await fresh_warmer._send_warm_request(run)
    assert captured, "续暖 usage 必须落 token meter（request_type=cache_warm）"
    rnd = captured[0][0]
    assert rnd["cache_read"] == 800, (
        f"anthropic 命中必须映射进 cache_read，实得 {rnd['cache_read']}"
    )
    assert rnd["cache_creation"] == 120
    assert rnd["input"] == 1000
