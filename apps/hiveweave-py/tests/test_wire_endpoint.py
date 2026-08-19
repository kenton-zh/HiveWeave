"""Base URL is the /v1 prefix; protocol is a first-class field."""

from __future__ import annotations

from hiveweave.llm.provider import AnthropicHandler, ApiFormat, OpenAIHandler, ProviderFactory
from hiveweave.llm.wire_endpoint import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    apply_wire_endpoint,
    extract_nonstream_text,
    split_wire_endpoint,
)

ZEN_BASE = "https://opencode.ai/zen/go/v1"


def test_split_strips_known_suffixes_and_infers_protocol():
    assert split_wire_endpoint(f"{ZEN_BASE}/responses") == (ZEN_BASE, PROTOCOL_RESPONSES)
    assert split_wire_endpoint(f"{ZEN_BASE}/chat/completions") == (ZEN_BASE, PROTOCOL_CHAT)
    assert split_wire_endpoint(f"{ZEN_BASE}/messages") == (ZEN_BASE, PROTOCOL_ANTHROPIC)
    assert split_wire_endpoint(ZEN_BASE) == (ZEN_BASE, None)
    assert split_wire_endpoint(f"{ZEN_BASE}/responses?foo=1") == (ZEN_BASE, PROTOCOL_RESPONSES)


def test_apply_suffix_inference_wins_over_label():
    prefix, proto = apply_wire_endpoint(f"{ZEN_BASE}/responses", PROTOCOL_CHAT)
    assert prefix == ZEN_BASE
    assert proto == PROTOCOL_RESPONSES
    prefix, proto = apply_wire_endpoint(ZEN_BASE, PROTOCOL_RESPONSES)
    assert prefix == ZEN_BASE
    assert proto == PROTOCOL_RESPONSES
    prefix, proto = apply_wire_endpoint(ZEN_BASE, "")
    assert proto == PROTOCOL_CHAT


def test_openai_chat_build_url_does_not_double_append():
    h = OpenAIHandler()
    assert h.build_url(ZEN_BASE, "x") == f"{ZEN_BASE}/chat/completions"
    assert h.build_url(f"{ZEN_BASE}/chat/completions", "x") == f"{ZEN_BASE}/chat/completions"


def test_anthropic_build_url_is_idempotent_for_messages():
    h = AnthropicHandler()
    messages = f"{ZEN_BASE}/messages"
    assert h.build_url(ZEN_BASE, "qwen") == messages
    assert h.build_url(messages, "qwen") == messages
    assert h.build_url(messages + "/", "qwen") == messages
    assert h.build_url("https://api.anthropic.com", "claude") == (
        "https://api.anthropic.com/v1/messages"
    )


def test_leftover_messages_url_selects_anthropic():
    fmt = ProviderFactory()._detect_format({
        "provider_type": "openai-compatible",
        "base_url": f"{ZEN_BASE}/messages",
        "model_id": "qwen-plus",
    })
    assert fmt == ApiFormat.ANTHROPIC


def test_explicit_responses_beats_leftover_messages_suffix():
    fmt = ProviderFactory()._detect_format({
        "provider_type": "openai-responses",
        "base_url": f"{ZEN_BASE}/messages",
        "model_id": "muse-spark-1.2",
    })
    assert fmt == ApiFormat.OPENAI_RESPONSES


def test_extract_responses_output_text():
    data = {
        "object": "response",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "OK"}],
        }],
    }
    assert extract_nonstream_text(data) == "OK"
