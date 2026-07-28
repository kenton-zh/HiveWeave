"""text-only 模型图像剥离测试（TEST_YLGY 潮汐事故修复）。

背景：截图无条件注入主对话模型，text-only 模型网关 400
「Model only support text input」→ 连续 LLM 错误 → agent 死亡螺旋。
修复：build_body 链路透传 supports_images，False 时剥离全部图像，
改写为 _IMAGES_OMITTED_NOTE 文字指引（走 look_at_image / 如实 blocked）。
"""

from __future__ import annotations

from hiveweave.llm.provider import (
    _IMAGES_OMITTED_NOTE,
    AnthropicHandler,
    GoogleHandler,
    OpenAIHandler,
    ProviderFactory,
)

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
    assert "supports_images" in note
    assert "look_at_image" in note
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
    assert "supports_images" in out[0]["content"]


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
                assert "supports_images" in content
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
    assert any("supports_images" in b.get("text", "") for b in blocks if b["type"] == "text")


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
    assert any("supports_images" in p.get("text", "") for p in parts if "text" in p)


def test_gemini_text_only_user_parts_note():
    h = GoogleHandler()
    parts = h._user_parts(
        {"role": "user", "content": "hi", "images": [_IMG]},
        supports_images=False,
    )
    assert not any("inlineData" in p for p in parts)
    assert any("supports_images" in p.get("text", "") for p in parts if "text" in p)


# ── Factory / ProviderConfig ────────────────────────────────────


def _model_row(supports_images) -> dict:
    return {
        "model_id": "m",
        "base_url": "https://example.com/v1",
        "api_key": "k",
        "supports_images": supports_images,
    }


def test_factory_maps_supports_images_flag():
    f = ProviderFactory()
    assert f.create(_model_row(1)).supports_images is True
    assert f.create(_model_row(0)).supports_images is False
    # NULL / 缺列 → 保守按 text-only（宁剥图不错 400）
    assert f.create(_model_row(None)).supports_images is False
    row = _model_row(1)
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
        isinstance(m.get("content"), str) and "supports_images" in m["content"]
        for m in msgs
    )


def test_note_text_guides_to_look_at_image():
    note = _IMAGES_OMITTED_NOTE.format(n=2)
    assert "2" in note
    assert "look_at_image" in note
