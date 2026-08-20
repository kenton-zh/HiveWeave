"""Thinking dialect is first-class: protocol ≠ thinking wire fields."""

from __future__ import annotations

from hiveweave.llm.openai_responses import OpenAIResponsesHandler
from hiveweave.llm.provider import AnthropicHandler, GoogleHandler, OpenAIHandler, ProviderFactory
from hiveweave.llm.thinking import (
    FORMAT_ANTHROPIC,
    FORMAT_DEEPSEEK,
    FORMAT_GEMINI,
    FORMAT_OFF,
    FORMAT_OPENAI_EFFORT,
    FORMAT_QWEN,
    FORMAT_RESPONSES,
    resolve_effort,
    resolve_thinking_format,
)

ZEN_BASE = "https://opencode.ai/zen/go/v1"


def test_checkbox_off_wins_over_leftover_dialect():
    assert resolve_thinking_format(
        FORMAT_OPENAI_EFFORT,
        supports_thinking=False,
        protocol="openai-compatible",
    ) == FORMAT_OFF


def test_explicit_off_wins_over_checkbox():
    assert resolve_thinking_format(
        FORMAT_OFF,
        supports_thinking=True,
        protocol="openai-responses",
    ) == FORMAT_OFF


def test_auto_follows_protocol():
    assert resolve_thinking_format(
        "", supports_thinking=True, protocol="openai-responses"
    ) == FORMAT_RESPONSES
    assert resolve_thinking_format(
        "", supports_thinking=True, protocol="anthropic"
    ) == FORMAT_ANTHROPIC
    assert resolve_thinking_format(
        "", supports_thinking=True, protocol="google"
    ) == FORMAT_GEMINI
    assert resolve_thinking_format(
        "", supports_thinking=True, protocol="openai-compatible"
    ) == FORMAT_OPENAI_EFFORT


def test_chat_auto_sends_reasoning_effort_not_thinking_type():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "deepseek-v4-flash",
        stream=False,
        supports_thinking=True,
    )
    assert body["reasoning_effort"] == "high"
    assert "thinking" not in body
    assert "enable_thinking" not in body


def test_chat_off_omits_reasoning_effort():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "deepseek-v4-flash",
        stream=False,
        supports_thinking=False,
        thinking_format=FORMAT_OPENAI_EFFORT,
    )
    assert "reasoning_effort" not in body


def test_deepseek_dialect_sends_thinking_type():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "deepseek-v4-flash",
        stream=False,
        supports_thinking=True,
        thinking_format=FORMAT_DEEPSEEK,
        reasoning_effort="max",
    )
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"


def test_qwen_dialect_sends_enable_thinking():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "qwen3",
        stream=False,
        supports_thinking=True,
        thinking_format=FORMAT_QWEN,
        reasoning_effort="high",
        max_tokens=32_000,
    )
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 16_000
    assert "reasoning_effort" not in body


def test_anthropic_thinking_does_not_require_effort_string():
    """Main settings page used to omit effort, so Anthropic thinking never fired."""
    body = AnthropicHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "claude-sonnet-4-5",
        stream=False,
        supports_thinking=True,
    )
    budget = body["thinking"]["budget_tokens"]
    assert body["thinking"]["type"] == "enabled"
    assert budget < body["max_tokens"]
    assert "temperature" not in body


def test_gemini_thinking_includes_budget():
    body = GoogleHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "gemini-2.5-pro",
        stream=False,
        supports_thinking=True,
        reasoning_effort="low",
    )
    cfg = body["generationConfig"]["thinkingConfig"]
    assert cfg["includeThoughts"] is True
    assert cfg["thinkingBudget"] == 4_096
    assert cfg["thinkingBudget"] < body["generationConfig"]["maxOutputTokens"]


def test_gemini_empty_effort_clamps_budget_below_output_cap():
    body = GoogleHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "gemini-2.5-pro",
        stream=False,
        supports_thinking=True,
        max_tokens=8192,
    )
    budget = body["generationConfig"]["thinkingConfig"]["thinkingBudget"]
    assert budget < 8192
    assert budget == 8192 - 1024


def test_chat_responses_dialect_sends_reasoning_object():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "muse-spark-1.2",
        stream=False,
        supports_thinking=True,
        thinking_format=FORMAT_RESPONSES,
        reasoning_effort="high",
    )
    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body


def test_chat_gemini_dialect_sends_thinking_config():
    body = OpenAIHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "gemini-2.5-pro",
        stream=False,
        supports_thinking=True,
        thinking_format=FORMAT_GEMINI,
    )
    assert body["thinkingConfig"]["includeThoughts"] is True
    assert "reasoning_effort" not in body


def test_responses_deepseek_dialect_keeps_thinking_type():
    body = OpenAIResponsesHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "deepseek-v4-flash",
        supports_thinking=True,
        thinking_format=FORMAT_DEEPSEEK,
        reasoning_effort="max",
    )
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert "reasoning" not in body
    assert "temperature" not in body


def test_responses_auto_uses_reasoning_object():
    cfg = ProviderFactory().create({
        "base_url": ZEN_BASE,
        "api_key": "sk-test",
        "model_id": "muse-spark-1.2",
        "provider_type": "openai-responses",
        "supports_thinking": True,
        "context_window": 1024000,
        "max_output_tokens": 8192,
    })
    body = cfg.build_body(messages=[{"role": "user", "content": "hi"}])
    assert body["reasoning"] == {"effort": "high"}
    assert "temperature" not in body
    assert "reasoning_effort" not in body


def test_responses_thinking_off_keeps_temperature():
    body = OpenAIResponsesHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "muse-spark-1.2",
        supports_thinking=False,
        temperature=0.4,
    )
    assert body["temperature"] == 0.4
    assert "reasoning" not in body


def test_responses_rejects_global_max_slot():
    """Old UI 最大 stored as max; muse-spark only accepts xhigh as the top slot."""
    assert resolve_effort("max", FORMAT_RESPONSES) == "xhigh"
    assert resolve_effort("xhigh", FORMAT_RESPONSES) == "xhigh"
    assert resolve_effort("max", FORMAT_DEEPSEEK) == "max"
    assert resolve_effort("max", FORMAT_OPENAI_EFFORT) == "max"
    body = OpenAIResponsesHandler().build_body(
        [{"role": "user", "content": "hi"}],
        "muse-spark-1.2-contributor",
        supports_thinking=True,
        reasoning_effort="max",
    )
    assert body["reasoning"] == {"effort": "xhigh"}


def test_factory_responses_leftover_max_becomes_xhigh():
    cfg = ProviderFactory().create({
        "base_url": ZEN_BASE,
        "api_key": "sk-test",
        "model_id": "muse-spark-1.2-contributor",
        "provider_type": "openai-responses",
        "supports_thinking": True,
        "default_reasoning_effort": "max",
        "context_window": 1024000,
        "max_output_tokens": 8192,
    })
    body = cfg.build_body(messages=[{"role": "user", "content": "hi"}])
    assert body["reasoning"] == {"effort": "xhigh"}
