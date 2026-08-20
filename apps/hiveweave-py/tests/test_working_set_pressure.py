"""In-loop DSH working-set pressure: 0.8 trigger, prune, LLM head summary, 0.16 tail."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.provider import ApiFormat, ProviderConfig
from hiveweave.llm.streamer.constants import WORKING_SET_CHECKPOINT_MARKER
from hiveweave.llm.streamer.context import ContextMixin
from tests.test_context_trim_tool_pairs import _assert_no_orphan_tool_calls
from tests.test_prefix_cache_append import _expect_prefix_extension, _round


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


@pytest.mark.asyncio
async def test_below_pressure_does_not_rewrite_prefix():
    ctx = _Ctx()
    provider = _provider()
    _, pressure_at, _ = ctx._working_set_budgets(provider)
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", "one"),
        *_round("c2", "two"),
    ]
    assert estimate_tokens_for_messages(messages) < pressure_at
    out = await ctx._pressure_compact_if_needed(messages, provider)
    assert out is messages


@pytest.mark.asyncio
async def test_pressure_prune_without_llm_when_placeholder_saves_enough():
    ctx = _Ctx()
    provider = _provider()
    _, pressure_at, _ = ctx._working_set_budgets(provider)
    _, trim_at = ctx._input_trim_at(provider)
    # ~87k tokens: over 0.8, under 0.95. Last two rounds skip prune.
    old_body = "y" * 350_000
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", old_body),
        *_round("c2", "recent-two"),
        *_round("c3", "recent-one"),
    ]
    total = estimate_tokens_for_messages(messages)
    assert pressure_at <= total < trim_at

    called = {"n": 0}

    async def _summarize(_transcript: str) -> str | None:
        called["n"] += 1
        return "should not run"

    out = await ctx._pressure_compact_if_needed(
        messages, provider, summarize=_summarize
    )
    assert called["n"] == 0
    assert out[3]["content"] == ctx._PRUNE_PLACEHOLDER
    assert estimate_tokens_for_messages(out) < pressure_at
    _assert_no_orphan_tool_calls(out)

    nxt = out + _round("c4", "after")
    again = await ctx._pressure_compact_if_needed(nxt, provider, summarize=_summarize)
    _expect_prefix_extension(out, again)


@pytest.mark.asyncio
async def test_pressure_compact_does_not_call_compaction_compact():
    ctx = _Ctx()
    provider = _provider()
    messages = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
        *_round("c1", "tiny"),
    ]
    with patch(
        "hiveweave.conversation.compaction.Compaction.compact",
        new_callable=AsyncMock,
    ) as compact:
        out = await ctx._pressure_compact_if_needed(messages, provider)
    compact.assert_not_called()
    assert out is messages


@pytest.mark.asyncio
async def test_pressure_llm_summary_keeps_tail_and_drops_below_line():
    ctx = _Ctx()
    provider = _provider()
    _, pressure_at, retain_at = ctx._working_set_budgets(provider)
    body = "z" * 12_000
    messages: list[dict] = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
    ]
    for i in range(30):
        messages.extend(_round(f"c{i}", body))
    assert estimate_tokens_for_messages(messages) >= pressure_at

    async def _summarize(transcript: str) -> str | None:
        assert "c0" in transcript or "echo" in transcript or "z" in transcript
        return "Goal: continue. Files: none. Next: finish."

    out = await ctx._pressure_compact_if_needed(
        messages, provider, summarize=_summarize
    )
    text = " ".join(str(m.get("content") or "") for m in out)
    assert WORKING_SET_CHECKPOINT_MARKER in text
    _assert_no_orphan_tool_calls(out)
    after = estimate_tokens_for_messages(out)
    assert after < pressure_at
    last_pair = estimate_tokens_for_messages(out[-2:]) if len(out) >= 2 else after
    assert after <= max(retain_at, last_pair)
    from hiveweave.conversation.compaction import SUMMARY_MARKER

    assert SUMMARY_MARKER not in text
    assert any(m.get("tool_call_id") == "c29" for m in out)


@pytest.mark.asyncio
async def test_empty_summary_hard_trims_without_checkpoint():
    ctx = _Ctx()
    provider = _provider()
    body = "z" * 12_000
    messages: list[dict] = [
        {"role": "system", "content": "identity"},
        {"role": "user", "content": "go"},
    ]
    for i in range(30):
        messages.extend(_round(f"c{i}", body))

    async def _empty(_transcript: str) -> str | None:
        return None

    out = await ctx._pressure_compact_if_needed(
        messages, provider, summarize=_empty
    )
    text = " ".join(str(m.get("content") or "") for m in out)
    assert WORKING_SET_CHECKPOINT_MARKER not in text
    _, pressure_at, retain_at = ctx._working_set_budgets(provider)
    after = estimate_tokens_for_messages(out)
    assert after < pressure_at
    last_pair = estimate_tokens_for_messages(out[-2:]) if len(out) >= 2 else after
    assert after <= max(retain_at, last_pair)
    _assert_no_orphan_tool_calls(out)
    assert out[0]["content"] == "identity"
