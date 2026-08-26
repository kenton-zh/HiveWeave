"""top_p/top_k 采样参数透传 — 模型配置 → ProviderConfig → handler 请求体。

契约：不设置（None）→ 请求体不带这些字段，行为与改造前完全一致；
设置 → 按各协议原生字段名下发（OpenAI top_p/top_k、Anthropic top_p/top_k、
Gemini topP/topK、Responses 仅 top_p）。
"""

from __future__ import annotations

from hiveweave.llm.openai_responses import OpenAIResponsesHandler
from hiveweave.llm.provider import FORMAT_HANDLERS, provider_factory
from hiveweave.llm.thinking import apply_anthropic_thinking

_MSGS = [{"role": "user", "content": "hi"}]


def _chat_body(**kwargs):
    return FORMAT_HANDLERS["openai-compatible"].build_body(
        _MSGS, "test-model", **kwargs
    )


def _responses_body(**kwargs):
    return OpenAIResponsesHandler().build_body(_MSGS, "test-model", **kwargs)


def _anthropic_body(**kwargs):
    return FORMAT_HANDLERS["anthropic"].build_body(_MSGS, "test-model", **kwargs)


def _gemini_body(**kwargs):
    return FORMAT_HANDLERS["google"].build_body(_MSGS, "test-model", **kwargs)


# ── OpenAI / openai-compatible ──────────────────────────────────────────────


def test_chat_top_p_top_k_sent_when_set():
    body = _chat_body(top_p=0.9, top_k=50)
    assert body["top_p"] == 0.9
    assert body["top_k"] == 50


def test_chat_absent_when_unset():
    body = _chat_body()
    assert "top_p" not in body
    assert "top_k" not in body


def test_chat_thinking_coexists_with_sampling():
    """chat 方言无采样互斥约束（DeepSeek 等）：思考激活时 top_p/top_k 照发。

    设计锁定：与 Responses 的守卫有意不对称，防止未来被「顺手」对齐改坏。
    """
    body = _chat_body(top_p=0.9, top_k=50, supports_thinking=True)
    assert body["top_p"] == 0.9
    assert body["top_k"] == 50


# ── OpenAI Responses ─────────────────────────────────────────────────────────


def test_responses_top_p_sent_without_thinking():
    body = _responses_body(top_p=0.9, top_k=50)
    assert body["top_p"] == 0.9
    assert "top_k" not in body  # Responses API 无 top_k 参数


def test_responses_top_p_suppressed_with_thinking():
    """o 系推理模型对采样参数修改直接 400：思考方言激活时不发 top_p。"""
    body = _responses_body(top_p=0.9, supports_thinking=True)
    assert "top_p" not in body


# ── Anthropic ────────────────────────────────────────────────────────────────


def test_anthropic_top_p_top_k_sent_when_set():
    body = _anthropic_body(top_p=0.95, top_k=40)
    assert body["top_p"] == 0.95
    assert body["top_k"] == 40


def test_anthropic_absent_when_unset():
    body = _anthropic_body()
    assert "top_p" not in body
    assert "top_k" not in body


def test_anthropic_thinking_strips_sampling_params():
    """扩展思考与采样参数互斥：thinking 激活时 top_p/top_k/temperature 全部剥离。"""
    body = _anthropic_body(
        top_p=0.95, top_k=40, supports_thinking=True
    )
    assert "thinking" in body
    assert "top_p" not in body
    assert "top_k" not in body
    assert "temperature" not in body


def test_apply_anthropic_thinking_pops_all_sampling_params():
    body = {"model": "m", "temperature": 0.7, "top_p": 0.9, "top_k": 30}
    apply_anthropic_thinking(body, "anthropic", None, max_tokens=4096)
    assert "thinking" in body
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body


# ── Google Gemini ────────────────────────────────────────────────────────────


def test_gemini_top_p_top_k_native_names():
    body = _gemini_body(top_p=0.8, top_k=20)
    cfg = body["generationConfig"]
    assert cfg["topP"] == 0.8
    assert cfg["topK"] == 20


def test_gemini_absent_when_unset():
    cfg = _gemini_body()["generationConfig"]
    assert "topP" not in cfg
    assert "topK" not in cfg


# ── 工厂端到端：模型 DB 记录 → 请求体 ─────────────────────────────────────


def _model_config(**extra) -> dict:
    base = {
        "id": "m1",
        "name": "Test Model",
        "model_id": "test-model",
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-test",
        "context_window": 131072,
        "max_output_tokens": 8192,
        "temperature": 0.7,
    }
    base.update(extra)
    return base


def test_factory_end_to_end_passthrough():
    config = provider_factory.create(_model_config(top_p=0.9, top_k=50))
    body = config.build_body(_MSGS)
    assert body["top_p"] == 0.9
    assert body["top_k"] == 50


def test_factory_unset_means_unchanged():
    """存量模型记录（无 top_p/top_k 字段或为 None）→ 请求体不带，零行为变化。"""
    for cfg in (_model_config(), _model_config(top_p=None, top_k=None)):
        body = provider_factory.create(cfg).build_body(_MSGS)
        assert "top_p" not in body
        assert "top_k" not in body


def test_factory_dirty_values_coerce_to_none():
    """脏数据（空串/非法字符串，手工 SQL 或旧客户端可写入）→ 容错为不发，
    不许在 ProviderConfig 构造处炸掉 streamer 主链路整轮 agent 回合。
    """
    for cfg in (
        _model_config(top_p="", top_k=""),
        _model_config(top_p="abc", top_k="xyz"),
    ):
        body = provider_factory.create(cfg).build_body(_MSGS)
        assert "top_p" not in body
        assert "top_k" not in body
