"""I6（fixplan 批 11）唯一价格源 + I7 决策侧 idle 归因。

验收对照（fixplan §四 I6/I7 · 可机检面）：

① **价格不可得 ⇒ 不武装且 reason 可判定** —— ``cache_warmer_not_armed``
   回执事件，``reason="prices_unavailable"`` + ``missing`` 逐列列缺失；
② **价格可得 ⇒ compute_warm_decision 收真实 prices** —— 不再 DEFAULT_PRICES
   误判（病灶：40 次武装 / 0 次触发，11.9 万 token 按 Sonnet 档价恒差一倍）；
③ **llm_models 价格列存在且进 fail-loud 自检登记** —— ⚠ llm_models 是
   **Meta DB** 表，登记在 ``META_DB_COLUMN_CHECKS``（Meta 侧自检，
   db/meta.py::init_meta_db 消费）。**不许**登进 ``PROJECT_DB_COLUMN_CHECKS``
   （per-project DB 无此表，PRAGMA table_info 落空 ⇒ 恒 fail-loud 假阳性
   炸掉所有项目库）—— 本文件有回归守卫钉死这一点；
④ **I7 决策侧**：武装回执纳入 idle 归因（probe 留档 readout 的
   ``final``/``idle_ms``），「间隔类 miss」与「前缀漂移类 miss」在决策面
   不再同桶；「1h TTL 档只写不续」生效决策不推翻（AI_MEMORY 拍板）。

不做真实网络调用；不写生产库（conftest._isolate_meta_db 钉 tmp）；
不 wrap aiosqlite.Connection（自建连接直连，用完即关）。
"""

from __future__ import annotations

import re
from unittest.mock import patch

import aiosqlite
import pytest
from structlog.testing import capture_logs

from hiveweave.config import settings
from hiveweave.db import meta as meta_db_mod
from hiveweave.db.schema import (
    META_DB_COLUMN_CHECKS,
    META_DB_TABLES,
    PROJECT_DB_COLUMN_CHECKS,
)
from hiveweave.llm.streamer import probe as probe_mod
from hiveweave.llm.streamer.probe import (
    compare_and_record,
    last_cache_readout,
    report_cache_readout,
    reset_probe,
)
from hiveweave.services import cache_warmer as cw
from hiveweave.services.cache_warmer import (
    DEFAULT_PRICES,
    CacheWarmer,
    compute_warm_decision,
    extract_model_prices,
)
from hiveweave.services.model import InvalidModelConfig, ModelService

# ── 公共夹具 ──────────────────────────────────────────────────

MODEL = {
    "base_url": "https://gw.example/v1",
    "api_key": "k",
    "model_id": "cheap-model",
    "provider_type": "openai-compatible",
    "context_window": 128_000,
    "max_output_tokens": 8_192,
}

MESSAGES = [
    {"role": "system", "content": "identity"},
    {"role": "user", "content": "go"},
]

SONNET_PRICES = {
    "price_input": 3.0,
    "price_output": 15.0,
    "price_cache_read": 0.30,
    "price_cache_write": 3.75,
}


@pytest.fixture(autouse=True)
def _isolate_probe_state():
    """探针运行态按用例隔离（_last_readout 是本批新增的进程级留档）。"""
    reset_probe()
    yield
    reset_probe()


@pytest.fixture()
def fresh_warmer():
    return CacheWarmer()


def _fast_ttl(monkeypatch):
    """openai 系 TTL=10.5s ⇒ 延迟 0.5s（测试可等）；其他协议走真实推档。"""
    real = cw.estimate_prompt_cache_ttl_ms
    monkeypatch.setattr(
        cw,
        "estimate_prompt_cache_ttl_ms",
        lambda fmt: (
            10_500 if str(fmt or "").startswith("openai") else real(fmt)
        ),
    )
    monkeypatch.setattr(settings, "cache_warmer_enabled", True)


def _armed_events(captured: list[dict]) -> list[dict]:
    return [e for e in captured if e.get("event") == "cache_warmer_armed"]


