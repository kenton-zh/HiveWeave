"""OpenAI Responses API: zen muse-spark must not hit /chat/completions.

Pasting https://opencode.ai/zen/go/v1/responses used to become
.../responses/chat/completions because every openai-compatible row used the
Chat Completions handler. muse-spark-1.2 then 400'd with an empty assistant
message and finish_reason=null.
"""

from __future__ import annotations

from hiveweave.api.models import _normalize_models_probe_base
from hiveweave.llm.openai_responses import (
    OpenAIResponsesHandler,
    looks_like_responses_endpoint,
    rewrite_to_responses_url,
)
from hiveweave.llm.provider import ApiFormat, ProviderFactory


ZEN_CHAT = "https://opencode.ai/zen/go/v1/chat/completions"
ZEN_RESP = "https://opencode.ai/zen/go/v1/responses"
ZEN_BASE = "https://opencode.ai/zen/go/v1"


def test_rewrite_url_idempotent_and_from_chat():
    h = OpenAIResponsesHandler()
    assert h.build_url(ZEN_RESP, "muse-spark-1.2") == ZEN_RESP
    assert h.build_url(ZEN_RESP + "/", "muse-spark-1.2") == ZEN_RESP
    assert h.build_url(ZEN_CHAT, "muse-spark-1.2") == ZEN_RESP
    assert h.build_url(ZEN_BASE, "muse-spark-1.2") == ZEN_RESP
    assert rewrite_to_responses_url(ZEN_RESP) == ZEN_RESP


def test_pasted_responses_url_is_not_appended_with_chat_completions():
    assert not rewrite_to_responses_url(ZEN_RESP).endswith(
        "/responses/chat/completions"
    )
    assert looks_like_responses_endpoint(ZEN_RESP)
    assert looks_like_responses_endpoint(ZEN_RESP + "/")


def test_model_id_is_not_the_protocol_authority():
    """muse-spark + Chat Completions label + leftover /chat URL stays Chat."""
    factory = ProviderFactory()
    fmt = factory._detect_format({
        "provider": "openai-compatible",
        "provider_type": "",
        "base_url": ZEN_CHAT,
        "model_id": "muse-spark-1.2",
    })
    assert fmt == ApiFormat.OPENAI_COMPATIBLE
    assert not looks_like_responses_endpoint(ZEN_CHAT)


def test_explicit_responses_protocol_uses_prefix():
    factory = ProviderFactory()
    fmt = factory._detect_format({
        "provider_type": "openai-responses",
        "base_url": ZEN_BASE,
        "model_id": "muse-spark-1.2",
    })
    assert fmt == ApiFormat.OPENAI_RESPONSES


def test_detect_responses_url_overrides_openai_compatible_label():
    factory = ProviderFactory()
    fmt = factory._detect_format({
        "provider": "openai-compatible",
        "base_url": ZEN_RESP,
        "model_id": "some-custom-model",
    })
    assert fmt == ApiFormat.OPENAI_RESPONSES


def test_deepseek_on_zen_stays_chat_completions():
    factory = ProviderFactory()
    fmt = factory._detect_format({
        "provider": "openai-compatible",
        "base_url": ZEN_CHAT,
        "model_id": "deepseek-v4-flash",
    })
    assert fmt == ApiFormat.OPENAI_COMPATIBLE
    assert not looks_like_responses_endpoint(ZEN_CHAT)


def test_factory_create_rewrites_muse_spark_url_and_body():
    cfg = ProviderFactory().create({
        "base_url": ZEN_BASE,
        "api_key": "sk-test",
        "model_id": "muse-spark-1.2",
        "provider_type": "openai-responses",
        "supports_thinking": True,
        "context_window": 1024000,
        "max_output_tokens": 8192,
    })
    assert cfg.api_format == ApiFormat.OPENAI_RESPONSES
    assert cfg.build_url() == ZEN_RESP
    body = cfg.build_body(
        messages=[
            {"role": "system", "content": "you are ceo"},
            {"role": "user", "content": "做个网页"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "commit_turn",
                "description": "end turn",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )
    assert "messages" not in body
    assert body["model"] == "muse-spark-1.2"
    assert body["store"] is False
    assert body["stream"] is True
    assert "stream_options" not in body
    assert body["input"][0]["role"] == "system"
    assert body["input"][1] == {"role": "user", "content": "做个网页"}
    assert body["tools"] == [{
        "type": "function",
        "name": "commit_turn",
        "description": "end turn",
        "parameters": {"type": "object", "properties": {}},
    }]
    assert body["reasoning"] == {"effort": "high"}
    assert "temperature" not in body


def test_messages_to_input_tool_roundtrip():
    h = OpenAIResponsesHandler()
    body = h.build_body(
        messages=[
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"a.py"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "print(1)",
            },
        ],
        model_id="muse-spark-1.2",
        supports_thinking=False,
    )
    assert body["input"] == [
        {"role": "user", "content": "read it"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "print(1)",
        },
    ]
    assert body["temperature"] == 0.7
    assert "reasoning" not in body


def test_parse_stream_text_tool_and_finish():
    h = OpenAIResponsesHandler()
    assert h.parse_stream_chunk({
        "type": "response.output_text.delta",
        "delta": "hello",
    }) == [{"type": "text", "content": "hello"}]
    added = h.parse_stream_chunk({
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "call_id": "call_9",
            "name": "commit_turn",
            "arguments": "",
        },
    })
    assert added[0]["tool_call"]["id"] == "call_9"
    assert added[0]["tool_call"]["name"] == "commit_turn"
    arg = h.parse_stream_chunk({
        "type": "response.function_call_arguments.delta",
        "output_index": 0,
        "delta": '{"phase":"waiting"}',
    })
    assert arg[0]["tool_call"]["arguments"] == '{"phase":"waiting"}'
    finish = h.parse_stream_chunk({
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "function_call", "name": "commit_turn"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 80},
            },
        },
    })
    assert finish == [{"type": "finish", "reason": "tool_calls"}]
    usage = h.extract_usage({
        "type": "response.completed",
        "response": {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 80},
            },
        },
    })
    assert usage == {
        "input": 100,
        "output": 20,
        "total": 120,
        "cache_read": 80,
        "prompt_cache_hit_tokens": 80,
    }


def test_parse_failed_event():
    h = OpenAIResponsesHandler()
    chunks = h.parse_stream_chunk({
        "type": "response.failed",
        "response": {"error": {"message": "bad request"}},
    })
    assert chunks == [{"type": "error", "content": "bad request"}]


def test_probe_base_strips_responses_suffix():
    assert _normalize_models_probe_base(ZEN_RESP) == ZEN_BASE
    assert _normalize_models_probe_base(ZEN_CHAT) == ZEN_BASE
    assert _normalize_models_probe_base(ZEN_BASE + "/messages") == ZEN_BASE


def test_explicit_responses_rewrites_leftover_chat_url():
    cfg = ProviderFactory().create({
        "base_url": ZEN_CHAT,
        "api_key": "sk-test",
        "model_id": "muse-spark-1.2",
        "provider_type": "openai-responses",
        "context_window": 128000,
        "max_output_tokens": 8192,
    })
    assert cfg.api_format == ApiFormat.OPENAI_RESPONSES
    assert cfg.build_url() == ZEN_RESP
