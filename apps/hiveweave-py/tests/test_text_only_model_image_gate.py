"""text-only 模型图像剥离测试（TEST_YLGY 潮汐事故修复 + auto 自判定）。

背景：截图无条件注入主对话模型，text-only 模型网关 400
「Model only support text input」→ 连续 LLM 错误 → agent 死亡螺旋。
修复：build_body 链路透传 supports_images，False 时剥离全部图像，
改写为 _IMAGES_OMITTED_NOTE 事实说明（400 文案匹配是猜，不点名工具）。
2026-08 演进：不再依赖人工勾选 supports_images —— 默认 auto「放行」，
由模型/网关在 400 时自行「判定」，streamer 标记负缓存并剥图重试一次；
显式 supports_images 仍可手工覆盖。
"""

from __future__ import annotations

import pytest

from hiveweave.llm import provider as prov_mod
from hiveweave.llm.provider import (
    _IMAGES_OMITTED_NOTE,
    _looks_like_image_unsupported_error,
    AnthropicHandler,
    GoogleHandler,
    OpenAIHandler,
    ProviderFactory,
)


@pytest.fixture(autouse=True)
def _clean_image_cache():
    """进程内负缓存是全局集合，逐用例清空防相互污染。"""
    prov_mod._image_unsupported_cache.clear()
    yield
    prov_mod._image_unsupported_cache.clear()


_IMG = {"media_type": "image/png", "data": "AAAA"}


def _openai_tool_block() -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "tool_call_id": "t1", "content": "shot ok", "images": [_IMG]},
    ]


# ── OpenAI ──────────────────────────────────────────────────────


def test_openai_text_only_tool_images_become_note():
    h = OpenAIHandler()
    out = h._normalize_messages_with_images(_openai_tool_block(), supports_images=False)
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "user"]
    assert all("images" not in m for m in out if m["role"] == "tool")
    note = out[-1]["content"]
    assert isinstance(note, str)
    assert "不支持图像" in note
    assert "look_at_image" not in note
    # 无任何图像 part 泄漏进请求
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for m in out
        for p in (m["content"] if isinstance(m.get("content"), list) else [])
    )


def test_openai_text_only_user_images_stripped_with_note():
    h = OpenAIHandler()
    msgs = [{"role": "user", "content": "看看这个", "images": [_IMG]}]
    out = h._normalize_messages_with_images(msgs, supports_images=False)
    assert len(out) == 1
    assert "images" not in out[0]
    assert "看看这个" in out[0]["content"]
    assert "不支持图像" in out[0]["content"]


def test_openai_default_supports_images_true_keeps_legacy():
    h = OpenAIHandler()
    out = h._normalize_messages_with_images(_openai_tool_block())
    assert any(
        p.get("type") == "image_url"
        for p in out[-1]["content"]
        if isinstance(p, dict)
    )


# ── Anthropic ───────────────────────────────────────────────────


def _anthropic_body(supports_images: bool) -> dict:
    h = AnthropicHandler()
    return h.build_body(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "browse", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "shot ok", "images": [_IMG]},
        ],
        model_id="claude-test",
        stream=False,
        tools=None,
        supports_images=supports_images,
    )


def test_anthropic_text_only_tool_result_text_only_with_note():
    body = _anthropic_body(supports_images=False)
    for m in body["messages"]:
        for block in m.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                assert isinstance(content, str)
                assert "不支持图像" in content
    # 没有任何 image block
    assert not any(
        b.get("type") == "image"
        for m in body["messages"]
        for b in (m.get("content") or [])
        if isinstance(b, dict)
    )


def test_anthropic_default_keeps_image_blocks():
    body = _anthropic_body(supports_images=True)
    found = any(
        b.get("type") == "image"
        for m in body["messages"]
        for block in (m.get("content") or [])
        for b in (block.get("content") if block.get("type") == "tool_result" and isinstance(block.get("content"), list) else [])
        if isinstance(b, dict)
    )
    assert found


def test_anthropic_text_only_user_blocks_note():
    h = AnthropicHandler()
    blocks = h._user_content_blocks(
        {"role": "user", "content": "hi", "images": [_IMG]},
        supports_images=False,
    )
    types = [b["type"] for b in blocks]
    assert "image" not in types
    assert any("不支持图像" in b.get("text", "") for b in blocks if b["type"] == "text")


# ── Gemini ──────────────────────────────────────────────────────