def _not_armed_events(captured: list[dict]) -> list[dict]:
    return [e for e in captured if e.get("event") == "cache_warmer_not_armed"]


# ── 验收①：价格不可得 ⇒ 不武装，reason 可判定 ─────────────────


def test_extract_prices_missing_all_required():
    prices, missing = extract_model_prices(MODEL)
    assert prices is None
    assert missing == [
        "price_input",
        "price_output",
        "price_cache_read",
    ], "cache_write 可缺省（回退 input 价），三个必需价缺失须逐列点名"


def test_extract_prices_partial_missing_lists_only_the_gap():
    cfg = {**MODEL, "price_input": 1.0, "price_output": 2.0}
    prices, missing = extract_model_prices(cfg)
    assert prices is None
    assert missing == ["price_cache_read"]


def test_extract_prices_negative_or_garbage_is_unavailable():
    for bad in (-1.0, "abc", float("nan"), float("inf")):
        cfg = {**MODEL, **SONNET_PRICES, "price_input": bad}
        prices, missing = extract_model_prices(cfg)
        assert prices is None, bad
        assert missing == ["price_input"]


def test_extract_prices_zero_is_available_not_unavailable():
    """0.0 = 免费模型（价格真实可得），与 NULL（不可得）不同形。"""
    cfg = {
        **MODEL,
        "price_input": 0.0,
        "price_output": 0.0,
        "price_cache_read": 0.0,
    }
    prices, missing = extract_model_prices(cfg)
    assert missing == []
    assert prices == {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                      "cache_write": 0.0}


def test_extract_prices_cache_write_null_falls_back_in_math():
    """cache_write NULL ⇒ 以 0.0 参与 ⇒ 决策数学按 input 价算 miss（pi 同回退）。"""
    cfg = {**MODEL, "price_input": 3.0, "price_output": 15.0,
           "price_cache_read": 0.30}
    prices, missing = extract_model_prices(cfg)
    assert missing == []
    d = compute_warm_decision(100_000, prices, phase="streaming")
    # missCost = 100000 × (3.0 − 0.30)/1e6 = 0.27（写价回退 input 价）
    assert round(d["miss_cost"], 6) == 0.27
    assert d["action"] == "warm"


