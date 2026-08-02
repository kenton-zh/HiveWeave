"""TEST19: OpenAI-compatible build_url must tolerate full endpoint base_url.

User added a model with base_url = https://opencode.ai/zen/go/v1/chat/completions
(full endpoint). The handler appended /chat/completions again ->
.../chat/completions/chat/completions -> HTML 404 page. Now idempotent.
"""

from __future__ import annotations

from hiveweave.llm.provider import OpenAIHandler, OpenAICompatibleHandler


def test_build_url_plain_base():
    h = OpenAIHandler()
    assert (
        h.build_url("https://api.deepseek.com", "m1")
        == "https://api.deepseek.com/chat/completions"
    )


def test_build_url_base_with_v1_suffix():
    h = OpenAIHandler()
    assert (
        h.build_url("https://api.openai.com/v1", "m1")
        == "https://api.openai.com/v1/chat/completions"
    )


def test_build_url_full_endpoint_is_idempotent():
    h = OpenAIHandler()
    assert (
        h.build_url("https://opencode.ai/zen/go/v1/chat/completions", "m1")
        == "https://opencode.ai/zen/go/v1/chat/completions"
    )


def test_build_url_full_endpoint_trailing_slash():
    h = OpenAIHandler()
    assert (
        h.build_url("https://opencode.ai/zen/go/v1/chat/completions/", "m1")
        == "https://opencode.ai/zen/go/v1/chat/completions"
    )


def test_compatible_handler_uses_same_guard():
    h = OpenAICompatibleHandler()
    assert (
        h.build_url("https://opencode.ai/zen/go/v1/chat/completions", "m1")
        == "https://opencode.ai/zen/go/v1/chat/completions"
    )
    assert (
        h.build_url("https://ark.cn-beijing.volces.com/api/coding/v3", "m1")
        == "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
    )
