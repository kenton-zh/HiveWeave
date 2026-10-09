"""N3 批28-2 施工1 回归：流内退化检测器（判据 / 误伤边界 / env 钩子）。

病灶（第六轮实锤）：
- Robert 两次 out=128000 全是 token soup（「Hmm. OK. Let's act…」循环）；
- DSH 星野把 ``</longcat_tool_call>`` 标签原文当正文连吐。

验收判据：
  ① 循环样本命中 loop（短单元 a-1「tail==pattern*k」+ 长单元 a-2「tail
    在窗口 ≥5 次」两形态）；
  ② 标签泄漏命中 tag_leak（≥2 次；含跨 delta 分片自愈；单次不够）；
  ③ 正常文本不命中 —— 多组狡猾负例（代码 import 块 / 正则量词 {2,}
    列举 / 测试参数表 / 章节分隔线 / CJK 长报告）+ s24 库真实健康 run 的
    assistant 正文与 thinking 拼接（负对照，fixture 见
    tests/fixtures/degenerate_neg_control_s24.json）；
  ④ env 总开关关闭 ⇒ 零检测；循环重复次数阈值 env 可调；
  ⑥ 总累积 <4k 不启用（暖机门槛 —— 正常短输出零误伤）；
  ⑦ per-request 一次：命中即闩锁。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveweave.llm.streamer.degenerate_detect import (
    DEGENERATE_KIND_LOOP,
    DEGENERATE_KIND_TAG_LEAK,
    DegenerateDetector,
    loop_repeat_threshold,
)

FIXTURE = Path(__file__).parent / "fixtures" / "degenerate_neg_control_s24.json"


def _feed_all(detector: DegenerateDetector, text: str, chunk: int = 97) -> str | None:
    """按流式 delta 形态分片喂入（97 = 刁钻奇数尺寸，压跨片分断相位）；
    返回首次命中类别（无命中 None）。"""
    for i in range(0, len(text), chunk):
        hit = detector.feed(text[i : i + chunk])
        if hit is not None:
            return hit
    return None


def _healthy_warmup(n_lines: int = 130) -> str:
    """有变化的健康暖机文本（≥4k）：计数器/状态逐行不同 —— 本身不得命中。"""
    return "\n".join(
        f"progress note {i}: reading module_{i}, state ok, next step {i * 7 % 13}"
        for i in range(n_lines)
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """env 钩子显式回默认（防宿主环境带了开关导致假绿/假红）。"""
    monkeypatch.delenv("HIVEWEAVE_DEGENERATE_DETECT", raising=False)
    monkeypatch.delenv("HIVEWEAVE_DEGENERATE_LOOP_REPEATS", raising=False)


# ── ① 循环命中（两形态）─────────────────────────────────────────────


def test_loop_short_unit_pattern_hits():
    """短单元循环（Robert「Hmm. Hmm. …」形态）：尾部 200 字符被 ≤16 字符
    单元精确平铺（a-1）。"""
    d = DegenerateDetector()
    assert d.feed(_healthy_warmup()) is None  # 暖机 ≥4k，健康文本不命中
    hit = None
    for _ in range(20):
        hit = d.feed("Hmm. " * 20)  # 每片 100 字符纯循环
        if hit is not None:
            break
    assert hit == DEGENERATE_KIND_LOOP


def test_loop_long_unit_block_repeat_hits():
    """长单元循环（单元 41 字符 > 16，a-1 够不着）：200 字符 tail 在窗口
    内非重叠出现 ≥5 次（a-2）命中。"""
    d = DegenerateDetector()
    assert d.feed(_healthy_warmup()) is None
    unit = "Hmm. OK. Let me act on the task now. OK. "  # 42 字符
    hit = None
    for _ in range(20):
        hit = d.feed(unit)
        if hit is not None:
            break
    assert hit == DEGENERATE_KIND_LOOP


def test_loop_single_char_run_hits():
    """单字符长跑（换行/分隔符坍塌形态）也是循环：200 字符同字符平铺。"""
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("\n" * 500) == DEGENERATE_KIND_LOOP


# ── ② 标签泄漏命中 ───────────────────────────────────────────────────


def test_tag_leak_two_hits():
    """正文出现工具标签原文 ≥2 次 ⇒ tag_leak（DSH 星野形态）。"""
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("工具结果：</longcat_tool_call> 然后继续。") is None  # 1 次不够
    assert (
        d.feed("再次输出：</longcat_tool_call> 收尾") == DEGENERATE_KIND_TAG_LEAK
    )


def test_tag_leak_split_across_deltas_heals():
    """标签被网络分片切成两个 delta：窗口计数自愈（增量计数会漏检）。"""
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("前半 </longcat_tool_c") is None  # 分片标签不成形
    assert (
        d.feed("all> 后半 </:tool-call> 收尾") == DEGENERATE_KIND_TAG_LEAK
    )


def test_tag_leak_single_occurrence_not_enough():
    """单次出现可能是引用示例（低危）：阈值 ≥2 才算「连吐」。"""
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("示例：某些网关会用 </:tool-call> 这类标记。") is None


# ── ③ 正常文本不命中（狡猾负例 + s24 真实负对照）──────────────────────


def test_healthy_code_imports_no_hit():
    """代码 import 块（模块名各异）+ 函数体：良性重复不命中。"""
    code = "\n".join(
        f"from module_{i} import func_{i}, cls_{i} as C{i}" for i in range(160)
    )
    code += "\n\ndef main():\n    for i in range(160):\n        print(func_i(i))\n"
    assert _feed_all(DegenerateDetector(), code) is None


def test_healthy_regex_quantifier_listing_no_hit():
    """正则量词 {2,} 密集列举（spec 点名的狡猾负例）：模式各异 ⇒ 不命中。"""
    lines = [
        f"pat_{i}: re.compile(r\"[a-z_]{{{i % 9 + 2},}}\")  # 量词 {{2,}} 示例 {i}"
        for i in range(150)
    ]
    assert _feed_all(DegenerateDetector(), "\n".join(lines)) is None


def test_healthy_test_parameter_table_no_hit():
    """测试参数表（结构重复但值各异）：不命中。"""
    rows = [
        f"  {{'name': 'case_{i}', 'input': {i}, 'expect': {i * 3 + 1}}},"
        for i in range(220)
    ]
    table = "PARAMS = [\n" + "\n".join(rows) + "\n]\n"
    assert _feed_all(DegenerateDetector(), table) is None


def test_healthy_section_separators_no_hit():
    """章节分隔线（-----×60）穿插的健康文档：分隔线 <200 字符且两侧有变化。"""
    sections = []
    for i in range(80):
        sections.append(
            f"## Section {i}\n分析了第 {i} 模块的结构，结论编号 {i * 31}。\n"
            + "-" * 60
        )
    assert _feed_all(DegenerateDetector(), "\n\n".join(sections)) is None


def test_healthy_cjk_long_report_no_hit():
    """中文长报告（含排比句式）：措辞有变化 ⇒ 不命中。"""
    paras = [
        f"第{i}阶段：我们核对了组件{i}的行为，输入样例{i * 5}，输出符合预期"
        f"（偏差 {i % 7}）；下一步处理模块{i + 1}。"
        for i in range(150)
    ]
    assert _feed_all(DegenerateDetector(), "\n".join(paras)) is None


def test_s24_real_healthy_samples_no_hit():
    """s24（TEST_DSH_24）库健康 run 的 assistant 正文/thinking 拼接 ——
    真实负对照：全量分片 feed 全程不命中（含 39k 字符的健康 thinking 巨块）。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    samples = data["samples"]
    assert len(samples) >= 5, "fixture 样本不足（重新生成 fixtures）"
    long_seen = False
    for s in samples:
        if len(s["text"]) >= 30_000:
            long_seen = True
        det = DegenerateDetector()
        assert _feed_all(det, s["text"]) is None, (
            f"健康样本误报：agent={s['agent_prefix']} kind={s['kind']}"
        )
    assert long_seen, "须含 ≥30k 字符的长样本（thinking 巨块负对照）"