@pytest.mark.asyncio
async def test_prices_unavailable_not_armed_with_judgeable_receipt(
    fresh_warmer, monkeypatch
):
    """验收①主断言：无价格 ⇒ 不武装 + 回执点名缺失列（可从回执判定）。"""
    _fast_ttl(monkeypatch)
    fresh_warmer.observe_request(
        "a1", "p1", MODEL, MESSAGES, None, prompt_tokens=119_000
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a1")
    assert "a1" not in fresh_warmer._runs, "价格不可得 ⇒ 不得武装"
    events = _not_armed_events(cap)
    assert events, "必须有显式「为何不武装」回执（不许静默/不许看起来在跑）"
    evt = next(e for e in events if e.get("reason") == "prices_unavailable")
    assert evt["missing"] == ["price_input", "price_output", "price_cache_read"]
    assert evt["model"] == "cheap-model"


@pytest.mark.asyncio
async def test_with_prices_same_model_arms(fresh_warmer, monkeypatch):
    """阳性对照（配对断言）：同一模型补上价格 ⇒ 正常武装。"""
    _fast_ttl(monkeypatch)
    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a1")
    assert "a1" in fresh_warmer._runs
    assert not [
        e for e in _not_armed_events(cap) if e.get("reason") == "prices_unavailable"
    ]
    fresh_warmer.cancel("a1")


@pytest.mark.asyncio
async def test_one_hour_tier_still_write_only_even_with_prices(
    fresh_warmer, monkeypatch
):
    """I7 生效决策「1h TTL 档只写不续」不许推翻（AI_MEMORY 拍板）：
    anthropic long_ttl ⇒ delay 54min > 30min idle 上限 ⇒ 即便价格齐备也不武装，
    回执 reason=ttl_exceeds_idle_horizon。"""
    fresh_warmer.observe_request(
        "a-1h",
        "p1",
        {
            **MODEL,
            **SONNET_PRICES,
            "provider_type": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "base_url": "https://api.anthropic.com",
        },
        MESSAGES,
        None,
        prompt_tokens=300_000,
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a-1h")
    assert "a-1h" not in fresh_warmer._runs
    evt = next(
        e
        for e in _not_armed_events(cap)
        if e.get("reason") == "ttl_exceeds_idle_horizon"
    )
    assert evt["delay_ms"] == 3_240_000


# ── 验收②：价格可得 ⇒ 决策收真实 prices（DEFAULT_PRICES 退役）──


@pytest.mark.asyncio
async def test_armed_run_carries_real_prices(fresh_warmer, monkeypatch):
    _fast_ttl(monkeypatch)
    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    fresh_warmer.on_agent_settled("a1")
    run = fresh_warmer._runs.get("a1")
    assert run is not None
    assert run.prices == dict(DEFAULT_PRICES), (
        "武装时点钉住的单价表必须来自 model_config 价格列"
    )
    fresh_warmer.cancel("a1")


@pytest.mark.asyncio
async def test_warm_loop_decision_receives_real_prices(
    fresh_warmer, monkeypatch
):
    """到点决策必须用武装时钉住的真实价（不再隐式落回 DEFAULT_PRICES）。"""
    _fast_ttl(monkeypatch)
    captured_calls: list[dict] = []
    real_decision = cw.compute_warm_decision

    def _spy(prompt_tokens, prices=None, *, phase="idle"):
        captured_calls.append({"prices": prices, "phase": phase})
        return real_decision(prompt_tokens, prices, phase=phase)

    async def fake_send(run):
        return None

    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with patch.object(
        cw, "compute_warm_decision", side_effect=_spy
    ), patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        await _sleep_until(lambda: captured_calls, 2.0)
        fresh_warmer.cancel("a1")
    assert captured_calls, "到点决策必须发生"
    assert all(
        c["prices"] == dict(DEFAULT_PRICES) for c in captured_calls
    ), f"决策收到的必须是真实价，实得 {captured_calls[0]['prices']}"


async def _sleep_until(cond, timeout: float) -> None:
    import asyncio
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cheap_real_prices_flip_verdict_default_prices_gone(
    fresh_warmer, monkeypatch
):
    """病灶回归对照：30 万 token 的便宜模型（DeepSeek 档价）——
    旧实现按 DEFAULT_PRICES(Sonnet 档) 判 warm（fixplan：要 ≥$0.05 需
    n≳23 万，纯靠档价虚高凑数）；真实价下决策 stop，且回执带
    economics_available=True + prices（按什么价算的可判定）。"""
    _fast_ttl(monkeypatch)
    cheap = {
        "price_input": 0.27,
        "price_output": 1.1,
        "price_cache_read": 0.027,
        "price_cache_write": 0.27,
    }
    # 假阳性对照：硬编码 Sonnet 档价 ⇒ 30 万 token 判 warm（正是被废掉的
    # 误判路径——便宜模型被档价虚高「谎报」成值得续暖）
    assert compute_warm_decision(300_000, None, phase="idle")["action"] == "warm"
    # 真实价（extract 后的键形：input/output/cache_read/cache_write）⇒
    # expected = 0.002734 < 0.05 ⇒ stop
    cheap_prices = {
        "input": 0.27, "output": 1.1, "cache_read": 0.027, "cache_write": 0.27,
    }
    d = compute_warm_decision(300_000, cheap_prices, phase="idle")
    assert d["action"] == "stop"
    assert d["economics_available"] is True

    sends: list[int] = []

    async def fake_send(run):
        sends.append(run.prompt_tokens)

    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **cheap}, MESSAGES, None, prompt_tokens=300_000
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        await _sleep_until(lambda: False, 1.2)  # 等 0.5s 延迟到点
        assert "a1" not in fresh_warmer._runs, "决策 stop ⇒ 停止续暖"
    assert not sends, "便宜模型真实价下不得发续暖请求"


@pytest.mark.asyncio
async def test_free_model_zero_prices_stop_economics_unavailable(
    fresh_warmer, monkeypatch
):
    """免费模型（全 0.0 价，真实可得）⇒ 武装后决策 stop：
    无钱可省，economics_available=False，回执可判定。"""
    _fast_ttl(monkeypatch)
    free = {
        "price_input": 0.0,
        "price_output": 0.0,
        "price_cache_read": 0.0,
        "price_cache_write": 0.0,
    }
    sends: list[int] = []

    async def fake_send(run):
        sends.append(run.prompt_tokens)

    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **free}, MESSAGES, None, prompt_tokens=119_000
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        with capture_logs() as cap:
            fresh_warmer.on_agent_settled("a1")
            await _sleep_until(lambda: False, 1.2)
    assert not sends, "免费模型无节省可图 ⇒ 不得发送"
    stopped = [e for e in cap if e.get("event") == "cache_warmer_stopped"]
    assert stopped, "必须有 stop 回执"
    assert stopped[0]["reason"] == "cache economics unavailable"
    assert stopped[0]["economics_available"] is False
    assert stopped[0]["prices"] == {"input": 0.0, "output": 0.0,
                                    "cache_read": 0.0, "cache_write": 0.0}


