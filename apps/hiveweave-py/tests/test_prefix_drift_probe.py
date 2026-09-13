"""P1-3 Phase 0 回归：prompt prefix drift probe（run 跨边界缓存前缀探针）。

覆盖（docs/platform-issue-research/P1-3-run-cache-prefix-plan.md 验收标准 1/2）：
1. no_baseline：agent 首个 run / reset 后无基准。
2. prefix_stable：同前缀 + history 追加（相邻 run 正常形态）→ 前缀对齐。
3. identity_drift：System1 字节变化。
4. compacted_drift：compacted 摘要变化（无压缩事件时异常漂移）。
5. history_rewritten：上次 history 不是本次前缀（中段改写）。
6. model_changed：model_id@base_url 变化（换缓存域）。
7. 多重漂移 verdict 拼接。
8. report_cache_readout 四分类：hit_ok / cold_start / cache_window_expired / drift_zero_hit，
   以及无 verdict 时 None（cold_start 为 TEST_DSH_54 #6 新增）。
9. 段切分边界：无 compacted / 无尾部 system / 空 messages。
"""

from __future__ import annotations

import pytest

from hiveweave.llm.streamer.probe import (
    compare_and_record,
    fingerprint_messages,
    report_cache_readout,
    reset_probe,
)


MODEL_KEY = "deepseek-v4@https://api.example.com/v1"


def _build_messages(
    *,
    identity: str = "IDENTITY-V1",
    compacted: str | None = None,
    history: list[dict] | None = None,
    system2: str | None = "CONTEXT-DYNAMIC",
    user: str = "hello",
) -> list[dict]:
    """按 agent._build_messages 布局构造 messages：
    [System1][System compacted?][history...][System2?][user]"""
    msgs: list[dict] = [{"role": "system", "content": identity}]
    if compacted is not None:
        msgs.append({"role": "system", "content": compacted})
    msgs.extend(history or [])
    if system2 is not None:
        msgs.append({"role": "system", "content": system2})
    msgs.append({"role": "user", "content": user})
    return msgs


@pytest.fixture(autouse=True)
def _clean_probe_state():
    reset_probe()
    yield
    reset_probe()


# ── 1. no_baseline ──────────────────────────────────────────


def test_first_run_has_no_baseline():
    msgs = _build_messages()
    v = compare_and_record("a1", msgs, model_key=MODEL_KEY)
    assert v["verdict"] == "no_baseline"
    assert v["drifts"] == []
    assert v["gap_s"] is None


# ── 2. prefix_stable（相邻 run 正常形态）────────────────────


def test_adjacent_run_with_appended_history_is_stable():
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    v1 = compare_and_record(
        "a1", _build_messages(history=h, user="q2"), model_key=MODEL_KEY
    )
    assert v1["verdict"] == "no_baseline"

    # 上一 run 的对话 append 进 history；S1/compacted/S2/user 都换新内容
    h2 = h + [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    v2 = compare_and_record(
        "a1", _build_messages(history=h2, user="q3"), model_key=MODEL_KEY
    )
    assert v2["verdict"] == "prefix_stable"
    assert v2["prev_dialog_len"] == 3  # h(2) + user q2
    assert v2["dialog_len"] == 5  # h2(4) + user q3
    assert v2["gap_s"] >= 0


def test_identical_repeat_is_stable():
    msgs = _build_messages()
    compare_and_record("a1", msgs, model_key=MODEL_KEY)
    v = compare_and_record("a1", msgs, model_key=MODEL_KEY)
    assert v["verdict"] == "prefix_stable"


# ── 3. identity_drift ───────────────────────────────────────


def test_identity_drift_detected():
    compare_and_record(
        "a1", _build_messages(identity="IDENTITY-V1"), model_key=MODEL_KEY
    )
    v = compare_and_record(
        "a1", _build_messages(identity="IDENTITY-V2"), model_key=MODEL_KEY
    )
    assert v["verdict"] == "identity_drift"
    assert v["drifts"] == ["identity_drift"]


# ── 4. compacted_drift ──────────────────────────────────────


def test_compacted_drift_detected():
    compare_and_record(
        "a1", _build_messages(compacted="SUMMARY-OLD"), model_key=MODEL_KEY
    )
    v = compare_and_record(
        "a1", _build_messages(compacted="SUMMARY-NEW"), model_key=MODEL_KEY
    )
    assert "compacted_drift" in v["drifts"]


def test_compacted_appearing_is_not_false_positive():
    """摘要从无到有（首次压缩）：识别状态翻转（"" → hash）不报
    compacted_drift——切分歧义非真实漂移；真实前缀变化由 history
    截断路径（history_rewritten）呈现。"""
    compare_and_record("a1", _build_messages(compacted=None), model_key=MODEL_KEY)
    v = compare_and_record(
        "a1", _build_messages(compacted="SUMMARY-NEW"), model_key=MODEL_KEY
    )
    assert "compacted_drift" not in v["drifts"]


# ── 5. history_rewritten ────────────────────────────────────


def test_history_rewrite_detected():
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "tool", "tool_call_id": "t1", "content": "OUTPUT-V1"},
    ]
    compare_and_record(
        "a1", _build_messages(history=h, user="next"), model_key=MODEL_KEY
    )
    # prune 式中段替换：t1 输出换成占位符 → 非前缀
    h_rewritten = [
        h[0],
        h[1],
        h[2],
        {"role": "tool", "tool_call_id": "t1", "content": "[cleared]"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "resp"},
    ]
    v = compare_and_record(
        "a1",
        _build_messages(history=h_rewritten, user="more"),
        model_key=MODEL_KEY,
    )
    assert v["verdict"] == "history_rewritten"


