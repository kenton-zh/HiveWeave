"""Regression: context hard-trim must not orphan assistant(tool_calls).

TEST18: head=messages[:2] pinned system+leading assistant(tool_calls) while
trimming the matching tool results from the tail → OpenCode/DeepSeek HTTP 400.
"""

from __future__ import annotations

from hiveweave.llm.provider import ApiFormat, ProviderConfig
from hiveweave.llm.streamer.context import ContextMixin
from hiveweave.llm.streamer.core import Streamer


def _provider(*, context_window: int = 32_000, max_output: int = 8_000) -> ProviderConfig:
    # Small window forces hard trim on modest histories.
    return ProviderConfig(
        api_format=ApiFormat.OPENAI_COMPATIBLE,
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model_name="test-model",
        context_window=context_window,
        max_output_tokens=max_output,
        supports_thinking=False,
    )


def _assistant_tools(*ids: str) -> dict:
    return {
        "role": "assistant",
        "content": "calling tools",
        "tool_calls": [
            {
                "id": tid,
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }
            for tid in ids
        ],
    }


def _tool(tid: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": content}


def _assert_no_orphan_tool_calls(messages: list[dict]) -> None:
    for i, m in enumerate(messages):
        tcs = m.get("tool_calls")
        if m.get("role") != "assistant" or not isinstance(tcs, list) or not tcs:
            continue
        needed = {tc.get("id") for tc in tcs if isinstance(tc, dict) and tc.get("id")}
        found: set[str] = set()
        j = i + 1
        while j < len(messages) and (
            messages[j].get("role") == "tool" or messages[j].get("tool_call_id")
        ):
            tid = messages[j].get("tool_call_id")
            if tid:
                found.add(tid)
            j += 1
        assert needed <= found, (
            f"orphan tool_calls at idx={i}: needed={needed} found={found} "
            f"next_role={messages[i+1].get('role') if i+1 < len(messages) else None}"
        )


def test_leading_system_count():
    msgs = [
        {"role": "system", "content": "id"},
        {"role": "system", "content": "compact"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "go"},
    ]
    assert ContextMixin._leading_system_count(msgs) == 2
    assert ContextMixin._leading_system_count(msgs[2:]) == 0


def test_drop_orphan_tool_artifacts_removes_pinned_assistant():
    # Simulate old bug residue: assistant(tool_calls) then unrelated assistant
    broken = [
        {"role": "system", "content": "id"},
        _assistant_tools("c1", "c2"),
        {"role": "assistant", "content": "later turn"},
        {"role": "user", "content": "wake"},
    ]
    fixed = ContextMixin._drop_orphan_tool_artifacts(broken)
    assert fixed[0]["role"] == "system"
    assert fixed[1]["role"] == "assistant"
    assert fixed[1].get("content") == "later turn"
    assert not any(m.get("tool_calls") for m in fixed)
    _assert_no_orphan_tool_calls(fixed)


def test_drop_orphan_keeps_complete_pairs():
    msgs = [
        {"role": "system", "content": "id"},
        _assistant_tools("a1", "a2"),
        _tool("a1", "r1"),
        _tool("a2", "r2"),
        {"role": "user", "content": "next"},
    ]
    kept = ContextMixin._drop_orphan_tool_artifacts(msgs)
    assert len(kept) == 5
    _assert_no_orphan_tool_calls(kept)


def test_trim_does_not_pin_leading_assistant_tool_calls():
    """Reproduce TEST18 layout: system + history starting with tool_calls."""
    # Fat tool results so prune/trim must cut early history.
    fat = "X" * 8_000
    history = [
        _assistant_tools("call_00", "call_01"),
        _tool("call_00", fat),
        _tool("call_01", fat),
        _assistant_tools("call_10"),
        _tool("call_10", fat),
        {"role": "assistant", "content": "status summary " + ("y" * 200)},
    ]
    # Pad with more complete pairs so total exceeds trim_at
    for i in range(6):
        tid = f"pad_{i}"
        history.append(_assistant_tools(tid))
        history.append(_tool(tid, fat))

    messages = [
        {"role": "system", "content": "IDENTITY " + ("i" * 500)},
        *history,
        {"role": "system", "content": "CONTEXT " + ("c" * 500)},
        {"role": "user", "content": "## Messages\n" + ("m" * 2000)},
    ]

    # context_window=32000, max_out=8000, safety buffer → usable forces trim
    streamer = Streamer()
    trimmed = streamer._trim_context_if_needed(messages, _provider())

    assert trimmed[0]["role"] == "system"
    # Must NOT keep a leading assistant(tool_calls) without its tools
    _assert_no_orphan_tool_calls(trimmed)
    # Recent user wake should survive
    assert trimmed[-1]["role"] == "user"
    assert any(m.get("role") == "assistant" for m in trimmed)


# ── 硬裁滞回（2026-11-03 施工单 §1）：裁到 0.85 保留线，留 10% 带 ──

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.streamer.constants import (
    CONTEXT_TRIM_RETAIN_RATIO,
    CONTEXT_TRIM_TRIGGER_RATIO,
)


def _provider_big() -> ProviderConfig:
    # usable = 64_000 - 8_000 - 20_000 = 36_000（> 8192 地板，滞回带可见）
    return _provider(context_window=64_000, max_output=8_000)


def _text_history(target_tokens: int) -> list[dict]:
    """纯文本消息（无 tool 输出可 prune），细粒度自适应填充到 ~target。"""
    messages = [{"role": "system", "content": "IDENTITY " + "i" * 200}]
    filler = "w" * 1_000
    while estimate_tokens_for_messages(messages) < target_tokens:
        messages.append({"role": "assistant", "content": filler})
        messages.append({"role": "user", "content": filler})
    return messages


def test_hard_trim_targets_retain_band():
    """96% 触发硬裁 ⇒ 裁后 ≤ 0.85 保留线（而非贴着 0.95）。"""
    usable = 36_000
    trim_at = max(int(usable * CONTEXT_TRIM_TRIGGER_RATIO), 8_192)
    retain_at = max(int(usable * CONTEXT_TRIM_RETAIN_RATIO), 8_192)

    messages = _text_history(int(usable * 0.96))
    total = estimate_tokens_for_messages(messages)
    assert total > trim_at, f"sanity: {total} must exceed trigger {trim_at}"

    trimmed = Streamer()._trim_context_if_needed(messages, _provider_big())
    trimmed_tokens = estimate_tokens_for_messages(trimmed)

    assert trimmed_tokens <= retain_at, (
        f"hard trim must cut to retain band: {trimmed_tokens} > {retain_at}"
    )
    assert trimmed[0]["role"] == "system"


def test_hysteresis_band_not_trimmed():
    """retain(0.85) 与 trigger(0.95) 之间不裁——滞回带语义。"""
    usable = 36_000
    trim_at = max(int(usable * CONTEXT_TRIM_TRIGGER_RATIO), 8_192)
    retain_at = max(int(usable * CONTEXT_TRIM_RETAIN_RATIO), 8_192)

    messages = _text_history(int(usable * 0.92))
    total = estimate_tokens_for_messages(messages)
    assert retain_at < total <= trim_at, f"sanity: {total} in ({retain_at}, {trim_at}]"

    trimmed = Streamer()._trim_context_if_needed(messages, _provider_big())

    assert estimate_tokens_for_messages(trimmed) == total
    assert len(trimmed) == len(messages)


def test_hard_trim_keeps_tool_pairs_intact():
    """滞回硬裁后 tool_calls/tool_result 对仍完整（TEXT18 不变量）。"""
    usable = 36_000
    retain_at = max(int(usable * CONTEXT_TRIM_RETAIN_RATIO), 8_192)
    trim_at = max(int(usable * CONTEXT_TRIM_TRIGGER_RATIO), 8_192)

    # 大块纯文本（不可 prune）+ tool 对交错：prune 掉 tool 体后文本仍超线，
    # 强制走 _hard_trim_tail，且从 tail 前端整对裁剪。
    history: list[dict] = []
    for i in range(24):
        history.append({"role": "assistant", "content": "log " + "L" * 6_000})
        tid = f"call_{i:02d}"
        history.append(_assistant_tools(tid))
        history.append(_tool(tid, "X" * 4_000))
    messages = [
        {"role": "system", "content": "IDENTITY " + "i" * 200},
        *history,
        {"role": "user", "content": "## Messages\n" + "m" * 1_000},
    ]
    assert estimate_tokens_for_messages(messages) > trim_at

    trimmed = Streamer()._trim_context_if_needed(messages, _provider_big())

    assert estimate_tokens_for_messages(trimmed) <= retain_at
    _assert_no_orphan_tool_calls(trimmed)
