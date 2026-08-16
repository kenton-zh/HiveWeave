"""Tool-loop requests must append-extend (DeepSeek prefix cache / DSH KV).

DSH request-reconstruction: each step's messages are a strict prefix of the
next unless a logged compaction replacement explains the difference.
HiveWeave's in-loop prune/strip used to rewrite the middle every round.
"""
from __future__ import annotations

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.provider import ApiFormat, ProviderConfig
from hiveweave.llm.streamer.context import ContextMixin
from tests.test_context_trim_tool_pairs import _assert_no_orphan_tool_calls


class _Ctx(ContextMixin):
    max_tool_rounds = 100


def _provider() -> ProviderConfig:
    return ProviderConfig(
        api_format=ApiFormat.OPENAI_COMPATIBLE,
        base_url="http://localhost",
        api_key="test",
        model_name="deepseek-test",
        context_window=128_000,
        max_output_tokens=8_192,
        supports_thinking=False,
    )


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


def _expect_prefix_extension(previous: list[dict], current: list[dict]) -> None:
    assert len(current) > len(previous)
    assert current[: len(previous)] == previous


def test_under_budget_does_not_replace_old_tool_bodies():
    """Sliding-window prune used to fire well below trim_at and pin hits at ~50%."""
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    old_body = "x" * 210_000  # ~52k tokens — prune candidate, still under trim_at
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", old_body),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    total = estimate_tokens_for_messages(messages)
    assert total < trim_at
    pruned = ctx._prune_old_tool_outputs(messages)
    assert pruned[3]["content"] == ctx._PRUNE_PLACEHOLDER

    trimmed = ctx._trim_context_if_needed(messages, provider)
    assert trimmed[3]["content"] == old_body
    assert trimmed is messages


def test_each_step_within_a_turn_append_extends_the_previous():
    ctx = _Ctx()
    provider = _provider()
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", "one"),
    ]
    first = ctx._trim_context_if_needed(messages, provider)
    second_src = first + _round("c2", "two")
    second = ctx._trim_context_if_needed(second_src, provider)
    _expect_prefix_extension(first, second)
    third_src = second + _round("c3", "three")
    third = ctx._trim_context_if_needed(third_src, provider)
    _expect_prefix_extension(second, third)


def test_strip_images_does_not_rewrite_under_budget():
    ctx = _Ctx()
    provider = _provider()
    img = {"media_type": "image/png", "data": "aaaa", "path": "/tmp/a.png"}
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", "shot-1"),
        *_round("c2", "shot-2"),
        *_round("c3", "shot-3"),
    ]
    messages[3]["images"] = [img]
    messages[5]["images"] = [img]
    messages[7]["images"] = [img]
    trimmed = ctx._trim_context_if_needed(messages, provider)
    assert "images" in trimmed[3]
    assert "[image stripped" not in (trimmed[3].get("content") or "")
    assert trimmed[3]["content"] == "shot-1"


def test_overflow_compaction_rewrites_once_then_append_extends():
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    old_body = "y" * 400_000  # ~100k tokens → over trim_at, prune should save it
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", old_body),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    assert estimate_tokens_for_messages(messages) > trim_at
    compacted = ctx._trim_context_if_needed(messages, provider)
    assert compacted[3]["content"] == ctx._PRUNE_PLACEHOLDER
    assert estimate_tokens_for_messages(compacted) <= trim_at
    _assert_no_orphan_tool_calls(compacted)

    nxt = compacted + _round("c4", "after-compact")
    again = ctx._trim_context_if_needed(nxt, provider)
    _expect_prefix_extension(compacted, again)
    assert again[3]["content"] == ctx._PRUNE_PLACEHOLDER
    _assert_no_orphan_tool_calls(again)


def test_hard_trim_then_append_extends():
    """Last two rounds are prune-protected; overflow must drop older turns once."""
    ctx = _Ctx()
    provider = _provider()
    _, trim_at = ctx._input_trim_at(provider)
    huge = "z" * 210_000  # ~52k tokens each; two recent rounds skip prune
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", huge),
        *_round("c2", huge),
    ]
    assert estimate_tokens_for_messages(messages) > trim_at
    assert ctx._prune_old_tool_outputs(messages)[3]["content"] == huge
    trimmed = ctx._trim_context_if_needed(messages, provider)
    assert estimate_tokens_for_messages(trimmed) <= trim_at
    assert trimmed[0]["content"] == "identity"
    assert not any(m.get("tool_call_id") == "c1" for m in trimmed)
    assert any(m.get("tool_call_id") == "c2" for m in trimmed)
    _assert_no_orphan_tool_calls(trimmed)
    nxt = trimmed + _round("c3", "after-hard-trim")
    again = ctx._trim_context_if_needed(nxt, provider)
    _expect_prefix_extension(trimmed, again)
    _assert_no_orphan_tool_calls(again)


def _tight_provider() -> ProviderConfig:
    """Small window so trim_at is 8192 — image overflow without 100k-char bodies."""
    return ProviderConfig(
        api_format=ApiFormat.OPENAI_COMPATIBLE,
        base_url="http://localhost",
        api_key="test",
        model_name="deepseek-test",
        context_window=30_000,
        max_output_tokens=8_192,
        supports_thinking=False,
    )


def test_overflow_strips_oldest_image_once_then_append_extends():
    ctx = _Ctx()
    provider = _tight_provider()
    _, trim_at = ctx._input_trim_at(provider)
    blob = "B" * 1_920_000  # ~2812 image tokens; three of them exceed 8192
    def _img(path: str) -> dict:
        return {"media_type": "image/png", "data": blob, "path": path}

    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", "shot-1"),
        *_round("c2", "shot-2"),
        *_round("c3", "shot-3"),
    ]
    messages[3]["images"] = [_img("/tmp/1.png")]
    messages[5]["images"] = [_img("/tmp/2.png")]
    messages[7]["images"] = [_img("/tmp/3.png")]
    assert estimate_tokens_for_messages(messages) > trim_at

    compacted = ctx._trim_context_if_needed(messages, provider)
    assert estimate_tokens_for_messages(compacted) <= trim_at
    _assert_no_orphan_tool_calls(compacted)
    assert "images" not in compacted[3]
    assert "[image stripped" in (compacted[3].get("content") or "")
    assert compacted[3]["content"].count("[image stripped") == 1
    assert compacted[5].get("images")
    assert compacted[7].get("images")
    assert "[image stripped" not in (compacted[5].get("content") or "")
    assert "[image stripped" not in (compacted[7].get("content") or "")

    nxt = compacted + _round("c4", "shot-4")
    again = ctx._trim_context_if_needed(nxt, provider)
    _expect_prefix_extension(compacted, again)
    _assert_no_orphan_tool_calls(again)
    assert again[3]["content"].count("[image stripped") == 1
    assert again[5].get("images")
    assert again[7].get("images")
    assert "[image stripped" not in (again[5].get("content") or "")
    assert "[image stripped" not in (again[7].get("content") or "")
