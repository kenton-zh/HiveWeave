"""prune_persisted 只在溢出改写点回写 — 回归测试（2026-08-23 根治）。

背景：prune_persisted 原本每个 run 结束都跑，改写历史中段 → 下一 run
首请求前缀从改写点全 miss（星轨 A206 命中率 91% vs 稳态 99%）。
根治：tool loop 实际改写请求前缀（prune / hard trim / working-set 摘要）
时置 context_rewritten，completion 仅在此时把等价裁剪回写 DB。

覆盖：
- 预算内（未溢出）：不改写、不打标 → completion 不调 prune_persisted
- 溢出 prune / hard trim / 压力摘要：打标 → completion 调 prune_persisted
- store 与 streamer 的 prune 阈值不漂移（回写必须覆盖 in-loop 触发带）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hiveweave.agents.agent import Agent, AgentState
from hiveweave.conversation.store import ConversationStore
from hiveweave.conversation.token_utils import (
    PRUNE_MINIMUM_TOKENS,
    estimate_tokens_for_messages,
)
from hiveweave.llm.streamer.context import ContextMixin
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)
from tests.test_prefix_cache_append import _provider, _tight_provider


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


def _messages(old_body: str) -> list[dict]:
    return [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", old_body),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]


# ── 信号：预算内不改写 ───────────────────────────────────────


def test_under_budget_sets_no_rewrite_flag():
    """未溢出的 run：前缀 append-only，不得打改写标（否则 DB 被无谓改写）。"""
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    msgs = _messages("x" * 210_000)  # ~52k tokens，仍是 prune 候选但在预算内
    assert estimate_tokens_for_messages(msgs) < trim_at

    trimmed = ctx._trim_context_if_needed(msgs, provider)

    assert trimmed is msgs
    assert ctx._context_rewrote is False


# ── 信号：溢出改写要打标 ─────────────────────────────────────


def test_overflow_prune_sets_rewrite_flag():
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    msgs = _messages("y" * 400_000)
    assert estimate_tokens_for_messages(msgs) > trim_at

    compacted = ctx._trim_context_if_needed(msgs, provider)

    assert compacted[3]["content"] == ctx._PRUNE_PLACEHOLDER
    assert ctx._context_rewrote is True


def test_hard_trim_sets_rewrite_flag():
    """最近两轮受 prune 保护 → 只能 hard trim 丢旧轮，同样算改写。"""
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    huge = "z" * 210_000
    msgs = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", huge),
        *_round("c2", huge),
    ]
    assert estimate_tokens_for_messages(msgs) > trim_at

    trimmed = ctx._trim_context_if_needed(msgs, provider)

    assert not any(m.get("tool_call_id") == "c1" for m in trimmed)
    assert ctx._context_rewrote is True


async def test_pressure_compact_sets_rewrite_flag():
    ctx = _Ctx()
    provider = _tight_provider()
    msgs = _messages("p" * 80_000)  # 压力线(0.8×usable)以上

    async def _summarize(_transcript: str) -> str:
        return "summary"

    compacted = await ctx._pressure_compact_if_needed(
        msgs, provider, summarize=_summarize
    )

    assert estimate_tokens_for_messages(compacted) < estimate_tokens_for_messages(msgs)
    assert ctx._context_rewrote is True


# ── 阈值不漂移：回写必须覆盖 in-loop prune 触发带 ────────────


def test_store_prune_threshold_covers_streamer_band():
    """store 起闸阈值 ≤ streamer 阈值：in-loop 裁了 10k-20k 时回写不得 no-op。"""
    assert PRUNE_MINIMUM_TOKENS <= ContextMixin._PRUNE_MINIMUM_TOKENS


# ── completion 门控：未改写不回写，改写才回写 ────────────────

PROJECT_ID = "prune-flush-test-project"
AGENT_ID = "prune-flush-exec"


def _make_agent() -> Agent:
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "Exec", "role": "executor"}
    agent.status = AgentState.PROCESSING
    agent.pending_inbox_msg_ids = None
    agent.current_job = None
    agent._cancel_reason = None
    agent._streaming_msg_id = None
    agent._llm_task = None
    agent._safety_timer = None
    agent._on_status_change = None
    agent.disposition = "runnable"
    agent.empty_retry_count = 0
    agent._message_queue = []
    agent.visibility = "foreground"
    agent._workspace_path = None
    agent._current_run_id = None
    agent._resume_cooldown_until = 0.0
    agent._consecutive_errors = 0
    agent._stream_timeout_streak = 0
    agent._rate_limit_streak = 0
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._slice_budget = 0
    agent._SLICE_BUDGET_MAX = 2
    agent._progress_fingerprint = None
    agent._no_progress_streak = 0
    agent._empty_done_slice_streak = 0
    agent._on_stream_event = None
    agent._conversation = AsyncMock()
    agent._inbox = AsyncMock()
    agent._org = AsyncMock()
    agent._chat_msg = AsyncMock()
    agent._work_log = AsyncMock()
    agent._run_ledger = AsyncMock()
    return agent


def _result() -> dict:
    return {
        "status": "ok",
        "content": "done",
        "thinking": None,
        "tool_calls": [],
        "tool_turn_messages": [],
        "rounds": 1,
        "usage": None,
    }


async def _run_completion(result: dict) -> Agent:
    agent = _make_agent()
    set_pending_turn_result(AGENT_ID, {"phase": "done_slice", "summary": "done"})
    try:
        with patch(
            "hiveweave.services.task.TaskService.get_actionable_obligations",
            AsyncMock(return_value=[]),
        ), patch.object(
            Agent, "_maybe_self_retrigger", AsyncMock()
        ):
            await agent._handle_completion(result, "user msg", {})
    finally:
        clear_pending_turn_result(AGENT_ID)
    return agent


async def test_completion_without_rewrite_skips_db_prune():
    """预算内 run 结束：历史保持 append-only，DB 原文不得被占位符替换。"""
    agent = await _run_completion(_result())
    agent._conversation.prune_persisted.assert_not_awaited()


async def test_completion_with_rewrite_flushes_db_prune():
    result = _result()
    result["context_rewritten"] = True
    agent = await _run_completion(result)
    agent._conversation.prune_persisted.assert_awaited_once_with(
        AGENT_ID, PROJECT_ID
    )


# ── store.prune_persisted 语义未回归（占位符 + 保护窗） ──────


def test_store_prune_placeholder_semantics_unchanged():
    """阈值改动不动裁剪语义：候选超出保护窗即占位符化。"""
    assert ConversationStore._PRUNE_PROTECTED_TOOLS == set()
    # PRUNE_PROTECT_TOKENS 仍与 streamer 保护窗一致（40k）
    from hiveweave.conversation.token_utils import PRUNE_PROTECT_TOKENS

    assert PRUNE_PROTECT_TOKENS == ContextMixin._PRUNE_PROTECT_TOKENS


# ── 端到端：stream() 携带信号 + 跨 attempt 重置 ──────────────

_MODEL_CONFIG = {
    "base_url": "http://localhost:1",
    "api_key": "test",
    "model_id": "stub-model",
    "name": "stub",
    "provider_type": "openai",
}


def _make_streamer():
    from hiveweave.llm.streamer import Streamer

    return Streamer(max_tool_rounds=1)  # 走真实 __init__（锁定 P0 构造路径）


async def test_stream_carries_context_rewritten_signal():
    """tool loop 打标 → stream() 结果必须携带（completion 门控依赖此键）。"""
    streamer = _make_streamer()

    # 模拟 tool loop 内打标后返回
    async def _loop_and_mark(**_kwargs):
        streamer._mark_context_rewrite()
        return {"status": "ok"}

    with patch.object(streamer, "_run_tool_loop", new=_loop_and_mark):
        result = await streamer.stream(
            agent_id="a", messages=[{"role": "user", "content": "hi"}],
            model_config=_MODEL_CONFIG,
        )
    assert result["status"] == "ok"
    assert result["context_rewritten"] is True


async def test_stream_resets_stale_flag_from_previous_attempt():
    """empty 重试/failover 复用同一实例：上一 attempt 的标志不得泄漏。"""
    streamer = _make_streamer()
    streamer._context_rewrote = True  # 模拟上一 attempt 遗留脏标志

    with patch.object(
        streamer, "_run_tool_loop", new=AsyncMock(return_value={"status": "ok"})
    ):
        result = await streamer.stream(
            agent_id="a", messages=[{"role": "user", "content": "hi"}],
            model_config=_MODEL_CONFIG,
        )
    assert result["status"] == "ok"
    assert result["context_rewritten"] is False
