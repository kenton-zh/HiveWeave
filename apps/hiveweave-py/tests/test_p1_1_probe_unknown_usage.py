"""P1-1②：探针不得把**未知 usage**（cache_read=None）记成「漂移、平台可修」。

旧判据 `cache_read and cache_read > 0` ⇒ None 落进 else 链 ⇒
`drift_zero_hit`「平台改写了前缀、可修」—— 与 `executed` 同族：**None 不得与 0 混同**。
（同理可见 cold_start 单列：TEST_DSH_54 的 15 条 drift_zero_hit 里 8 条是"根本没有缓存可读"。）

两格判据：① `cache_read=None` ⇒ `unknown_usage`；② `cache_read=0` + 同 verdict ⇒ `drift_zero_hit`（真漂移仍要能报）。
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


def _record_drifting_verdict(agent: str) -> None:
    """第一次建基准，第二次改变前缀 ⇒ verdict 为漂移类。"""
    reset_probe(agent)
    compare_and_record(agent, _msgs(), model_key=MODEL_KEY)
    compare_and_record(agent, _msgs("changed-prefix-" + "x" * 50), model_key=MODEL_KEY)


def test_unknown_cache_read_is_not_drift():
    _record_drifting_verdict("a-unknown")
    out = report_cache_readout(
        "a-unknown", input_tokens=1000, cache_read=None, cache_creation=0
    )
    assert out is not None
    assert out["final"] == "unknown_usage", out


def test_real_zero_hit_is_still_drift():
    """对照：同一个漂移 verdict + cache_read=**0** ⇒ 仍必须报 drift_zero_hit。"""
    _record_drifting_verdict("a-zero")
    out = report_cache_readout(
        "a-zero", input_tokens=1000, cache_read=0, cache_creation=0
    )
    assert out is not None
    assert out["final"] == "drift_zero_hit", out
