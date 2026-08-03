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
