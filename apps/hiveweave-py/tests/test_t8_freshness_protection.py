"""T8（2026-09-29）：裁剪侧新鲜性保护 — 回归守卫。

病灶（S15 朝露，101 run 长项目实测）：会话被历史撑满后，token 预算裁剪
不断驱逐**新鲜**工具输出 → 模型永远凑不齐信息 → 同参 read_file（同文件
同 offset）重复 8+ 次死循环，每次读入又被驱逐。

修法（conversation/ 包内三处）：
1. ``token 预算裁剪``（store._trim_fresh_aware）：最近
   min(3 轮, 4 条工具结果) 的工具输出不参与驱逐——宁可多裁最老历史；
   预算连保护窗都装不下 ⇒ 溢出标志置位，get_history 触发既有 compaction
   （LLM 摘要），不硬驱逐。
2. ``Compaction._trim_to_budget``（LLM 摘要失败时的硬裁回退）：截断停在
   保护窗起点，绝不吃掉新鲜工具输出。
3. 保护窗边界不拆 assistant(tool_calls)/tool(result) 对（拆了对 =
   _clean_messages 清孤儿 = 保护形同虚设）。

既有语义保留：窗内无工具输出时（纯散文）维持旧裁剪语义
（test_clean_messages_sanitize::TestTrimKeepsSystemPrefix 不动）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hiveweave.conversation.compaction import (
    FRESH_PROTECT_MAX_TOOL_RESULTS,
    FRESH_PROTECT_TURNS,
    Compaction,
    fresh_window_has_tool_output,
    fresh_window_start,
)
from hiveweave.conversation.store import ConversationStore

from tests.test_idle_architecture_p0 import EXEC, task_env  # noqa: F401


# ── 构造器 ────────────────────────────────────────────────────────────


def _assistant(cid: str, body: str = "reading") -> dict:
    return {
        "role": "assistant",
        "content": body,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }


def _tool(cid: str, body: str) -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": body}


def _turn(i: int, size: int = 400) -> list[dict]:
    """一轮：user 提问 + assistant(tool_calls) + tool 结果。"""
    return [
        {"role": "user", "content": f"old question {i} " + "x" * size},
        _assistant(f"old-{i}", f"old answer {i} " + "y" * size),
        _tool(f"old-{i}", f"old tool out {i} " + "w" * size),
    ]


def _history(n_old: int = 6, n_fresh: int = 3) -> list[dict]:
    msgs: list[dict] = []
    for i in range(n_old):
        msgs.extend(_turn(i))
    msgs.append({"role": "user", "content": "new task"})
    for i in range(n_fresh):
        msgs.append(_assistant(f"fresh-{i}"))
        msgs.append(_tool(f"fresh-{i}", f"FRESH-RESULT-{i} " + "z" * 100))
    return msgs


def _has(msgs: list[dict], needle: str) -> bool:
    return any(needle in (m.get("content") or "") for m in msgs)


# ── 保护窗选址 ────────────────────────────────────────────────────────


class TestFreshWindow:
    def test_covers_last_three_turns(self) -> None:
        msgs = _history(n_old=6, n_fresh=0)
        msgs.extend([_assistant("f0"), _tool("f0", "r0")])
        # 逆序 3 轮 = [A(f0), T(f0)] + 旧最后两轮；窗起点不拆对
        start = fresh_window_start(msgs)
        window = msgs[start:]
        assert _is_tool_result(window[-1])
        # 窗内恰含 3 个 assistant 轮
        assert sum(1 for m in window if m.get("role") == "assistant") == (
            FRESH_PROTECT_TURNS
        )
        # 边界不拆对：窗首若为 tool 结果，其前一条不能是它的 owning assistant
        if _is_tool_result(window[0]):
            assert not (window[0].get("tool_call_id") and start > 0
                        and msgs[start - 1].get("tool_calls"))

    def test_tool_result_limit_binds_first(self) -> None:
        """单轮多条工具结果：K=4 上限先到 ⇒ 窗只盖 2 轮（取小值防爆量）。"""
        msgs: list[dict] = []
        for i in range(4):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append(_assistant(f"a{i}"))
            msgs.append(_tool(f"a{i}-1", f"r{i}-1"))
            msgs.append(_tool(f"a{i}-2", f"r{i}-2"))
        start = fresh_window_start(msgs)
        window = msgs[start:]
        tool_results = [m for m in window if _is_tool_result(m)]
        assert len(tool_results) == FRESH_PROTECT_MAX_TOOL_RESULTS
        # 窗内只有 2 个 assistant 轮（3 轮上限未触顶）
        assert (
            sum(1 for m in window if m.get("role") == "assistant") == 2
        )

    def test_no_tool_outputs_reports_false(self) -> None:
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ] * 4
        assert fresh_window_has_tool_output(msgs) is False

    def test_window_start_never_splits_pair(self) -> None:
        """窗起点落在 tool result 上时，owning assistant 必须一并进窗。"""
        msgs: list[dict] = [{"role": "user", "content": "q"}]
        for i in range(6):
            msgs.append(_assistant(f"c{i}"))
            msgs.append(_tool(f"c{i}", f"r{i}"))
        start = fresh_window_start(msgs)
        first = msgs[start]
        if _is_tool_result(first):
            owner = msgs[start - 1]
            # owning assistant 不得留在窗外
            tcs = owner.get("tool_calls") or []
            owner_ids = {
                tc.get("id") for tc in tcs if isinstance(tc, dict)
            }
            assert first.get("tool_call_id") not in owner_ids or (
                owner.get("tool_calls") and False
            ), "pair split: assistant in head, its result in window"


def _is_tool_result(m: object) -> bool:
    return isinstance(m, dict) and bool(
        m.get("tool_call_id") or m.get("role") == "tool"
    )


# ── token 预算裁剪的新鲜性保护 ────────────────────────────────────────


class TestTrimFreshAware:
    def test_fresh_tool_result_survives_oldest_trimmed(self) -> None:
        """超预算：最老历史被裁、新鲜工具结果存活。"""
        msgs = _history(n_old=6, n_fresh=3)
        trimmed, overflow = ConversationStore._trim_fresh_aware(msgs, 900)
        assert overflow is False
        # 最老历史被裁
        assert not _has(trimmed, "old question 0")
        # 三条新鲜工具结果全部存活
        for i in range(3):
            assert _has(trimmed, f"FRESH-RESULT-{i}")

    def test_extreme_budget_signals_compaction_keeps_window(self) -> None:
        """极端小预算（连保护窗都装不下）：不硬驱逐，置溢出标志。"""
        msgs = _history(n_old=6, n_fresh=3)
        trimmed, overflow = ConversationStore._trim_fresh_aware(msgs, 10)
        assert overflow is True
        # 新鲜工具输出一个都没动
        for i in range(3):
            assert _has(trimmed, f"FRESH-RESULT-{i}")
        # 最老历史已裁光（能裁的都裁了）
        assert not _has(trimmed, "old question 0")

    def test_no_tool_outputs_keeps_legacy_semantics(self) -> None:
        """窗内无工具输出 ⇒ 保护空转，维持既有裁剪语义（钉死旧测试）。"""
        msgs: list[dict] = [
            {"role": "system", "content": "你是 CEO，身份+工具定义"},
        ]
        for i in range(5):
            msgs.append({"role": "user", "content": f"任务{i}"})
            msgs.append({"role": "assistant", "content": f"处理{i}"})
        out, overflow = ConversationStore._trim_fresh_aware(msgs, 1)
        assert overflow is False
        assert out[0] == {"role": "system", "content": "你是 CEO，身份+工具定义"}
        non_system = [m for m in out if m.get("role") != "system"]
        assert len(non_system) <= 1

    def test_trim_to_budget_wraps_fresh_aware(self) -> None:
        """_trim_to_budget 兼容签名不变：只返回列表。"""
        msgs = _history()
        out = ConversationStore._trim_to_budget(msgs, 10_000_000)
        assert out == msgs
        out2 = ConversationStore._trim_to_budget(msgs, None)
        assert out2 == msgs

    def test_pair_not_split_by_head_fitting(self) -> None:
        """head 侧按 turn 整块塞入：assistant(tool_calls) 不与其结果分离。"""
        msgs = _history(n_old=6, n_fresh=3)
        trimmed, _ = ConversationStore._trim_fresh_aware(msgs, 900)
        # 每条 assistant(tool_calls) 的调用 id 都有对应 tool 结果在场
        ids = {
            tc.get("id")
            for m in trimmed
            for tc in (m.get("tool_calls") or [])
            if isinstance(tc, dict)
        }
        result_ids = {
            m.get("tool_call_id") for m in trimmed if _is_tool_result(m)
        }
        assert ids <= result_ids, "assistant(tool_calls) 与结果被拆散"


# ── Compaction 硬裁回退尊重保护窗 ─────────────────────────────────────


class TestCompactionFallback:
    def test_hard_trim_fallback_never_eats_fresh_window(self) -> None:
        """LLM 摘要失败的硬裁回退：截断停在保护窗起点。"""
        msgs = _history(n_old=6, n_fresh=3)
        out = Compaction._trim_to_budget(msgs, 900)
        assert not _has(out, "old question 0"), "最老历史应被裁"
        for i in range(3):
            assert _has(out, f"FRESH-RESULT-{i}"), "新鲜工具输出被硬裁吃掉"

    def test_prose_history_keeps_legacy_trim(self) -> None:
        """纯散文历史（无工具输出）：回退维持旧语义（裁到最少）。"""
        msgs: list[dict] = []
        for i in range(10):
            msgs.append({"role": "user", "content": f"q{i} " + "x" * 200})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 200})
        out = Compaction._trim_to_budget(msgs, 50)
        assert len(out) < len(msgs)

    def test_budget_zero_keeps_old_semantics(self) -> None:
        """budget<=0 的守卫分支不变。"""
        msgs = _history(n_old=1, n_fresh=1)
        out = Compaction._trim_to_budget(msgs, 0)
        assert len(out) == 2


# ── get_history 联动：保护窗溢出 ⇒ 触发既有 compaction ────────────────


@pytest.mark.asyncio
async def test_get_history_triggers_compaction_on_window_overflow(task_env):
    """极端小预算 get_history：不硬驱逐 + 入队一次 _do_compaction。"""
    pid = task_env["project_id"]
    store = ConversationStore()
    msgs = _history(n_old=6, n_fresh=3)
    store._cache[(pid, EXEC)] = list(msgs)

    enqueue = AsyncMock()
    store._enqueue_write = enqueue  # type: ignore[method-assign]

    out = await store.get_history(EXEC, pid, token_budget=10)

    # 新鲜工具输出在本请求中存活（模型本轮仍能看到刚读到的内容）
    for i in range(3):
        assert _has(out, f"FRESH-RESULT-{i}")
    # 压缩已入队（去重/冷却/写队列语义沿用 _maybe_trigger_compaction）
    assert enqueue.await_count == 1
    partial = enqueue.await_args.args[1]
    assert partial.func.__name__ == "_do_compaction"
    # 目标预算 = 触发裁剪的那个 budget
    assert partial.args[4] == 10
    # 入队标记已置位（防同轮重复入队）
    assert (pid, EXEC) in store._compaction_pending


@pytest.mark.asyncio
async def test_get_history_cooldown_prevents_trigger_storm(task_env):
    """压缩待执行/冷却期内反复 get_history 不再重复入队。"""
    pid = task_env["project_id"]
    store = ConversationStore()
    store._cache[(pid, EXEC)] = _history(n_old=6, n_fresh=3)
    enqueue = AsyncMock()
    store._enqueue_write = enqueue  # type: ignore[method-assign]

    await store.get_history(EXEC, pid, token_budget=10)
    assert enqueue.await_count == 1
    # pending 未清（_do_compaction 尚未跑）⇒ 再读不重入队
    await store.get_history(EXEC, pid, token_budget=10)
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_get_history_no_trigger_when_budget_fits(task_env):
    """预算装得下（含保护窗）⇒ 不触发 compaction。"""
    pid = task_env["project_id"]
    store = ConversationStore()
    store._cache[(pid, EXEC)] = _history(n_old=1, n_fresh=1)
    enqueue = AsyncMock()
    store._enqueue_write = enqueue  # type: ignore[method-assign]

    out = await store.get_history(EXEC, pid, token_budget=10_000_000)
    assert enqueue.await_count == 0
    assert out == store._cache[(pid, EXEC)]


# ── _maybe_trigger_compaction：budget_override 旁路 ──────────────────


@pytest.mark.asyncio
async def test_budget_override_bypasses_window_check(task_env):
    """budget_override 显式预算：跳过 70% 窗口线，直接入队。"""
    pid = task_env["project_id"]
    store = ConversationStore()
    # 小消息量——窗口线检查永远不会放行，override 必须旁路它
    small = [{"role": "user", "content": "tiny"}]

    async def fake_ctx(_agent_id: str) -> int:
        return 128_000

    store._get_agent_context_window = fake_ctx  # type: ignore[method-assign]
    store._enqueue_write = AsyncMock()  # type: ignore[method-assign]

    await store._maybe_trigger_compaction(
        EXEC, pid, (pid, EXEC), small, budget_override=42
    )
    assert store._enqueue_write.await_count == 1
    partial = store._enqueue_write.await_args.args[1]
    assert partial.args[4] == 42  # 目标预算逐字传递

    # 无 override 时同消息量不触发（守卫：override 确实是旁路而不是直通）
    store._compaction_pending.discard((pid, EXEC))
    store._last_compaction_ts[(pid, EXEC)] = 0.0
    store._enqueue_write.reset_mock()
    await store._maybe_trigger_compaction(EXEC, pid, (pid, EXEC), small)
    assert store._enqueue_write.await_count == 0
