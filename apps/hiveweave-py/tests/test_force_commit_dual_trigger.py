"""E14 双触发判定（e14_steering_decision）单元测试。

07 实测：纯轮数疏导线在现实预算下永不触发（~52s/轮，40 轮 ≈35min >>
1710s 硬顶，.env 一度被调到 400 当静音）——疏导两阶段（提示 → 宽限 →
优雅收口）全部死于墙钟闸口之前。修正为「轮数线 OR 累计墙钟线」双触发：
轮数线抓快轮 churn，墙钟线（硬预算 85%）抓慢轮无界磨。

调用方契约（审计[1]）：e14 判定必须位于循环体内**轮闸检查之前**——否则
越过墙钟宽限点后的轮边界会被轮闸（MIN_ROUND_BUDGET_S）先行收走，疏导的
note/归因全部漂移成 hard_budget，宽限期名存实亡。
"""

from __future__ import annotations

import pytest

from hiveweave.llm.streamer.tool_loop import e14_steering_decision

HARD = 1710.0  # 07 的 .env 硬预算
# 与 fixture 锁定的阈值一致（.env 的 FORCE_COMMIT_ROUNDS=400 是历史静音值，
# fixture 已 patch 回 40/8/0.85——测试算术必须用同一套数字）
ROUNDS = 40
GRACE_ROUNDS = 8
WALL_AT = HARD * 0.85  # 1453.5s
GRACE_WALL = 120.0  # 名义墙钟宽限


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch):
    """锁定默认阈值，隔离用户 .env 的覆盖（如 FORCE_COMMIT_ROUNDS=400）。

    helper 读的是 tool_loop 的模块全局（import 时绑定），patch 目标是
    tool_loop 模块属性。
    """
    tl = "hiveweave.llm.streamer.tool_loop"
    monkeypatch.setattr(f"{tl}.FORCE_COMMIT_ROUNDS", ROUNDS)
    monkeypatch.setattr(f"{tl}.FORCE_COMMIT_GRACE_ROUNDS", GRACE_ROUNDS)
    monkeypatch.setattr(f"{tl}.FORCE_COMMIT_WALL_PCT", 0.85)


def test_below_both_lines_is_none():
    d, t = e14_steering_decision(
        round_num=10, elapsed_s=500.0, hard_budget_s=HARD, hint_injected=False
    )
    assert (d, t) == ("none", "-")


def test_rounds_line_triggers_hint():
    d, t = e14_steering_decision(
        round_num=ROUNDS, elapsed_s=300.0,
        hard_budget_s=HARD, hint_injected=False,
    )
    assert (d, t) == ("hint", "rounds")


def test_rounds_grace_exceeded_forces():
    d, t = e14_steering_decision(
        round_num=ROUNDS + GRACE_ROUNDS,
        elapsed_s=300.0, hard_budget_s=HARD, hint_injected=False,
    )
    assert (d, t) == ("force", "rounds")


def test_wall_line_triggers_hint_even_with_few_rounds():
    """07 主场景：慢轮无界磨——轮数远不到 40，但墙钟越线即疏导。"""
    d, t = e14_steering_decision(
        round_num=12, elapsed_s=WALL_AT + 5.0,
        hard_budget_s=HARD, hint_injected=False,
    )
    assert (d, t) == ("hint", "wall_clock")


def test_wall_grace_exceeded_forces():
    """墙钟宽限（名义 120s）已过 → force/wall_clock。"""
    d, t = e14_steering_decision(
        round_num=14,
        elapsed_s=WALL_AT + GRACE_WALL + 1.0,
        hard_budget_s=HARD, hint_injected=False,
    )
    assert (d, t) == ("force", "wall_clock")


def test_force_label_follows_actual_grace_line():
    """审计[6]：两线同越但仅墙钟宽限先到 → force 标签必须报 wall_clock
    （churn 信号与墙钟信号在遥测里要可区分）。"""
    d, t = e14_steering_decision(
        round_num=42,  # rounds 命中但宽限（48 轮）未到
        elapsed_s=WALL_AT + GRACE_WALL + 1.0,  # 墙钟宽限已过
        hard_budget_s=HARD, hint_injected=False,
    )
    assert (d, t) == ("force", "wall_clock")


def test_hint_injected_suppresses_second_hint_during_grace():
    d, t = e14_steering_decision(
        round_num=ROUNDS + 2, elapsed_s=WALL_AT + 30.0,
        hard_budget_s=HARD, hint_injected=True,
    )
    assert d == "none"  # 宽限中不重复注入


def test_constants_env_tunable_docs():
    from hiveweave.llm.streamer import constants as c

    assert 0 < c.FORCE_COMMIT_WALL_PCT < 1
    assert c.FORCE_COMMIT_GRACE_WALL_S > 0


# ── 39 审计 P2-1：gate_reject 正交归类（演练语义：被拒 = 成功）──────────


def test_all_submit_errors_classified_gate_reject():
    from hiveweave.llm.streamer.doom_loop import STALL_REASON_GATE_REJECT
    from hiveweave.llm.streamer.doom_loop import classify_stall_round

    tool_calls = [
        {"id": "t1", "name": "submit_task", "arguments": "{}"},
        {"id": "t2", "name": "submit_task", "arguments": "{}"},
    ]
    d = classify_stall_round(
        tool_calls,
        error_ids={"t1", "t2"},
        blocked_ids=set(),
        duplicate_ids=set(),
        seen_readonly_fingerprints=set(),
    )
    assert d == STALL_REASON_GATE_REJECT


def test_mixed_errors_still_tool_failed():
    from hiveweave.llm.streamer.doom_loop import STALL_REASON_TOOL_FAILED
    from hiveweave.llm.streamer.doom_loop import classify_stall_round

    tool_calls = [
        {"id": "t1", "name": "submit_task", "arguments": "{}"},
        {"id": "t2", "name": "bash", "arguments": "{}"},
    ]
    d = classify_stall_round(
        tool_calls,
        error_ids={"t1", "t2"},
        blocked_ids=set(),
        duplicate_ids=set(),
        seen_readonly_fingerprints=set(),
    )
    assert d == STALL_REASON_TOOL_FAILED


def test_no_error_round_not_gate_reject():
    from hiveweave.llm.streamer.doom_loop import STALL_REASON_GATE_REJECT
    from hiveweave.llm.streamer.doom_loop import classify_stall_round

    tool_calls = [{"id": "t1", "name": "submit_task", "arguments": "{}"}]
    d = classify_stall_round(
        tool_calls,
        error_ids=set(),  # submit 成功（未被拒）
        blocked_ids=set(),
        duplicate_ids=set(),
        seen_readonly_fingerprints=set(),
    )
    assert d != STALL_REASON_GATE_REJECT
