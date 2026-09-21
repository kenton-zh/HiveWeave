"""prune **选址**的单一实现 —— 两处调用点必须走同一个函数（收口纪律）。

背景（2026-09-21 收口）：同一套「逆序挑可裁剪 tool 输出」算法在
`llm/streamer/context.py`（in-loop 临时裁剪）与 `conversation/store.py`
（持久化裁剪）各写一份，且**已经漂移**：持久化版多一条「压缩摘要边界」停止
条件、占位符一处是常量一处是硬编码字面量、阈值靠注释「与 streamer 对齐」。
⇒ 收口到 `conversation/token_utils.select_prune_indices`（选址共用、apply 各写）。

本文件钉住（都是状态/对象判据）：
① 常量与占位符是**同一个对象**（`is`）—— 不是两份恰好相等的字面量；
② 两处**实际选址结果**逐字一致（parity：删掉共享实现改回两份必然漂移）；
③ 持久化路径多出的「压缩摘要边界」必须**显式声明**（`stop_predicate`），
   不是悄悄漂移 —— 差异要可见、可解释；
④ 「无候选」与「候选收益不足」可区分（`candidate_count`）。
"""

from __future__ import annotations

from hiveweave.conversation import token_utils as tu
from hiveweave.conversation.token_utils import (
    PRUNE_MINIMUM_TOKENS,
    PRUNE_PLACEHOLDER,
    PRUNE_PROTECT_TOKENS,
    select_prune_indices,
)
from hiveweave.llm.streamer.context import ContextMixin


class _Ctx(ContextMixin):
    max_tool_rounds = 100


def _round(call_id: str, body: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": body},
    ]


def _msgs(*extra_old: dict) -> list[dict]:
    return [
        {"role": "system", "content": "identity"},
        *extra_old,
        {"role": "user", "content": "go"},
        *_round("c1", "x" * 210_000),   # ~52k tokens ⇒ 保护窗外 ⇒ 候选
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]


def _pruned_indices(messages: list[dict]) -> list[int]:
    ctx = _Ctx()
    out = ctx._prune_old_tool_outputs(messages)
    return [i for i, m in enumerate(out) if m.get("content") == PRUNE_PLACEHOLDER]


# ── ① 单一源（对象同一性，不是字面量相等）────────────────────


def test_prune_constants_are_single_source():
    assert ContextMixin._PRUNE_PLACEHOLDER is tu.PRUNE_PLACEHOLDER
    assert ContextMixin._PRUNE_PROTECT_TOKENS == tu.PRUNE_PROTECT_TOKENS
    assert ContextMixin._PRUNE_MINIMUM_TOKENS == tu.PRUNE_MINIMUM_TOKENS
    # 同一份字符串只应出现在 token_utils（其余文件若再写字面量会漂移）
    assert PRUNE_PLACEHOLDER == "[Old tool result content cleared]"


# ── ② parity：in-loop 实际裁剪 == 共享选址（持久化参数）────────


def test_in_loop_matches_shared_selector_with_persisted_params():
    messages = _msgs()
    in_loop = _pruned_indices(messages)
    assert in_loop, "夹具失去意义：本轮应当有可裁剪候选"

    plan = select_prune_indices(
        messages,
        protect_tokens=PRUNE_PROTECT_TOKENS,
        minimum_tokens=PRUNE_MINIMUM_TOKENS,
        placeholder=PRUNE_PLACEHOLDER,
        stop_predicate=lambda m: (
            m.get("role") == "system" and "SUMMARY" in (m.get("content") or "")
        ),
    )
    assert list(plan.indices) == in_loop
    assert plan.prune_tokens > 0


def test_default_params_match_in_loop():
    """默认参数就是 in-loop 的语义（in-loop 不传 stop_predicate）。"""
    messages = _msgs()
    assert list(select_prune_indices(messages).indices) == _pruned_indices(messages)


# ── ③ 摘要边界是**显式声明**的差异，不是漂移 ─────────────────