def test_history_shrink_detected_as_rewrite():
    """history 变短（trim/compaction 截断）也非前缀包含。"""
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    compare_and_record("a1", _build_messages(history=h, user="x"), model_key=MODEL_KEY)
    v = compare_and_record(
        "a1", _build_messages(history=h[:2], user="x"), model_key=MODEL_KEY
    )
    assert v["verdict"] == "history_rewritten"


# ── 6. model_changed ────────────────────────────────────────


def test_model_change_detected():
    compare_and_record(
        "a1",
        _build_messages(),
        model_key="deepseek-v4@https://api.example.com/v1",
    )
    v = compare_and_record(
        "a1",
        _build_messages(),
        model_key="deepseek-v4@https://other.example.com/v1",
    )
    assert v["verdict"] == "model_changed"


# ── 7. 多重漂移拼接 ─────────────────────────────────────────


def test_multiple_drifts_joined():
    h = [{"role": "user", "content": "q1"}]
    compare_and_record(
        "a1", _build_messages(compacted="S-OLD", history=h), model_key="m1@u"
    )
    v = compare_and_record(
        "a1",
        _build_messages(
            identity="IDENTITY-V2",
            compacted="S-NEW",
            history=h,
        ),
        model_key="m2@u",
    )
    # dialog 序列相同（同 history + 同 user），仅三个 system/model 段漂移
    assert v["verdict"] == "model_changed+identity_drift+compacted_drift"


# ── 8. report_cache_readout 三分类 ──────────────────────────


