"""T33 波次感知重试 + T34 多 provider 故障转移（2026-09-29 设计更新版）。

实测病灶（80 三案 + 84 两案，同根因）：上游 longcat 免费档 500 波次——
单波持续 1.5-3 分钟、波间隔 10-20 分钟；旧重试梯 6 连退避总窗 ~90s ⇒
持续波次必杀 run（error 终态）。

T33（波次级骑乘，对照 DSH ``mode:'always'`` retryPolicy）：
- 主链路重试默认**无界**（10-15s 稳态节奏 + 短指数 ramp），靠成功 /
  取消信号 / deadline / 故障转移闸终止；
- Retry-After ≤60s 原样尊重、>60s 忽略；45s 保留为单次退避帽（env 可调）；
- 瞬态成功重置 failover 连败计数（80 病例：第二次波次按新梯算）；
- 先落账后等待（persist 先于退避睡眠，进程重启不丢）；
- 4xx（除 408/429）仍 PermanentError 不重试。

T34（故障转移）：同一 agent 同一 run 内连续 ≥3 次上游级故障（5xx/零分块
超时/连接失败，复用 RetryableError 分类）⇒ 从模型池选非当前模型接管
（优先同网关异模型，次同方言异网关；open_DS_V 等禁用名单跳过）；
备用也连败 ⇒ 链上下一个候选，池走完按现状 error 收口；转移本回合临时
生效（实例态，不写 DB），下回合恢复主模型。

可抄①（opencode message-v2.ts）：失败 turn 的 [ERROR] 标记消息不进下轮
模型上下文（get_history 过滤面收窄到 LLM 读路径）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

import hiveweave.llm.retry as retry_mod
from hiveweave.llm.failover import RunFailover, failover_threshold, skip_models
from hiveweave.llm.retry import (
    MAX_DELAY_MS,
    PermanentError,
    RetryAborted,
    RetryHandler,
    RetryableError,
    compute_backoff,
    main_ride_budget,
    ride_wall_cap_s,
)
from hiveweave.llm.streamer.core import Streamer

# 模块导入期捕获真实退避计算（_tiny_backoff fixture 之后用于恢复）
_REAL_COMPUTE_BACKOFF = retry_mod.compute_backoff


# ── 工具 ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """骑乘退避睡眠快速化（节奏语义另由 compute_backoff 单测钉住）。"""
    real_sleep = asyncio.sleep

    async def _fast(delay, *a, **kw):
        await real_sleep(min(float(delay), 0.002))

    monkeypatch.setattr(asyncio, "sleep", _fast)


@pytest.fixture(autouse=True)
def _tiny_backoff(monkeypatch):
    """骑乘退避计算快速化（1ms；延迟值语义由 compute_backoff 单测钉住）。"""
    monkeypatch.setattr(retry_mod, "compute_backoff", lambda *a, **k: 1)


def _mk_pool(*models: dict) -> AsyncMock:
    return AsyncMock(return_value=list(models))


def _model(
    uid: str,
    *,
    name: str = "",
    model_id: str = "m1",
    base_url: str = "https://gw.test/v1",
    dialect: str = "openai-compatible",
    is_active: bool = True,
) -> dict:
    return {
        "id": uid,
        "name": name or f"name-{uid}",
        "model_id": model_id,
        "base_url": base_url,
        "api_key": f"key-{uid}",
        "provider_type": dialect,
        "is_active": is_active,
    }


class _FakeProvider:
    """ProviderConfig 替身（_run_round_chain 的构建面）。"""

    def __init__(
        self, model_name: str = "fake", base_url: str = "https://gw.test/v1"
    ):
        self.model_name = model_name
        self.base_url = base_url
        self.provider_type = "openai-compatible"

    def build_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_headers(self, session_id: str | None = None) -> dict:
        return {}

    def build_body(self, **kwargs) -> dict:
        return {"model": self.model_name}

    def extract_usage(self, event) -> None:
        return None

    def parse_stream_chunk(self, event) -> list:
        return []


def _inbox_cls(sink: list):
    """InboxService 替身类工厂（patch 替换的是类本身，实例化后记录投递）。"""

    class _Svc:
        async def send_message(self, **kw):
            sink.append(kw)
            return {}

    return _Svc


# ── T33：波次级无界骑乘 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_riding_survives_sustained_500s():
    """长波次 mock：连续 500 覆盖远超旧梯预算 ⇒ 骑乘存活、run 不死。

    旧梯（MAX_RETRIES=5）在第 6 次失败即耗尽；无界骑乘骑过 12 连败。
    """
    handler = RetryHandler(max_retries=None)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] <= 12:
            raise RetryableError("HTTP 500: upstream wave", status=500)
        return {"ok": True}

    result = await handler.with_retry(fn)
    assert result == {"ok": True}
    assert calls["n"] == 13  # 12 败 + 1 成


@pytest.mark.asyncio
async def test_ride_terminated_by_deadline_raises_last_error():
    """deadline 到点 ⇒ 停止骑乘并抛最后一次 RetryableError（交既有
    error → durable recovery 路收口），exhausted 落账。"""
    persisted: list[tuple] = []
    handler = RetryHandler(max_retries=None)

    async def fn():
        raise RetryableError("HTTP 503: down", status=503)

    async def persist(*a):
        persisted.append(a)

    with pytest.raises(RetryableError):
        await handler.with_retry(
            fn,
            persist=persist,
            deadline=time.monotonic() - 1,  # 已过期
        )
    assert persisted and persisted[0][3] is True  # exhausted=True 落账


@pytest.mark.asyncio
async def test_ride_terminated_by_task_cancel():
    """用户停止 / 会话取消 ⇒ CancelledError 自然传播（预算外终止信号）。"""
    handler = RetryHandler(max_retries=None)

    async def fn():
        raise RetryableError("HTTP 500: wave", status=500)

    async def rider():
        await handler.with_retry(fn)

    task = asyncio.create_task(rider())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_4xx_still_permanent_no_retry():
    """红线：4xx 客户端错误（除 408/429）仍 PermanentError 不重试。"""
    handler = RetryHandler(max_retries=None)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise PermanentError("HTTP 400: bad request", status=400)

    with pytest.raises(PermanentError):
        await handler.with_retry(fn)
    assert calls["n"] == 1  # 零重试


def test_retry_after_honored_up_to_60s_ignored_beyond():
    """Retry-After ≤60s 原样尊重；>60s 忽略（落回稳态骑乘节奏）。"""
    assert compute_backoff(7, 50_000) == 50_000
    assert compute_backoff(7, 0) == 0
    beyond = compute_backoff(7, 120_000)
    assert beyond <= 15_000  # 稳态节奏，不是 120s


def test_steady_cadence_and_single_backoff_cap():
    """短 ramp（1/2/4/8）后进入 10-15s 稳态；本地值受 45s 单次帽封顶。"""
    for attempt in range(4):
        assert compute_backoff(attempt) <= 8_000 * 1.25 + 1
    for _ in range(20):
        assert 10_000 <= compute_backoff(4) <= 15_000
        assert 10_000 <= compute_backoff(50) <= 15_000
    assert MAX_DELAY_MS == 45_000
    assert compute_backoff(0) <= MAX_DELAY_MS


def test_env_overrides_ladder_params(monkeypatch):
    """env 清单：HIVEWEAVE_LLM_RETRY_MAX_DELAY_S / _RETRIES / 墙钟可调。"""
    monkeypatch.setenv("HIVEWEAVE_LLM_RETRY_MAX_DELAY_S", "1")
    assert compute_backoff(0) <= 1_000  # 帽收窄生效

    monkeypatch.setenv("HIVEWEAVE_LLM_RETRY_MAX_DELAY_S", "bogus")
    assert retry_mod.retry_max_delay_ms() == 45_000  # 非法回退默认
    monkeypatch.delenv("HIVEWEAVE_LLM_RETRY_MAX_DELAY_S")

    monkeypatch.setenv("HIVEWEAVE_LLM_RETRY_MAX_RETRIES", "4")
    assert main_ride_budget() == 4  # 有界紧急刹车
    monkeypatch.setenv("HIVEWEAVE_LLM_RETRY_MAX_RETRIES", "0")
    assert main_ride_budget() is None  # ≤0 = 无界
    monkeypatch.delenv("HIVEWEAVE_LLM_RETRY_MAX_RETRIES")
    assert main_ride_budget() is None  # 默认无界骑乘

    assert ride_wall_cap_s() == 900.0  # 骑乘兜底墙默认
    monkeypatch.setenv("HIVEWEAVE_LLM_RIDE_WALL_CAP_S", "60")
    assert ride_wall_cap_s() == 60.0


@pytest.mark.asyncio
async def test_persist_ledger_before_wait(monkeypatch):
    """可抄③（DSH 先落账后等待）：持久化先于退避睡眠，重启不丢。"""
    order: list[str] = []
    monkeypatch.setattr(retry_mod, "compute_backoff", _REAL_COMPUTE_BACKOFF)

    real_sleep = asyncio.sleep

    async def _recording_sleep(delay, *a, **kw):
        order.append(f"sleep:{delay}")
        await real_sleep(min(float(delay), 0.002))

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)

    handler = RetryHandler(max_retries=None)

    async def persist(attempt, delay_ms, exc, exhausted):
        order.append(f"persist:{attempt}:{exhausted}")

    async def fn():
        if len(order) < 2:
            raise RetryableError("HTTP 500: once", status=500)
        return "ok"

    assert await handler.with_retry(fn, persist=persist) == "ok"

    assert any(o.startswith("persist:1:False") for o in order)
    first_sleep_idx = next(
        (i for i, o in enumerate(order) if o.startswith("sleep:")), -1
    )
    first_persist_idx = next(
        (i for i, o in enumerate(order) if o.startswith("persist:")), -1
    )
    assert 0 <= first_persist_idx < first_sleep_idx


# ── 可抄③②：重试计数持久化（llm_retry 事件 + failover_streak 随行）──


@pytest.mark.asyncio
async def test_persist_retry_writes_llm_retry_event_with_streak():
    """验收项 4：llm_retry 事件持久化（重启后事件流仍可审计），
    payload 携带 failover_streak（T34 连败数与重试事件同窗可审计）；
    耗尽走 llm_retry_exhausted 事件。"""
    streamer = Streamer(
        provider_factory_inst=MagicMock(),
        circuit_breaker_inst=MagicMock(),
        retry_handler=RetryHandler(max_retries=None),
    )
    persisted: list[dict] = []

    class _FakeAudit:
        async def log(self, **kw):
            persisted.append(kw)

    streamer._failover_state().observe_failure()  # streak = 1

    with patch("hiveweave.services.event_audit.event_audit", _FakeAudit()):
        cb = streamer._persist_retry("a1", "longcat", None)
        await cb(3, 12_000, RetryableError("HTTP 500: wave", status=500), False)
        await cb(4, 0, RetryableError("HTTP 500: wave", status=500), True)

    assert len(persisted) == 2
    first = persisted[0]
    assert first["event_type"] == "llm_retry"
    assert first["agent_id"] == "a1"
    payload = first["payload"]
    assert payload["attempt"] == 3
    assert payload["delay_ms"] == 12_000
    assert payload["provider"] == "longcat"
    assert "500" in payload["error"]
    assert payload["failover_streak"] == 1
    # 耗尽落 llm_retry_exhausted（exhausted=True 路径）
    assert persisted[1]["event_type"] == "llm_retry_exhausted"
    assert persisted[1]["payload"]["failover_streak"] == 1


# ── T33.2：瞬态成功重置连败计数 ─────────────────────────────


@pytest.mark.asyncio
async def test_transient_success_resets_failover_streak():
    """80 病例：波次 1 的单发 500 被吸收、中间成功 ⇒ 波次 2 按新梯算。

    连败 2 → 成功（归零）→ 连败 2 ⇒ 未达阈值，不转移。
    """
    fo = RunFailover()
    fo.bind_primary(_model("uuid-A"))
    pool = _mk_pool(_model("uuid-A"), _model("uuid-B"))

    for _ in range(2):
        fo.observe_failure()
    fo.observe_success()  # attempt-1 吸收后成功
    for _ in range(2):
        fo.observe_failure()

    with patch("hiveweave.services.model.ModelService.list_active_full", pool):
        switched = await fo.maybe_switch(agent_id="a1", from_model="A")
    assert switched is None  # 2 连败 < 3，不转移


# ── T34：故障转移 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_failover_after_3_consecutive_upstream_failures():
    """主模型连续 3 次上游故障 ⇒ 切换日志 + notice + 备用模型接管。"""
    fo = RunFailover()
    primary = _model("uuid-A", model_id="longcat")
    backup = _model("uuid-B", model_id="ark")
    fo.bind_primary(primary)

    notices: list[dict] = []
    pool = _mk_pool(primary, backup)
    with (
        patch("hiveweave.services.model.ModelService.list_active_full", pool),
        patch("hiveweave.services.inbox.InboxService", _inbox_cls(notices)),
        structlog.testing.capture_logs() as logs,
    ):
        # 无界骑乘下的闸节奏：3 连败 → 闸触发转移
        for _ in range(3):
            fo.observe_failure()
        switched = await fo.maybe_switch(agent_id="a1", from_model="longcat")

    assert switched is not None and switched["id"] == "uuid-B"
    assert fo.active_config is not None and fo.active_config["id"] == "uuid-B"
    assert fo.streak == 0  # 新模型新梯
    assert "uuid-B" in fo.tried_ids
    # 结构化日志 llm_failover_switched（from/to model）
    events = [e for e in logs if e.get("event") == "llm_failover_switched"]
    assert events and events[0]["to_model"] == "name-uuid-B"
    assert events[0]["from_model"] == "longcat"
    # 诚实原则：platform_notice 到 agent，wake=False（不额外造唤醒）
    assert len(notices) == 1
    n = notices[0]
    assert n["message_type"] == "platform_notice"
    assert n["wake"] is False
    assert n["trusted_platform"] is True
    assert "主模型连续故障" in n["message"] and "已切换" in n["message"]
    assert "name-uuid-B" in n["message"]


@pytest.mark.asyncio
async def test_failover_chain_walks_pool_without_revisit():
    """循环防护：转移链最多走完池内可用模型数，不回头；走完按现状收口。"""
    fo = RunFailover()
    a = _model("uuid-A", base_url="https://gw.test/v1")
    b = _model("uuid-B", base_url="https://gw.test/v1")
    c = _model("uuid-C", base_url="https://gw2.test/v1")
    fo.bind_primary(a)

    pool = _mk_pool(a, b, c)
    with patch("hiveweave.services.model.ModelService.list_active_full", pool):
        # A 连败 3 → B（同网关优先）
        for _ in range(3):
            fo.observe_failure()
        first = await fo.maybe_switch(agent_id="a1")
        assert first["id"] == "uuid-B"
        # B 连败 3 → C（同方言异网关）
        for _ in range(3):
            fo.observe_failure()
        second = await fo.maybe_switch(agent_id="a1")
        assert second["id"] == "uuid-C"
        # C 连败 3 → 池走完 ⇒ None（继续骑当前梯，按现状 error 收口）
        for _ in range(3):
            fo.observe_failure()
        third = await fo.maybe_switch(agent_id="a1")
        assert third is None
        assert fo.active_config["id"] == "uuid-C"  # 不回头


@pytest.mark.asyncio
async def test_skip_models_and_env_override():
    """禁用模型跳过（open_DS_V 默认名单）；env 设置时整体覆盖默认。"""
    assert "open_ds_v" in skip_models()

    fo = RunFailover()
    primary = _model("uuid-A")
    ds_v = _model("uuid-V", name="open_DS_V", model_id="open_DS_V")
    good = _model("uuid-B")
    fo.bind_primary(primary)

    with patch(
        "hiveweave.services.model.ModelService.list_active_full",
        _mk_pool(primary, ds_v, good),
    ):
        for _ in range(3):
            fo.observe_failure()
        switched = await fo.maybe_switch(agent_id="a1")
    assert switched["id"] == "uuid-B"  # 跳过 open_DS_V

    # env 覆盖默认：open_DS_V 变为可选，good 进入名单
    fo2 = RunFailover()
    fo2.bind_primary(primary)
    with (
        patch(
            "hiveweave.services.model.ModelService.list_active_full",
            _mk_pool(primary, ds_v, good),
        ),
        patch.dict(
            "os.environ", {"HIVEWEAVE_LLM_FAILOVER_SKIP_MODELS": "name-uuid-b"}
        ),
    ):
        for _ in range(3):
            fo2.observe_failure()
        switched2 = await fo2.maybe_switch(agent_id="a1")
    assert switched2["id"] == "uuid-V"  # 名单已换（good 被新名单跳过）


@pytest.mark.asyncio
async def test_cross_dialect_never_picked():
    """方言安全约束：跨 provider_type 不作候选（usage/思考方言错位）。"""
    fo = RunFailover()
    primary = _model("uuid-A", dialect="openai-compatible")
    anthro = _model("uuid-C", dialect="anthropic")
    same = _model("uuid-B", dialect="openai-compatible")
    fo.bind_primary(primary)

    with patch(
        "hiveweave.services.model.ModelService.list_active_full",
        _mk_pool(primary, anthro),
    ):
        for _ in range(3):
            fo.observe_failure()
        assert await fo.maybe_switch(agent_id="a1") is None  # 只有异方言

    fo2 = RunFailover()
    fo2.bind_primary(primary)
    with patch(
        "hiveweave.services.model.ModelService.list_active_full",
        _mk_pool(primary, anthro, same),
    ):
        for _ in range(3):
            fo2.observe_failure()
        switched = await fo2.maybe_switch(agent_id="a1")
    assert switched["id"] == "uuid-B"  # 同方言候选胜出


@pytest.mark.asyncio
async def test_streamer_failover_switch_then_backup_fails_to_error():
    """链路集成：主模型 3 连败 → 换备用接管；备用 PermanentError ⇒
    按现状 error 收口（归一化 result，不无限换）。"""
    primary = _model("uuid-A", model_id="longcat")
    backup = _model("uuid-B", model_id="ark")
    current = {"cfg": primary}

    class _FakeFactory:
        def create(self, cfg):
            current["cfg"] = cfg
            return _FakeProvider(model_name=str(cfg.get("model_id") or ""))

    notices: list[dict] = []

    streamer = Streamer(
        provider_factory_inst=_FakeFactory(),
        circuit_breaker_inst=MagicMock(
            report_success=AsyncMock(),
            report_failure=AsyncMock(),
            open_for=AsyncMock(),
        ),
        retry_handler=RetryHandler(max_retries=None),
    )

    async def fake_stream_req(**kwargs):
        # 主模型：持续 5xx（连败积累）；备用模型：PermanentError 终态
        if current["cfg"].get("id") == "uuid-B":
            raise PermanentError("HTTP 401: backup misconfigured", status=401)
        raise RetryableError("HTTP 500: upstream wave", status=500)

    streamer._do_streaming_request = fake_stream_req  # type: ignore[method-assign]

    # 对齐真实流程：stream() 入口会 bind_primary（登记主模型进 tried 集，
    # 供候选排除与方言判定）；本用例直调 _stream_single_round，手动等价登记。
    streamer._failover_state().bind_primary(primary)

    with (
        patch(
            "hiveweave.services.model.ModelService.list_active_full",
            _mk_pool(primary, backup),
        ),
        patch("hiveweave.services.inbox.InboxService", _inbox_cls(notices)),
    ):
        result = await streamer._stream_single_round(
            agent_id="a1",
            provider=streamer._provider_factory.create(primary),
            provider_name="longcat",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            on_delta=None,
            round_num=0,
            delta_id="d0",
        )

    assert result["status"] == "error"
    assert "backup misconfigured" in str(result["error"])
    # 转移发生：生效配置 = 备用；notice 已发（诚实原则）
    assert streamer._failover_state().active_config["id"] == "uuid-B"
    assert len(notices) == 1 and "已切换" in notices[0]["message"]


@pytest.mark.asyncio
async def test_streamer_no_candidate_keeps_riding_until_deadline():
    """池走完（无候选）⇒ 闸不中止，骑乘到 deadline 抛最后错误收口。"""
    primary = _model("uuid-A")
    current = {"cfg": primary}

    class _FakeFactory:
        def create(self, cfg):
            current["cfg"] = cfg
            return _FakeProvider(model_name=str(cfg.get("model_id") or ""))

    streamer = Streamer(
        provider_factory_inst=_FakeFactory(),
        circuit_breaker_inst=MagicMock(
            report_success=AsyncMock(), report_failure=AsyncMock()
        ),
        retry_handler=RetryHandler(max_retries=None),
    )

    calls = {"n": 0}

    async def fake_stream_req(**kwargs):
        calls["n"] += 1
        raise RetryableError("HTTP 500: wave", status=500)

    streamer._do_streaming_request = fake_stream_req  # type: ignore[method-assign]

    # 对齐真实流程：stream() 入口 bind_primary ⇒ 主模型自身被 tried 集排除
    streamer._failover_state().bind_primary(primary)

    with patch(
        "hiveweave.services.model.ModelService.list_active_full",
        _mk_pool(primary),  # 池里只有主模型 → 无候选
    ):
        result = await streamer._stream_single_round(
            agent_id="a1",
            provider=streamer._provider_factory.create(primary),
            provider_name="name-uuid-A",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            on_delta=None,
            round_num=0,
            delta_id="d0",
            budget_deadline=time.monotonic() + 0.2,
        )

    assert result["status"] == "error"
    assert "wave" in str(result["error"])
    assert streamer._failover_state().active_config is None  # 从未切换
    assert calls["n"] >= 3  # 骑乘发生（连败 ≥3 未换模型）


def test_run_end_restores_primary_model():
    """转移本回合临时生效（实例态，不写 DB）：新 run（新实例）恢复主模型。"""
    primary = _model("uuid-A")
    backup = _model("uuid-B")

    streamer = Streamer(
        provider_factory_inst=MagicMock(),
        circuit_breaker_inst=MagicMock(),
        retry_handler=RetryHandler(max_retries=1),
    )
    fo = streamer._failover_state()
    fo.bind_primary(primary)
    # 未切换 → 生效 = 主模型
    assert fo.effective_config(primary) is primary

    # 本 run 内切换 → 生效 = 备用（同实例跨 stream() 续用）
    fo.active_config = backup
    assert fo.effective_config(primary) is backup

    # 下回合 = 新实例（agent 每 turn 新建 Streamer）→ 主模型
    fresh = Streamer(
        provider_factory_inst=MagicMock(),
        circuit_breaker_inst=MagicMock(),
        retry_handler=RetryHandler(max_retries=1),
    )
    fresh_fo = fresh._failover_state()
    fresh_fo.bind_primary(primary)
    assert fresh_fo.effective_config(primary) is primary


def test_failover_threshold_env(monkeypatch):
    """阈值 env 可调（默认 3）。"""
    assert failover_threshold() == 3
    monkeypatch.setenv("HIVEWEAVE_LLM_FAILOVER_THRESHOLD", "5")
    assert failover_threshold() == 5
    monkeypatch.setenv("HIVEWEAVE_LLM_FAILOVER_THRESHOLD", "bogus")
    assert failover_threshold() == 3


# ── stream() 入口：生效配置/身份切换 ────────────────────────


@pytest.mark.asyncio
async def test_stream_entry_applies_failover_config():
    """run 内已切换后重开 stream()（agent transport 重试路）⇒ 生效配置 =
    备用模型（provider/身份/熔断键全部按备用走），不回跳主模型。"""
    primary = _model("uuid-A", model_id="longcat")
    backup = _model("uuid-B", model_id="ark")
    created: list[dict] = []

    class _FakeFactory:
        def create(self, cfg):
            created.append(dict(cfg))
            return _FakeProvider(model_name=str(cfg.get("model_id") or ""))

    breaker = MagicMock()
    breaker.register = AsyncMock()
    breaker.check = AsyncMock(return_value=SimpleNamespace(allowed=True))
    breaker.cooldown_left_s = MagicMock(return_value=0)
    breaker.report_failure = AsyncMock()
    breaker.report_success = AsyncMock()

    streamer = Streamer(
        provider_factory_inst=_FakeFactory(),
        circuit_breaker_inst=breaker,
        retry_handler=RetryHandler(max_retries=0),
    )
    streamer._run_tool_loop = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "status": "ok",
            "content": "done",
            "thinking": "",
            "tool_calls": [],
            "tool_turn_messages": [],
            "rounds": 1,
            "usage": None,
        }
    )
    streamer._broadcast_model_cold_start = AsyncMock()  # type: ignore[method-assign]

    # 预置：本 run 已切换到备用
    streamer._failover_state().bind_primary(primary)
    streamer._failover_state().active_config = backup

    result = await streamer.stream(
        agent_id="a1",
        messages=[{"role": "user", "content": "hi"}],
        model_config=primary,
    )

    assert result["status"] == "ok"
    assert created and created[0]["id"] == "uuid-B"  # 生效配置 = 备用
    breaker.register.assert_awaited_once_with("name-uuid-B")


# ── 可抄①：失败 turn 标记不进下轮模型上下文 ────────────────


def _mk_store_with(history: list[dict]):
    from hiveweave.conversation.store import ConversationStore

    store = ConversationStore()
    store._load_from_db = AsyncMock(return_value=list(history))  # type: ignore[method-assign]
    store._load_compacted_prefix = AsyncMock()  # type: ignore[method-assign]
    return store


@pytest.mark.asyncio
async def test_get_history_filters_failed_turn_markers_for_llm_context():
    """LLM 上下文读路径过滤 [ERROR] 标记；recovery 判据读全量不受影响。"""
    from hiveweave.agents.recovery import FAILED_TURN_MARKER

    history = [
        {"role": "user", "content": "修复登录 bug"},
        {
            "role": "assistant",
            "content": f"{FAILED_TURN_MARKER} [ERROR] HTTP 500: wave",
        },
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "正常回复，提到 error 一词不应误伤"},
    ]
    store = _mk_store_with(history)

    llm_view = await store.get_history(
        "a1", "p1", include_failed_turn_markers=False
    )
    assert all(
        not str(m.get("content") or "").startswith(FAILED_TURN_MARKER)
        for m in llm_view
    )
    assert any(m.get("content") == "修复登录 bug" for m in llm_view)  # 指令保留
    assert any("正常回复" in str(m.get("content")) for m in llm_view)

    # 默认（recovery 判据/幂等检查路径）保持全量
    full_view = await store.get_history("a1", "p1")
    assert any(
        str(m.get("content") or "").startswith(FAILED_TURN_MARKER)
        for m in full_view
    )


@pytest.mark.asyncio
async def test_get_history_filters_legacy_error_prefix():
    """历史遗留的裸 [ERROR] 前缀（无 PLATFORM_TURN_MARKER 包装）同样过滤。"""
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "[ERROR] old run died"},
    ]
    store = _mk_store_with(history)
    llm_view = await store.get_history(
        "a1", "p1", include_failed_turn_markers=False
    )
    assert llm_view == [{"role": "user", "content": "q"}]
