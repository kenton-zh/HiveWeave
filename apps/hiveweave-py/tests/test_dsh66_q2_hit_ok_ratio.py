"""TEST_DSH_66 Q2 回归：`hit_ok` 必须是**比例**判据，不是存在性判据。

病灶（2026-09-22 实测）：`report_cache_readout` 里
``elif cache_read > 0: final = "hit_ok"`` 短路先于漂移分析 ⇒ 只要 provider
回了**任何**一个 token 就判绿。现场：77 条请求 `cache_read` 落在 1..500、
`input_tokens` 合计 3,793,790（均值约 4.9 万/条）⇒ 命中率量级 **0.2%~1%**
（等于没命中），却被 77/77 全判 `hit_ok`
（`GROUP BY r.cache_verdict` 只有一档）—— 判决与证据自相矛盾。

修后判据：``cache_read / (input_tokens + cache_read) >= _HIT_OK_MIN_RATIO``，
形式命中单列 ``near_zero_hit``（它是**新档**，与 0 命中那三档语义不同）。

⚠ 阈值经现场全量分布校准（审计 ②-5）：原取 0.5，实测会额外误伤
0.23~0.50 的**真实**部分命中（`0.5 ⇒ near_zero 87` 条 /
`0.05 ⇒ 77` 条 ⇒ 差 **10** 条）；现场数据在 **0.0112 ~ 0.2332** 之间
**完全没有取值**（实测 0 条）⇒ 阈值落在该空档即可。现取 0.05
（病灶群上界 0.0112 的 4.5 倍）。

⚠ **分母口径**（审计 A1 复核实证）：`input_tokens` 是**未命中桶**，故分母
= `input + cache_read`。此前提已逐环验证（`normalize_usage` 对
非 anthropic 会减、`total = input + output` **1456/1456** 行成立），**不是**
双计。现场 `cache_read > input` 占 94% 是正常签名，非异常。
（`util.cache_hit_percent(..., input_inclusive=)` 是 DeepSeek 系专用；
`openai-responses` 用本口径。）

本文件覆盖：
1. 现场形态逐字复现：`cache_read=113, input=49532` 之类近零命中**不得**判 hit_ok。
2. 阈值边界两侧（刚好达标 / 刚好不达标）。
3. 真命中（含**部分命中** 0.23~0.47 —— 0.5 阈值下会被误伤的那群）仍判 hit_ok。
4. 既有语义不变：cache_read=0 那三档（cold_start / window_expired /
   drift_zero_hit）与 None（unknown_usage）**全部不受影响**。
5. 阳性对照：把判据打回 `> 0`，第 1 条断言必须转红。
"""

from __future__ import annotations

import pytest

from hiveweave.llm.streamer import probe
from hiveweave.llm.streamer.probe import (
    _HIT_OK_MIN_RATIO,
    compare_and_record,
    report_cache_readout,
    reset_probe,
)


MODEL_KEY = "deepseek-v4@https://api.example.com/v1"

#: 阈值在本测试内的**字面副本** —— 故意不 import，避免"改坏常量后断言
#: 跟着变绿"的半自证（审计 ③-7）。`_HIT_OK_MIN_RATIO` 自身另由
#: `test_threshold_constant_is_a_ratio_not_a_count` 钉住量纲。
THRESHOLD_LITERAL = 0.05


def _seed_prefix_stable(agent_id: str = "a1") -> None:
    """造一个 prefix_stable 的基准（两次相邻 run，history 追加）。"""
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    msgs1 = [
        {"role": "system", "content": "IDENTITY-V1"},
        *h,
        {"role": "system", "content": "CTX"},
        {"role": "user", "content": "q2"},
    ]
    compare_and_record(agent_id, msgs1, model_key=MODEL_KEY)
    msgs2 = [
        {"role": "system", "content": "IDENTITY-V1"},
        *h,
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "system", "content": "CTX"},
        {"role": "user", "content": "q3"},
    ]
    v = compare_and_record(agent_id, msgs2, model_key=MODEL_KEY)
    assert v["verdict"] == "prefix_stable", f"夹具前提不成立: {v}"


@pytest.fixture(autouse=True)
def _clean_probe_state():
    reset_probe()
    yield
    reset_probe()


# ── 1. 现场形态：近零命中不得判绿 ────────────────────────────


