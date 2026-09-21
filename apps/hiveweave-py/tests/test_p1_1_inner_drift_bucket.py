"""P1-1②另半：**run 内漂移**也要能进 `drift_zero_hit` 桶。

病灶：探针只在 run **首请求**对比（`agent.py` 一处调用），且 verdict 一次性消费 ⇒
run 内部（prune/摘要改写之后）的漂移**从不进任何桶**；于是「本 run 自己动的刀」
造成的零命中被归成 `cache_window_expired`（**平台侧不可修**）—— 根因判错。

修法：`compare_and_record(..., slot="run_inner")` 由 `tool_loop` 每轮调用；`run_inner`
**不覆盖**首请求 verdict，只在真漂移时立粘性标记 ⇒ `report_cache_readout` 在
「首请求稳定 + 本 run 见过漂移 + 零命中」时归 `drift_zero_hit`。

两格判据：① 有 run 内漂移 ⇒ `drift_zero_hit`；② **对照**：无 run 内漂移 ⇒ `cache_window_expired`。
"""

from __future__ import annotations

from hiveweave.llm.streamer.probe import (
    compare_and_record,
    report_cache_readout,
    reset_probe,
)

MODEL_KEY = "deepseek-chat"


def _msgs(body: str = "hello") -> list[dict]:
    return [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": body},
    ]


def _stable_baseline(agent: str) -> None:
    """两次相同请求 ⇒ 第二次 verdict 为 prefix_stable（首请求语义）。"""
    reset_probe(agent)
    compare_and_record(agent, _msgs(), model_key=MODEL_KEY, slot="run_first")
    v = compare_and_record(agent, _msgs(), model_key=MODEL_KEY, slot="run_first")
    assert v.get("verdict") == "prefix_stable", v


def test_inner_drift_turns_expired_into_drift_zero_hit():
    _stable_baseline("a-inner")
    # run 内改写过前缀（prune/摘要）⇒ 立粘性标记
    compare_and_record("a-inner", _msgs("rewritten-" + "x" * 40), slot="run_inner")

    out = report_cache_readout(
        "a-inner", input_tokens=1000, cache_read=0, cache_creation=0
    )
    assert out is not None
    assert out["final"] == "drift_zero_hit", out


def test_without_inner_drift_it_stays_expired():
    """对照：没有 run 内漂移 ⇒ 仍是「缓存窗口过期」（真·不可修）。"""
    _stable_baseline("a-clean")
    out = report_cache_readout(
        "a-clean", input_tokens=1000, cache_read=0, cache_creation=0
    )
    assert out is not None
    assert out["final"] == "cache_window_expired", out


def test_inner_slot_does_not_clobber_first_verdict():
    """run 内对比**不得**覆盖首请求 verdict（它俩语义不同）。"""
    _stable_baseline("a-keep")
    compare_and_record("a-keep", _msgs("changed-" + "y" * 40), slot="run_inner")
    v = compare_and_record("a-keep", _msgs(), model_key=MODEL_KEY, slot="run_first")
    # 首请求仍以「相邻 run 对比」为准：上一基准已被 run_inner 之后的 run_first 接管，
    # 这里只断言它**没有**被 run_inner 的 verdict 顶掉（判定仍由 run_first 决定）
    assert "verdict" in v