def test_gemini_text_only_tool_parts_note():
    h = GoogleHandler()
    body = h.build_body(
        [
            {"role": "tool", "tool_call_id": "t1", "content": "shot ok", "images": [_IMG]},
        ],
        model_id="gemini-test",
        stream=False,
        tools=None,
        supports_images=False,
    )
    parts = body["contents"][0]["parts"]
    assert not any("inlineData" in p for p in parts)
    assert any("不支持图像" in p.get("text", "") for p in parts if "text" in p)


def test_gemini_text_only_user_parts_note():
    h = GoogleHandler()
    parts = h._user_parts(
        {"role": "user", "content": "hi", "images": [_IMG]},
        supports_images=False,
    )
    assert not any("inlineData" in p for p in parts)
    assert any("不支持图像" in p.get("text", "") for p in parts if "text" in p)


# ── Factory / ProviderConfig ────────────────────────────────────


def _model_row(supports_images) -> dict:
    return {
        "model_id": "m",
        "base_url": "https://example.com/v1",
        "api_key": "k",
        "supports_images": supports_images,
    }


def test_factory_supports_images_auto_and_override():
    f = ProviderFactory()
    # 显式覆盖优先
    assert f.create(_model_row(1)).supports_images is True
    assert f.create(_model_row(0)).supports_images is False
    # NULL / 缺列 → auto（默认放行，让模型在 400 时自行判定）
    assert f.create(_model_row(None)).supports_images is True
    row = _model_row(1)
    del row["supports_images"]
    assert f.create(row).supports_images is True
    # 负缓存：一旦被 400 证明纯文本 → 该真实身份 (base_url, model_id) 关闭
    prov_mod.mark_image_unsupported("https://example.com/v1", "m")
    row2 = _model_row(None)
    del row2["supports_images"]
    assert f.create(row2).supports_images is False
    # 换基址/换模型 id → key 变化，自动重新探测（不被旧缓存锁死）
    renamed = _model_row(None)
    renamed["model_id"] = "m2"
    del renamed["supports_images"]
    assert f.create(renamed).supports_images is True


def test_negative_cache_normalizes_trailing_slash():
    """探测(create 未 rstrip)与标记(provider.base_url 已 rstrip)必须一致命中：
    尾斜杠 base_url 若未归一，负缓存永不命中 → 每轮 400+剥图。"""
    prov_mod.mark_image_unsupported("https://example.com/v1/", "m")
    f = ProviderFactory()
    row = _model_row(None)
    row["base_url"] = "https://example.com/v1/"  # 探测侧带尾斜杠
    del row["supports_images"]
    assert f.create(row).supports_images is False


def test_provider_config_body_strips_images_when_text_only():
    f = ProviderFactory()
    provider = f.create(_model_row(0))
    body = provider.build_body(
        [
            {"role": "assistant", "content": "", "tool_calls": [{}]},
            {"role": "tool", "tool_call_id": "t1", "content": "shot ok", "images": [_IMG]},
        ],
        stream=False,
        tools=None,
    )
    msgs = body["messages"]
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for m in msgs
        for p in (m["content"] if isinstance(m.get("content"), list) else [])
    )
    assert any(
        isinstance(m.get("content"), str) and "不支持图像" in m["content"]
        for m in msgs
    )


def test_note_text_is_factual_not_prescriptive():
    note = _IMAGES_OMITTED_NOTE.format(n=2)
    assert "2" in note
    assert "look_at_image" not in note
    assert "assert_visual" not in note
    assert "未注入" in note


def test_image_unsupported_detector_capability_not_payload():
    """Strip/cache only on capability denial — not payload or random 400s."""
    assert _looks_like_image_unsupported_error(
        "Model only support text input"
    )
    assert _looks_like_image_unsupported_error(
        "this model does not support images"
    )
    assert _looks_like_image_unsupported_error(
        "This model is not a multimodal model"
    )
    assert not _looks_like_image_unsupported_error(
        "Invalid image_url: expected a data URL or https"
    )
    assert not _looks_like_image_unsupported_error(
        "image is too large; max 4MB"
    )
    assert not _looks_like_image_unsupported_error(
        "Please respond with text only"
    )
    assert not _looks_like_image_unsupported_error(
        "does not support image/webp"
    )
    assert not _looks_like_image_unsupported_error(
        "invalid_request_error: missing required parameter"
    )
    assert not _looks_like_image_unsupported_error(
        "This endpoint only support text and image_url"
    )
    assert not _looks_like_image_unsupported_error(
        "webp images are not supported"
    )
    assert not _looks_like_image_unsupported_error(
        "cannot process images: decoded payload is truncated"
    )