@pytest.mark.asyncio
async def test_expensive_prices_still_warm_and_send(fresh_warmer, monkeypatch):
    """验收②正向闭环：价格可得且过门槛 ⇒ 真实发续暖（机制真的会跑）。"""
    _fast_ttl(monkeypatch)
    sends: list[int] = []

    async def fake_send(run):
        sends.append(run.prompt_tokens)

    fresh_warmer.observe_request(
        "a1", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with patch.object(fresh_warmer, "_send_warm_request", side_effect=fake_send):
        fresh_warmer.on_agent_settled("a1")
        await _sleep_until(lambda: bool(sends), 2.0)
        fresh_warmer.cancel("a1")
    assert sends, "expected savings ≥ $0.05 ⇒ 必须发续暖（request_type=cache_warm 路径）"


# ── 验收③：llm_models 价格列 + fail-loud 自检登记 ───────────────


def _canonical_llm_models_ddl() -> str:
    for ddl in META_DB_TABLES:
        if "CREATE TABLE IF NOT EXISTS llm_models" in ddl:
            return ddl
    raise AssertionError("META_DB_TABLES 里找不到 llm_models 正典 DDL")


def test_price_columns_in_canonical_meta_ddl():
    ddl = _canonical_llm_models_ddl()
    for col in (
        "price_input",
        "price_output",
        "price_cache_read",
        "price_cache_write",
    ):
        assert re.search(rf"^\s*{col}\s+REAL,", ddl, re.M), (
            f"llm_models 正典 DDL 缺 {col} —— 每个新库都会缺唯一价格源"
        )


def test_price_columns_registered_in_fail_loud_check():
    registered = META_DB_COLUMN_CHECKS.get("llm_models")
    assert registered is not None, "价格列必须进 Meta 侧 fail-loud 自检登记"
    assert {
        "price_input",
        "price_output",
        "price_cache_read",
        "price_cache_write",
    } <= registered


def test_llm_models_must_not_be_in_project_db_checks():
    """回归守卫：llm_models 是 Meta 表，登进 PROJECT_DB_COLUMN_CHECKS 会让
    每个项目库建连时 PRAGMA table_info 落空 ⇒ 恒 fail-loud 假阳性炸全场。"""
    assert "llm_models" not in PROJECT_DB_COLUMN_CHECKS


@pytest.mark.asyncio
async def test_meta_column_check_green_on_canonical_ddl():
    """自检绿灯：按正典 DDL 建的 llm_models 必须过 _assert_meta_columns。"""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_canonical_llm_models_ddl())
        await meta_db_mod._assert_meta_columns(conn)  # 不 raise 即绿
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_meta_column_check_fails_loud_on_missing_column():
    """阳性对照（改坏 → 红）：抽掉一列价格 ⇒ 自检必须 fail-loud。"""
    stripped = re.sub(
        r"^\s*price_cache_read REAL,\n", "", _canonical_llm_models_ddl(),
        flags=re.M,
    )
    assert "price_cache_read" not in stripped, "测试前置：剥离必须生效"
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(stripped)
        with pytest.raises(meta_db_mod.MetaDbError) as ei:
            await meta_db_mod._assert_meta_columns(conn)
        assert "price_cache_read" in str(ei.value)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_meta_db_migration_adds_price_columns(tmp_path):
    """存量老库路径：无价格列的旧 llm_models 经 _migrate_meta_schema 补列
    后必须通过自检（迁移断裂才会 fail-loud，正常迁移不炸）。"""
    await meta_db_mod.close_meta_db()  # 复位 per-connection 迁移标记
    conn = await aiosqlite.connect(str(tmp_path / "legacy-meta.db"))
    try:
        await conn.execute(
            "CREATE TABLE llm_models ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, model_id TEXT NOT NULL, "
            "created_at INTEGER, updated_at INTEGER)"
        )
        await conn.commit()
        await meta_db_mod._migrate_meta_schema(conn)
        await meta_db_mod._assert_meta_columns(conn)
        cur = await conn.execute("PRAGMA table_info(llm_models)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {
            "price_input",
            "price_output",
            "price_cache_read",
            "price_cache_write",
        } <= cols
    finally:
        await conn.close()
        await meta_db_mod.close_meta_db()


