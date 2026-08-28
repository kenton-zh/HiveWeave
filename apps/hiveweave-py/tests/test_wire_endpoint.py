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


def test_extract_does_not_fall_back_to_reasoning_when_content_empty():
    """T2.3 修订：reasoning 字段**不**作为文本回退（与 vision 的有意差异）。

    reasoning 不是可见内容通道。compactor 的 length 守卫依赖「content 空 =
    失败 → 重试」（test_compaction_hardening）：若把 reasoning 当文本返回，
    reasoning 模型吃光输出预算时会把 "thinking..." 存成团队摘要（全量回归
    实测复现）。此测试钉住「不回退」，防止未来又被当缺口补回来。
    """
    data = {
        "choices": [{
            "message": {"content": "", "reasoning_content": "step by step"},
        }],
    }
    assert extract_nonstream_text(data) == ""


def test_extract_content_parts_without_type_field():
    """services/vision joins any part carrying `text`, type field or not.

    _content_to_text is strict (`type == "text"`), so the shared extractor
    needs this loose pass to stay a superset of the vision copy.
    """
    data = {
        "choices": [{
            "message": {"content": [{"text": "hello "}, {"text": "world"}]},
        }],
    }
    assert extract_nonstream_text(data) == "hello world"


def test_extract_top_level_string_content():
    """services/vision accepts a plain-string `content`; the shared extractor
    must too, or switching callers over regresses that shape to empty."""
    assert extract_nonstream_text({"content": "hi"}) == "hi"


def test_agent_uses_shared_extractor_not_vision_copy():
    """P0-6 regression guard.

    agents/agent.py imported ``extract_nonstream_text`` from services/vision,
    which predates Responses API support — every request_code_audit call
    parsed to an empty string and soft-failed (6/6 in TEST_DSH_35).
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "agents" / "agent.py"
    text = src.read_text(encoding="utf-8")
    assert "from hiveweave.services.vision import extract_nonstream_text" not in text
    assert "from hiveweave.llm.wire_endpoint import extract_nonstream_text" in text