@pytest.mark.parametrize(
    "cache_read,input_tokens",
    [
        (113, 49532),   # 现场均值量级（0.23%）
        (1, 3_793_790), # 极端：1 token 命中 / 379 万 input
        (500, 10_001),  # 报告取数区间上沿（4.8%）
        (300, 40_000),  # 0.74%
    ],
)
def test_near_zero_hits_are_not_green(cache_read, input_tokens):
    """⚠ Q2 核心断言：形式命中（>0 但量级不对）必须**离开** hit_ok 档。

    这些数字取自报告 §二 的取数片段：
    ``WHERE cache_read_tokens BETWEEN 1 AND 500 AND input_tokens > 10000``
    —— 该查询实测命中 77 条，修前 77/77 判 hit_ok。
    """
    _seed_prefix_stable()
    r = report_cache_readout(
        "a1", input_tokens=input_tokens, cache_read=cache_read, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "near_zero_hit", (
        f"cache_read={cache_read}/input={input_tokens} "
        f"(命中率 {cache_read / (cache_read + input_tokens):.4%}) "
        f"被判 {r['final']} —— 判决与证据矛盾"
    )
    # 必须同时保住"有字节回来"这个事实（不是 unknown，也不是 0 命中档）
    assert r["cache_read"] == cache_read
    assert r["cache_hit_ratio"] < _HIT_OK_MIN_RATIO


def test_near_zero_hit_is_distinct_from_zero_hit_buckets():
    """`near_zero_hit` 不得与 0 命中那三档混同 —— 它们语义前提不同。"""
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=49_532, cache_read=113,
                             cache_creation=0)
    assert r is not None
    # 这三档都以 cache_read == 0 为前提；113 落进去同样失真
    for wrong in ("cold_start", "cache_window_expired", "drift_zero_hit",
                  "unknown_usage"):
        assert r["final"] != wrong, f"113 tokens 不该判 {wrong}"


# ── 2. 阈值边界两侧 ──────────────────────────────────────────


def test_exactly_at_threshold_is_green():
    """**判据方向要跑出来**：恰好达阈 ⇒ 绿（>=，不是 >）。

    阈值 0.05 ⇒ ratio = 500/(500+9500) = 0.05 ⇒ 应判 hit_ok。
    """
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=9_500, cache_read=500,
                             cache_creation=0)
    assert r is not None
    assert r["cache_hit_ratio"] == pytest.approx(_HIT_OK_MIN_RATIO)
    assert r["final"] == "hit_ok"


def test_just_below_threshold_is_near_zero():
    """刚低于阈值 ⇒ 非绿（阈值是闭区间的下界，方向不可写反）。

    阈值 0.05 ⇒ 取 499/9501 = 0.0525 之上为绿、499/10001 = 0.0475 为红。
    """
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=10_001, cache_read=499,
                             cache_creation=0)
    assert r is not None
    assert r["cache_hit_ratio"] < _HIT_OK_MIN_RATIO
    assert r["final"] == "near_zero_hit"


def test_threshold_literal_matches_module_constant():
    """被测常量必须等于本文件写死的字面量（防"悄悄改阈值"无感）。

    审计 ③-7 的另一半：断言不 import 常量是**为了独立**，但也不能让
    常量被改到任意值而无人发觉 ⇒ 用这一条显式对表。阈值若确需调整，
    这里会红，逼改动者同时更新现场分布依据（见 probe.py 处注释）。
    """
    assert _HIT_OK_MIN_RATIO == pytest.approx(THRESHOLD_LITERAL)


# ── 3. 真命中不得误伤 ────────────────────────────────────────


@pytest.mark.parametrize(
    "cache_read,input_tokens",
    [
        (5000, 100),      # 98%（既有用例的形态）
        (50_000, 50_000), # 50%
        (90_000, 10_000), # 90%
        (1_000_000, 200_000),  # 83%
        # ↓ 审计 ②-5：现场真实存在的**部分命中**（0.23~0.40）。
        # 阈值取 0.5 时这群会被误判成 near_zero_hit —— 它们是 provider
        # 真实缓存了一部分，与 cr=113/input=133k 的"名义命中"完全不同量级。
        # ⚠ 实测：0.5 ⇒ near_zero 87 条；0.05 ⇒ 77 条 ⇒ 误伤 **10** 条
        # （0.2332~0.4964 区间，非 8；审计 B2 更正）。下面三条是其中的
        # 代表值（现场实测）。
        (16_497, 54_236),   # 0.2332（现场实测值）
        (38_769, 87_009),   # 0.3082（现场实测值）
        (47_857, 80_597),   # 0.3726（现场实测值）
    ],
)
def test_real_hits_stay_green(cache_read, input_tokens):
    """放宽判据**不得**把正常命中踢出绿档（否则就是收紧红线、误伤一片）。

    末三条是现场真实的部分命中 —— 修前（阈值 0.5）它们会被误伤，
    这条参数化因此同时是"阈值下调有据"的回归锚。
    """
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=input_tokens,
                             cache_read=cache_read, cache_creation=0)
    assert r is not None
    assert r["final"] == "hit_ok", (
        f"{cache_read}/{input_tokens}（{cache_read/(cache_read+input_tokens):.2%}）"
        f"被判 {r['final']} —— 阈值过严，误伤真实命中"
    )