# ── 验收③续：模型管理写入点（ModelService / API 映射）──────────


@pytest.mark.asyncio
async def test_model_service_price_roundtrip():
    """create 写价 → get/list 读价；update 显式 None 清回「不可得」。"""
    svc = ModelService()
    created = await svc.create(
        {
            "name": "priced",
            "model_id": "priced-model",
            "base_url": "https://gw.example/v1",
            "api_key": "k",
            "provider_type": "openai-compatible",
            **SONNET_PRICES,
        }
    )
    row = await svc.get(created["id"])
    assert row["price_input"] == 3.0
    assert row["price_output"] == 15.0
    assert row["price_cache_read"] == 0.30
    assert row["price_cache_write"] == 3.75

    # 清空 ⇒ NULL（不可得）——PATCH 显式 null 穿透语义
    await svc.update(created["id"], {"price_input": None})
    row = await svc.get(created["id"])
    assert row["price_input"] is None
    assert row["price_output"] == 15.0, "未触碰的列不得被连带清掉"

    # 清空后的行正是 cache_warmer fail-closed 的输入形态
    prices, missing = extract_model_prices(row)
    assert prices is None and missing == ["price_input"]

    listed = await svc.list_all()
    assert listed[0]["price_cache_read"] == 0.30


@pytest.mark.asyncio
async def test_model_service_rejects_negative_price():
    svc = ModelService()
    with pytest.raises(InvalidModelConfig):
        await svc.create(
            {
                "name": "bad",
                "model_id": "bad-model",
                "price_input": -0.5,
            }
        )
    created = await svc.create(
        {"name": "ok", "model_id": "ok-model", **SONNET_PRICES}
    )
    with pytest.raises(InvalidModelConfig):
        await svc.update(created["id"], {"price_output": float("nan")})


def test_api_attr_mapping_and_response_echo():
    from hiveweave.api.models import ModelCreate, _model_response, _normalize_attrs

    attrs = _normalize_attrs(
        ModelCreate(
            name="n",
            priceInput=3.0,
            priceOutput=15.0,
            priceCacheRead=0.3,
            priceCacheWrite=3.75,
        )
    )
    assert attrs["price_input"] == 3.0
    assert attrs["price_cache_write"] == 3.75

    echo = _model_response({"price_input": 0.3, "price_output": None})
    assert echo["priceInput"] == 0.3 and echo["price_input"] == 0.3
    assert echo["priceOutput"] is None and echo["price_output"] is None


# ── 验收④：I7 决策侧 —— 武装回执纳入 idle 归因 ─────────────────