# ── ④ env 钩子：总开关 + 阈值 ────────────────────────────────────────


def test_env_master_switch_disables(monkeypatch):
    """HIVEWEAVE_DEGENERATE_DETECT=0 ⇒ 纯循环喂 10k 字符也零检测
    （关闭态零累积 —— feed 全程 no-op）。"""
    monkeypatch.setenv("HIVEWEAVE_DEGENERATE_DETECT", "0")
    d = DegenerateDetector()  # 构造时读 env
    assert _feed_all(d, "Hmm. " * 2000) is None
    assert d.total_chars == 0


def test_env_master_switch_off_aliases(monkeypatch):
    for raw in ("false", "off", "no"):
        monkeypatch.setenv("HIVEWEAVE_DEGENERATE_DETECT", raw)
        assert DegenerateDetector().feed("Hmm. " * 1000) is None


def test_env_loop_repeat_threshold_tightens(monkeypatch):
    """重复次数阈值 env 可调（作用于 a-1 的 pattern*k 判据）：min_repeats=60
    ⇒ 50 次的 4 字符单元循环不再命中 a-1；默认 8 次命中。a-2（200 字符块
    重复出现）是独立判据，阈值不作用于它。"""
    monkeypatch.setenv("HIVEWEAVE_DEGENERATE_LOOP_REPEATS", "60")
    assert loop_repeat_threshold() == 60
    # 隔离 a-1：窗口尾部 200 字符 = "abcd"×50（4 字符单元、恰好 50 次），
    # 前段有变化 ⇒ tail 在窗口只出现 1 次（a-2 不参与）。
    window = (_healthy_warmup(200) + "x")[-1800:] + "abcd" * 50
    assert len(window) >= 2000
    assert window[-200:] == "abcd" * 50
    tight = DegenerateDetector()
    assert tight._loop_hit(window) is False  # 50 < 60
    default = DegenerateDetector(min_repeats=8)
    assert default._loop_hit(window) is True  # 50 ≥ 8
    # 高阈值不误伤紧凑循环的检出：2 字符单元 ×1000（repeats=100 ≥ 60）
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("ab" * 1000) == DEGENERATE_KIND_LOOP


