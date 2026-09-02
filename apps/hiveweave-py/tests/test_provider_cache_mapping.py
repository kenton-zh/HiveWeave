"""s3-clone_07 报告 P1-1：DeepSeek 系 cache-write 字段必须映射进账本。

DeepSeek 语义：prompt_tokens = prompt_cache_hit_tokens（读，0.1x 计费）
+ prompt_cache_miss_tokens（写，1.25x 计费，等价 Anthropic 的 cache_creation）。
此前 extract_usage 只映射了 hit（cache_read），miss 落地成裸 passthrough 字段，
账本 3764 行 cache_creation_tokens 恒 0——命中率分母缺分量。
"""

from __future__ import annotations

from hiveweave.llm.provider import OpenAIHandler


def test_deepseek_cache_miss_maps_to_cache_creation():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
    }
    out = OpenAIHandler.extract_usage({"usage": usage})
    assert out["cache_read"] == 800
    assert out["cache_creation"] == 200


def test_all_miss_maps_to_creation_when_no_hits():
    usage = {
        "prompt_tokens": 500,
        "completion_tokens": 50,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 500,
    }
    out = OpenAIHandler.extract_usage({"usage": usage})
    assert out["cache_read"] == 0
    assert out["cache_creation"] == 500  # miss=500 是真实的首次写入


def test_absent_miss_keeps_openai_shape():
    """纯 OpenAI 形态（无 DeepSeek 字段）不受影响。"""
    usage = {
        "prompt_tokens": 300,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 120},
    }
    out = OpenAIHandler.extract_usage({"usage": usage})
    assert out["cache_read"] == 120
    assert "cache_creation" not in out