def _seed_readout(
    agent_id: str,
    *,
    drifted: bool,
    gap_s: float = 120.0,
) -> dict:
    """走真实探针路径制造一份已上报 readout（含 final / idle_ms）。

    drifted=False ⇒ prefix_stable + 零命中 ⇒ final=cache_window_expired
    （**间隔类 miss**）；drifted=True ⇒ 对话主体被改写 ⇒ final=drift_zero_hit
    （**前缀漂移类 miss**）。两档 idle_ms 相同 ⇒ 决策面只能靠 final 分桶。
    """
    base = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "turn-1"},
    ]
    drifted_msgs = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "REWRITTEN"},
    ]
    t0 = 1_000.0
    compare_and_record(agent_id, base, model_key="m@gateway", now=t0)
    if drifted:
        # run 内见过漂移（粘性标记）+ 本次首请求对话主体被改写
        compare_and_record(
            agent_id,
            drifted_msgs,
            model_key="m@gateway",
            now=t0 + 60.0,
            slot="run_inner",
        )
        msgs = drifted_msgs
    else:
        msgs = base
    compare_and_record(
        agent_id, msgs, model_key="m@gateway", now=t0 + gap_s
    )
    readout = report_cache_readout(
        agent_id, input_tokens=50_000, cache_read=0, cache_creation=50_000
    )
    assert readout is not None
    assert readout["idle_ms"] == int(gap_s * 1000)
    assert readout["final"] == (
        "drift_zero_hit" if drifted else "cache_window_expired"
    )
    return readout


@pytest.mark.asyncio
async def test_armed_receipt_carries_idle_window_attribution(
    fresh_warmer, monkeypatch
):
    """间隔类 miss（cache_window_expired）⇒ 武装回执带 final + idle_ms。"""
    _fast_ttl(monkeypatch)
    readout = _seed_readout("a-idle", drifted=False, gap_s=120.0)
    fresh_warmer.observe_request(
        "a-idle", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a-idle")
    assert "a-idle" in fresh_warmer._runs
    armed = _armed_events(cap)
    assert armed, "武装回执必须存在"
    assert armed[0]["last_miss_final"] == "cache_window_expired"
    assert armed[0]["last_miss_idle_ms"] == readout["idle_ms"] == 120_000
    fresh_warmer.cancel("a-idle")


@pytest.mark.asyncio
async def test_armed_receipt_distinguishes_drift_class_from_idle_class(
    fresh_warmer, monkeypatch
):
    """前缀漂移类 miss（drift_zero_hit）与间隔类在**决策面**不再同桶：
    同样的 idle_ms、不同的 final，武装回执可分。"""
    _fast_ttl(monkeypatch)
    readout = _seed_readout("a-drift", drifted=True, gap_s=120.0)
    assert readout["final"] == "drift_zero_hit"
    fresh_warmer.observe_request(
        "a-drift", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a-drift")
    armed = _armed_events(cap)
    assert armed and armed[0]["last_miss_final"] == "drift_zero_hit"
    assert armed[0]["last_miss_idle_ms"] == 120_000
    fresh_warmer.cancel("a-drift")


@pytest.mark.asyncio
async def test_armed_without_readout_attribution_is_none_not_guessed(
    fresh_warmer, monkeypatch
):
    """无探针留档 ⇒ 归因位为 None（**不臆测**），武装照常（归因缺失不阻塞）。"""
    _fast_ttl(monkeypatch)
    assert last_cache_readout("a-none") is None
    fresh_warmer.observe_request(
        "a-none", "p1", {**MODEL, **SONNET_PRICES}, MESSAGES, None,
        prompt_tokens=300_000,
    )
    with capture_logs() as cap:
        fresh_warmer.on_agent_settled("a-none")
    armed = _armed_events(cap)
    assert armed
    assert armed[0]["last_miss_final"] is None
    assert armed[0]["last_miss_idle_ms"] is None
    fresh_warmer.cancel("a-none")


def test_probe_readout_accessor_and_reset():
    _seed_readout("a-x", drifted=False)
    assert last_cache_readout("a-x")["final"] == "cache_window_expired"
    # 返回的是副本，改它不影响留档
    got = last_cache_readout("a-x")
    got["final"] = "tampered"
    assert last_cache_readout("a-x")["final"] == "cache_window_expired"
    reset_probe("a-x")
    assert last_cache_readout("a-x") is None