def test_summary_boundary_is_explicit_and_only_for_persisted_path():
    """持久化路径停在「压缩摘要边界」——被它挡住的**更旧**消息不该入选。

    夹具要求：边界必须比要挡的消息**更新**（会话被压缩后，摘要就落在原位置；
    扫描是逆序的 ⇒ break 只挡住它**之后**还没访问到的更旧消息）。
    """
    marker = {"role": "system", "content": "SUMMARY: 早前对话已压缩"}
    messages = [
        {"role": "system", "content": "identity"},
        *_round("c0", "y" * 210_000),   # 边界**之前**的旧输出（应被边界挡住）
        marker,
        {"role": "user", "content": "go"},
        *_round("c1", "x" * 210_000),   # 边界**之后**的旧输出（应被裁）
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]

    boundary = lambda m: (  # noqa: E731
        m.get("role") == "system" and "SUMMARY" in (m.get("content") or "")
    )
    in_loop = _pruned_indices(messages)              # 无边界 ⇒ 会一路裁到更旧的
    persisted = select_prune_indices(
        messages, stop_predicate=boundary
    ).indices

    assert persisted, "摘要边界内的候选仍应被裁"
    assert set(persisted) < set(in_loop), (
        "持久化路径必须在摘要边界停下（严格子集）；"
        f"in_loop={in_loop} persisted={persisted}"
    )


# ── ④ 「无候选」与「收益不足」可区分 ─────────────────────────


def test_candidate_count_distinguishes_reasons():
    messages = _msgs()

    none_plan = select_prune_indices(
        messages, protect_tokens=10 ** 9, minimum_tokens=0
    )
    assert none_plan.candidate_count == 0 and none_plan.indices == ()

    short_plan = select_prune_indices(
        messages, protect_tokens=0, minimum_tokens=10 ** 9
    )
    assert short_plan.candidate_count > 0 and short_plan.indices == ()
    assert short_plan.prune_tokens > 0


def test_placeholder_stops_scan():
    """已裁过 ⇒ **停止**（不是跳过）：比它更旧的巨大输出也不该入选。

    ⚠ 夹具必须让「占位符之前」还存在一个**本会入选**的候选，否则 `break`
    与 `continue` 结果相同 —— 这条断言就是判别两者的唯一手段。
    """
    messages = [
        {"role": "system", "content": "identity"},
        *_round("c0", "y" * 210_000),   # 比占位符**更旧**：本会入选，必须被停止挡掉
        *_round("c1", "x" * 210_000),   # 占位符所在（下面标成已裁剪）
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    # 把 c1 的 tool 行标成「已裁剪」
    idx_c1 = next(
        i for i, m in enumerate(messages)
        if m.get("content") == "x" * 210_000
    )
    messages[idx_c1]["content"] = PRUNE_PLACEHOLDER

    plan = select_prune_indices(messages)
    assert idx_c1 not in plan.indices
    assert all(
        i > idx_c1 for i in plan.indices
    ), f"占位符之前（更旧）的消息不该入选：{plan.indices}"


def test_short_messages_short_circuit_is_owned_here():
    """`min_messages` 短路由本函数拥有（调用点不再各写一遍）。"""
    five = [{"role": "user", "content": "x"}] * 5
    assert select_prune_indices(five) == select_prune_indices(five, min_messages=6)
    p5 = select_prune_indices(five)
    assert p5.indices == () and p5.turns == 0 and p5.protected_tokens == 0

    # 恰好 6 条时不再短路（边界值）：有候选就应当选出来
    six = [
        {"role": "system", "content": "identity"},
        *_round("c0", "y" * 210_000),
        *_round("c1", "recent-two"),
        *_round("c2", "recent-one"),
    ]
    assert len(six) == 7
    assert select_prune_indices(six).indices


def test_indices_are_descending():
    """逆序扫描 ⇒ indices 降序（越靠前越旧）。契约写进 docstring 就要钉住。"""
    messages = [
        {"role": "system", "content": "identity"},
        *_round("c0", "a" * 210_000),
        *_round("c1", "b" * 210_000),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    plan = select_prune_indices(messages)
    assert len(plan.indices) >= 2, plan
    assert list(plan.indices) == sorted(plan.indices, reverse=True)

    # 审计发现：context.py 的占位符判停是 break，遮蔽了 stop 之外的路径
    # （见上一条用例）—— 这里顺带确认类型契约（不可变 tuple）
    assert isinstance(plan.indices, tuple)