# ── 4. 既有语义不变（0 命中三档 + None）──────────────────────


def test_zero_hit_buckets_unchanged():
    """cache_read=0 的三档与 None 档**必须**与修前一致（本次只碰 >0 分支）。

    这是"改动作用域"的断言：Q2 若顺手改了 0 命中的分流，
    就会重犯 TEST_DSH_54 #6 已经修过的病（把 cold_start 混进 drift 桶）。
    """
    # (a) no_baseline + 0 → cold_start
    compare_and_record("a1", [
        {"role": "system", "content": "IDENTITY-V1"},
        {"role": "user", "content": "hi"},
    ], model_key=MODEL_KEY)
    r = report_cache_readout("a1", input_tokens=1000, cache_read=0,
                             cache_creation=0)
    assert r is not None and r["final"] == "cold_start"

    # (b) prefix_stable + 0 → cache_window_expired
    reset_probe()
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=1000, cache_read=0,
                             cache_creation=0)
    assert r is not None and r["final"] == "cache_window_expired"

    # (c) None → unknown_usage（未知 ≠ 零命中）
    reset_probe()
    _seed_prefix_stable()
    r = report_cache_readout("a1", input_tokens=1000, cache_read=None,
                             cache_creation=0)
    assert r is not None and r["final"] == "unknown_usage"


def test_zero_cache_read_with_zero_input_does_not_crash():
    """分母为 0 的退化输入不得抛（首请求可能尚未记 usage）。"""
    reset_probe()
    compare_and_record("a1", [
        {"role": "system", "content": "IDENTITY-V1"},
        {"role": "user", "content": "hi"},
    ], model_key=MODEL_KEY)
    r = report_cache_readout("a1", input_tokens=0, cache_read=0,
                             cache_creation=0)
    assert r is not None
    assert r["cache_hit_ratio"] == 0.0
    assert r["final"] == "cold_start"


# ── 5. 阳性对照 ─────────────────────────────────────────────


def test_positive_control_presence_judge_would_be_green():
    """阳性对照：证明**存在性判据**对现场数字会判绿 ⇒ 修前必红。

    用与旧判据逐字相同的表达式（`cache_read > 0`）跑现场数字，
    可见 old 判绿 / new 判非绿 —— 这就是"缺陷回来它会转红"的证据。

    ⚠ 审计 ③-7（2026-09-22）：本用例原来写
    ``new_green = ratio >= _HIT_OK_MIN_RATIO``（**从被测模块 import 常量**）
    —— 那是**半自证**：把阈值一起改坏时断言会跟着变绿，测不出"判据退化成
    存在性"。现改为**字面量 0.5**（= 现场定义值；`_HIT_OK_MIN_RATIO`
    自身另有 `test_threshold_constant_is_a_ratio_not_a_count` 钉住量纲），
    使本断言独立于被测常量：只有"比例 > 0.5 判绿"这个**语义**成立它才绿。
    """
    cases = [(113, 49_532), (1, 3_793_790), (500, 10_001)]
    for cache_read, input_tokens in cases:
        old_green = cache_read > 0                      # 旧判据（存在性）
        ratio = cache_read / (cache_read + input_tokens)
        new_green = ratio >= 0.5                        # 新判据（比例，字面量）
        assert old_green is True, "旧判据必须判绿（否则现场 77/77 全绿无法解释）"
        assert new_green is False, f"{cache_read}/{input_tokens} 新判据也必须非绿"


def test_ratio_includes_cache_read_in_denominator():
    """分母口径必须含 cache_read —— 不同 provider 对 input 的口径不同。

    若只除 input，则 (16000, 4000) 与 (4000, 16000) 会算出不同 ratio，
    而两者"可命中总量"其实都是 20000。判据必须对口径不敏感。
    """
    _seed_prefix_stable()
    a = report_cache_readout("a1", input_tokens=16_000, cache_read=4_000,
                             cache_creation=0)
    reset_probe()
    _seed_prefix_stable()
    b = report_cache_readout("a1", input_tokens=4_000, cache_read=16_000,
                             cache_creation=0)
    assert a is not None and b is not None
    assert a["cache_hit_ratio"] == pytest.approx(0.2)
    assert b["cache_hit_ratio"] == pytest.approx(0.8)


def test_threshold_constant_is_a_ratio_not_a_count():
    """阈值必须是**比例**（0..1）—— 防止有人日后改成一个绝对计数。"""
    assert isinstance(_HIT_OK_MIN_RATIO, float)
    assert 0.0 < _HIT_OK_MIN_RATIO <= 1.0