def test_readout_hit_ok():
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    compare_and_record("a1", _build_messages(history=h, user="q2"), model_key=MODEL_KEY)
    compare_and_record(
        "a1",
        _build_messages(history=h + [{"role": "user", "content": "q2"},
                                     {"role": "assistant", "content": "a2"}],
                        user="q3"),
        model_key=MODEL_KEY,
    )
    r = report_cache_readout(
        "a1", input_tokens=100, cache_read=5000, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "hit_ok"
    assert r["verdict"] == "prefix_stable"


def test_readout_window_expired_when_prefix_stable_but_zero_hit():
    """核心分化：前缀对齐 + cache_read=0 → provider 窗口过期（平台不可修）。"""
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    compare_and_record("a1", _build_messages(history=h, user="q2"), model_key=MODEL_KEY)
    v = compare_and_record(
        "a1",
        _build_messages(history=h + [{"role": "user", "content": "q2"},
                                     {"role": "assistant", "content": "a2"}],
                        user="q3"),
        model_key=MODEL_KEY,
        now=1000.0,
    )
    assert v["verdict"] == "prefix_stable"
    r = report_cache_readout(
        "a1", input_tokens=81958, cache_read=0, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "cache_window_expired"
    assert r["input_tokens"] == 81958


def test_readout_drift_zero_hit_when_drifted_and_zero_hit():
    """漂移 + cache_read=0 → 平台侧可修目标。"""
    compare_and_record(
        "a1", _build_messages(identity="V1"), model_key=MODEL_KEY
    )
    compare_and_record(
        "a1", _build_messages(identity="V2", user="q2"), model_key=MODEL_KEY
    )
    r = report_cache_readout(
        "a1", input_tokens=500, cache_read=0, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "drift_zero_hit"


def test_readout_none_without_verdict():
    assert report_cache_readout("ghost", input_tokens=1, cache_read=0, cache_creation=0) is None


# ── 8b. cold_start：无可读缓存域的零命中 ≠ 平台改写了前缀 ──────────
#
# report TEST_DSH_54 #6（2026-09-12）：15 个 drift_zero_hit 里有 8 个落在
# 各 Agent 首次活动窗口（12:03–12:50）—— 那里的 cache_read=0 是**必然**
# （还没有缓存可读），判词却写「平台改写了前缀，可修」，会把排查引向
# 错误根因。新增 cold_start 桶与 drift_zero_hit 分开。


def test_readout_cold_start_on_first_ever_run():
    """no_baseline（agent 首个 run / 重启后首个 run）⇒ cold_start。"""
    reset_probe()
    compare_and_record("a-cold", _build_messages(), model_key=MODEL_KEY)
    r = report_cache_readout(
        "a-cold", input_tokens=9000, cache_read=0, cache_creation=9000
    )
    assert r is not None
    assert r["verdict"] == "no_baseline"
    assert r["final"] == "cold_start", (
        "首次活动窗口的零命中是必然，不得标成 drift_zero_hit（'平台可修'）"
    )
    assert r["drifts"] == []


def test_readout_cold_start_on_model_change():
    """model_changed（换缓存域）⇒ 没有可比的缓存，同属 cold_start。"""
    reset_probe()
    compare_and_record("a-mc", _build_messages(), model_key="m1@u")
    compare_and_record("a-mc", _build_messages(), model_key="m2@u")
    r = report_cache_readout(
        "a-mc", input_tokens=9000, cache_read=0, cache_creation=0
    )
    assert r is not None
    assert r["verdict"] == "model_changed"
    assert r["final"] == "cold_start"


def test_cold_start_does_not_swallow_real_drift():
    """反向对照：真漂移仍必须落 drift_zero_hit（新桶不得吞掉可修项）。"""
    reset_probe()
    compare_and_record("a-real", _build_messages(identity="V1"), model_key=MODEL_KEY)
    compare_and_record("a-real", _build_messages(identity="V2"), model_key=MODEL_KEY)
    r = report_cache_readout(
        "a-real", input_tokens=9000, cache_read=0, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "drift_zero_hit"
    assert r["drifts"] == ["identity_drift"]


def test_readout_carries_drift_detail_for_persistence():
    """`drifts[]` 明细必须随结果返回 —— 它是落库（cache_drifts）的唯一来源。

    报告原文：『真正仍缺的只有 drifts[] 那份明细（到底哪一段前缀漂了：
    compacted_drift？history_rewritten？）』—— 不随结果带出就永远落不了库。
    """
    reset_probe()
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    compare_and_record(
        "a-detail", _build_messages(compacted="C1", history=h), model_key=MODEL_KEY
    )
    compare_and_record(
        "a-detail", _build_messages(compacted="C2", history=h, user="q2"),
        model_key=MODEL_KEY,
    )
    r = report_cache_readout(
        "a-detail", input_tokens=1, cache_read=0, cache_creation=0
    )
    assert r is not None
    assert r["final"] == "drift_zero_hit"
    assert "compacted_drift" in r["drifts"], (
        "漂移明细必须可取值 —— 否则事后答不出'漂的是哪一段'"
    )


def test_readout_is_one_shot():
    """verdict 一次性消费：空响应重试循环二次调用不重复报告。"""
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    compare_and_record("a1", _build_messages(history=h, user="q2"), model_key=MODEL_KEY)
    compare_and_record(
        "a1",
        _build_messages(history=h + [{"role": "user", "content": "q2"},
                                     {"role": "assistant", "content": "a2"}],
                        user="q3"),
        model_key=MODEL_KEY,
    )
    r1 = report_cache_readout(
        "a1", input_tokens=100, cache_read=5000, cache_creation=0
    )
    assert r1 is not None and r1["final"] == "hit_ok"
    assert (
        report_cache_readout("a1", input_tokens=1, cache_read=0, cache_creation=0)
        is None
    )


def test_clear_verdict_prevents_stale_readout():
    """compare 阶段失败后接线方调 clear_verdict → readout 不用陈旧 verdict。"""
    compare_and_record("a1", _build_messages(), model_key=MODEL_KEY)
    reset_probe("a1")  # 模拟 compare 基准丢失但 verdict 残留的场景
    # 直接构造残留 verdict：compare 成功一次（verdict= no_baseline 已存）
    compare_and_record("a1", _build_messages(), model_key=MODEL_KEY)
    from hiveweave.llm.streamer.probe import clear_verdict

    clear_verdict("a1")
    assert (
        report_cache_readout("a1", input_tokens=10, cache_read=0, cache_creation=0)
        is None
    )


def test_segment_ambiguity_flip_not_reported():
    """[S1][C][u] → [S1][C][S2][u]：run1 的 C 因无 S2 被排除（hash=""），
    run2 出现 S2 后同一 C 被计入——识别翻转不是真实漂移，不报。"""
    msgs1 = [
        {"role": "system", "content": "S1"},
        {"role": "system", "content": "SAME-COMPACTED"},
        {"role": "user", "content": "u1"},
    ]
    msgs2 = [
        {"role": "system", "content": "S1"},
        {"role": "system", "content": "SAME-COMPACTED"},
        {"role": "system", "content": "CONTEXT-DYNAMIC"},
        {"role": "user", "content": "u1"},
    ]
    compare_and_record("a1", msgs1, model_key=MODEL_KEY)
    v = compare_and_record("a1", msgs2, model_key=MODEL_KEY)
    assert v["verdict"] == "prefix_stable", v


# ── 9. 段切分边界 ────────────────────────────────────────────


def test_fingerprint_without_compacted_and_tail_system():
    msgs = [
        {"role": "system", "content": "S1"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    fp = fingerprint_messages(msgs, model_key=MODEL_KEY)
    assert fp["identity_hash"]
    assert fp["compacted_hash"] == ""  # 唯一 system = S1，无独立摘要段
    assert fp["dialog_len"] == 3


def test_fingerprint_empty_messages():
    fp = fingerprint_messages([], model_key=MODEL_KEY)
    assert fp["identity_hash"] == ""
    assert fp["compacted_hash"] == ""
    assert fp["dialog_len"] == 0


def test_single_trailing_system_not_counted_as_compacted():
    """[S1][S2][u]（history 空、无摘要）：唯一追随 system 可能是 S2 动态段，
    保守不对比（防 S2 逐轮变化误报 compacted_drift）。"""
    fp = fingerprint_messages(
        [
            {"role": "system", "content": "S1"},
            {"role": "system", "content": "CONTEXT-DYNAMIC"},
            {"role": "user", "content": "u1"},
        ],
        model_key=MODEL_KEY,
    )
    assert fp["compacted_hash"] == ""
    assert fp["dialog_len"] == 1


def test_agents_are_isolated():
    compare_and_record("a1", _build_messages(), model_key=MODEL_KEY)
    v = compare_and_record("a2", _build_messages(), model_key=MODEL_KEY)
    assert v["verdict"] == "no_baseline"  # a2 不受 a1 基准影响


# ── 10. first_mismatch_index（issue-5 §3.2 ④(b)）────────────────
#
# 为什么落这个下标：TEST_DSH_55 实测的 `history_rewritten` 是**假阳**，其
# 签名是「不一致点恒在末位」（发送版 user 消息带 exit_hint、落库版不带 ⇒
# index == prev_dialog_len-1）。落了它就能一眼区分「末位单点 ≠ 中段改写」。


def test_first_mismatch_index_is_last_position_on_tail_only_drift():
    """末位单点（本仓假阳签名）：index 必须恰为 prev_dialog_len-1。"""
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    # run1 首请求 dialog = [q1, a1, "go|exit_hint"]（末位带 in-flight 后缀）
    compare_and_record(
        "a1", _build_messages(history=h, user="go|exit_hint"), model_key=MODEL_KEY
    )
    # run2 历史里同一位置是**落库版**（无 exit_hint），随后追加了后续回合
    h2 = h + [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "done"},
    ]
    v = compare_and_record(
        "a1", _build_messages(history=h2, user="next"), model_key=MODEL_KEY
    )
    assert "history_rewritten" in v["drifts"]
    assert v["prev_dialog_len"] == 3
    assert v["first_mismatch_index"] == v["prev_dialog_len"] - 1, (
        "末位单点的下标必须落在 prev_dialog_len-1 —— 这是区分假阳的判据"
    )


def test_first_mismatch_index_points_to_middle_on_true_rewrite():
    """真·中段改写：index 必须指向被替换的那一条，而非末位。"""
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    compare_and_record("a1", _build_messages(history=h, user="x"), model_key=MODEL_KEY)
    h_mid = [
        h[0],
        {"role": "assistant", "content": "a1-REWRITTEN"},
        h[2],
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]
    v = compare_and_record(
        "a1", _build_messages(history=h_mid, user="z"), model_key=MODEL_KEY
    )
    assert "history_rewritten" in v["drifts"]
    assert v["first_mismatch_index"] == 1
    assert v["first_mismatch_index"] != v["prev_dialog_len"] - 1


def test_first_mismatch_index_when_current_is_strict_prefix():
    """cur 比 prev 短且逐条前缀相同 ⇒ 首个不一致在 cur 耗尽处 = len(cur)。"""
    compare_and_record(
        "a1",
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ],
        model_key=MODEL_KEY,
    )
    v = compare_and_record(
        "a1", [{"role": "user", "content": "q1"}], model_key=MODEL_KEY
    )
    assert "history_rewritten" in v["drifts"]
    assert v["first_mismatch_index"] == 1


def test_first_mismatch_index_none_when_stable_or_no_baseline():
    """无漂移 / 无基准 ⇒ None（不得用 0 伪装成"第 0 位不一致"）。"""
    v0 = compare_and_record("a1", _build_messages(), model_key=MODEL_KEY)
    assert v0["verdict"] == "no_baseline"
    assert v0["first_mismatch_index"] is None
    h = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    compare_and_record("a1", _build_messages(history=h, user="q2"), model_key=MODEL_KEY)
    v = compare_and_record(
        "a1",
        _build_messages(
            history=h + [{"role": "user", "content": "q2"},
                         {"role": "assistant", "content": "a2"}],
            user="q3",
        ),
        model_key=MODEL_KEY,
    )
    assert v["verdict"] == "prefix_stable"
    assert v["first_mismatch_index"] is None


def test_first_mismatch_index_survives_readout():
    """必须随 report_cache_readout 结果带出 —— 这是落库（cache_drifts）的来源。"""
    h = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    compare_and_record("a1", _build_messages(history=h, user="go|hint"), model_key=MODEL_KEY)
    compare_and_record(
        "a1",
        _build_messages(
            history=h + [{"role": "user", "content": "go"},
                         {"role": "assistant", "content": "done"}],
            user="next",
        ),
        model_key=MODEL_KEY,
    )
    r = report_cache_readout("a1", input_tokens=1, cache_read=0, cache_creation=0)
    assert r is not None
    assert r["first_mismatch_index"] == r["prev_dialog_len"] - 1


# ── 11. 落库载荷（agent.py `_cache_drifts_payload`）─────────────
#
# issue-5 §3.2 ④(b)：不改 schema，把 first_mismatch_index 放进既有
# `cache_drifts` JSON。载荷构造函数在此单测，锁死"缺陷回来会转红"。


def test_cache_drifts_payload_embeds_first_mismatch_index():
    import json

    from hiveweave.agents.agent import _cache_drifts_payload

    payload = _cache_drifts_payload(
        {"drifts": ["history_rewritten"], "first_mismatch_index": 41}
    )
    assert payload is not None
    decoded = json.loads(payload)
    assert decoded["drifts"] == ["history_rewritten"]
    assert decoded["first_mismatch_index"] == 41, (
        "first_mismatch_index 不得在下标字段缺失/被丢弃 —— 否则落库后仍答不出"
        "「末位单点 vs 中段改写」"
    )


def test_cache_drifts_payload_none_without_drifts():
    from hiveweave.agents.agent import _cache_drifts_payload

    assert _cache_drifts_payload({"drifts": [], "first_mismatch_index": None}) is None
    assert _cache_drifts_payload(None) is None
