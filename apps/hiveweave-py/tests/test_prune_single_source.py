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
    # ⚠ P1-1 之后边界的**可观测形态变了**：旧形态是「persisted 是 in_loop 的严格子集」；
    # 现在是「被边界挡住的候选**不计入收益** ⇒ 可能判定收益不足（空计划）」。
    # 阈值 60_000：in_loop 有两条候选（105k ≥ 60k ⇒ 取到）；persisted 只剩边界的
    # 那一条（52.5k < 60k ⇒ 收益不足 ⇒ 空）—— 这个差**正是边界的作用**。
    in_loop = select_prune_indices(messages, minimum_tokens=60_000).indices
    persisted = select_prune_indices(
        messages, minimum_tokens=60_000, stop_predicate=boundary
    ).indices
    # 对照：阈值降到边界内那一条就够（50_000 ≤ 52.5k）⇒ 非空
    # ⇒ 证明空是被**边界挡掉更旧候选**所致，不是别的原因。
    reachable = select_prune_indices(
        messages, minimum_tokens=50_000, stop_predicate=boundary
    ).indices

    assert in_loop, "无边界时应能取到候选"
    assert persisted == (), (
        "边界的可观测形态应是「更旧候选被挡 ⇒ 收益不足」；"
        f"in_loop={in_loop} persisted={persisted}"
    )
    assert reachable, "阈值可达时边界内候选仍应被裁（对照）"


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
    # P1-1 之后默认是「够用就停」（只取最新的那批）；本条测的是**顺序**语义
    # ⇒ 显式给一个「需要两条候选才够」的阈值（两候选各 ≈52.5k）：
    #   60_000 ≤ 105_000（总量）⇒ 会取到两条、且不会被判「收益不足」。
    plan = select_prune_indices(messages, minimum_tokens=60_000)
    assert len(plan.indices) >= 2, plan
    assert list(plan.indices) == sorted(plan.indices, reverse=True)

    # 审计发现：context.py 的占位符判停是 break，遮蔽了 stop 之外的路径
    # （见上一条用例）—— 这里顺带确认类型契约（不可变 tuple）
    assert isinstance(plan.indices, tuple)


# ── P1-1：改写点必须尽可能靠后（够用就停）────────────────────────


def test_p1_1_rewrite_starts_as_late_as_possible():
    """P1-1：默认阈值下只取**最靠后**的候选 —— 首个被改写下标尽可能大。

    缓存前缀的有效性终止于**首个被改写的 token**；取最旧的候选会让改写起点
    尽可能靠前 ⇒ 作废跨度最大（压缩后 `cache_read=0` 的机制之一）。
    """
    messages = [
        {"role": "system", "content": "identity"},
        *_round("c0", "a" * 210_000),   # 最旧：P1-1 之后**不该**被选
        *_round("c1", "b" * 210_000),
        *_round("c2", "recent-two"),    # 保护窗内的两轮
        *_round("c3", "recent-one"),
    ]
    default_plan = select_prune_indices(messages)
    # 「取全量」= 阈值设在总量之下但高于单条（两条各 ≈52.5k ⇒ 总量 ≈105k）
    all_plan = select_prune_indices(messages, minimum_tokens=100_000)

    assert default_plan.indices, default_plan
    assert all_plan.indices, all_plan
    # 默认证的改写起点必须**晚于**全量取法（即不碰最旧那批）
    assert min(default_plan.indices) > min(all_plan.indices), (
        f"改写起点没有后移：default={default_plan.indices} all={all_plan.indices}"
    )
    # 且默认取法选的都是全量集合里最靠后的那批（子集语义）
    assert set(default_plan.indices) <= set(all_plan.indices)


def test_p1_1_rewrite_point_never_moves_earlier_across_rounds():
    """§3.1 CI 断言 ①：prune 改写点位置**单调不早于**上一改写点。"""
    base = [
        {"role": "system", "content": "identity"},
        *_round("c0", "a" * 210_000),
        *_round("c1", "b" * 210_000),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    first = min(select_prune_indices(base).indices)
    grown = base + _round("c4", "c" * 210_000)
    second = min(select_prune_indices(grown).indices)
    assert second >= first, (
        f"改写点前移了：第一轮 {first} → 第二轮 {second}（前缀作废跨度变大）"
    )