def test_env_loop_repeat_threshold_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_DEGENERATE_LOOP_REPEATS", "abc")
    assert loop_repeat_threshold() == 8
    monkeypatch.setenv("HIVEWEAVE_DEGENERATE_LOOP_REPEATS", "-3")
    assert loop_repeat_threshold() == 8


# ── ⑥ 暖机门槛：<4k 不启用 ──────────────────────────────────────────


def test_below_4k_warmup_not_enabled():
    """<4k 纯循环不判（正常短输出零误伤）；跨过 4k 后同流继续喂 ⇒ 命中
    （4k 是暖机线，不是对「开头就是循环」的豁免）。"""
    d = DegenerateDetector()
    assert d.feed("Hmm. " * 600) is None  # 3000 < 4096
    assert d.total_chars == 3000
    hit = None
    for _ in range(12):
        hit = d.feed("Hmm. " * 20)  # 每片 100 字符
        if hit is not None:
            break
    assert hit == DEGENERATE_KIND_LOOP


def test_text_and_thinking_accumulate_together():
    """正文与 thinking 合计过暖机线（检测器不区分来源 —— Robert 病灶正是
    thinking 巨块坍塌）。"""
    d = DegenerateDetector()
    assert d.feed("思考：先看结构。" * 375) is None  # 3000 字符 thinking
    hit = None
    for _ in range(20):
        hit = d.feed("Hmm. " * 20)  # 正文循环（每片 100 字符）
        if hit is not None:
            break
    assert hit == DEGENERATE_KIND_LOOP


# ── ⑦ per-request 一次：闩锁 ────────────────────────────────────────


def test_latched_after_first_hit():
    d = DegenerateDetector()
    d.feed(_healthy_warmup())
    assert d.feed("Hmm. " * 200) == DEGENERATE_KIND_LOOP
    # 闩锁：后续 feed 一律 None（调用方已 abort 本请求，无需再检）
    assert d.feed("</longcat_tool_call> " * 10) is None
    assert d.feed("Hmm. " * 500) is None


def test_empty_delta_and_disabled_constructor():
    d = DegenerateDetector()
    assert d.feed("") is None
    off = DegenerateDetector(enabled=False)
    assert off.feed("Hmm. " * 1000) is None
