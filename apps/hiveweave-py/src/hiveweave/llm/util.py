"""LLM usage normalization helpers for token metering.

提供 provider 无关的 token 明细归一化（F1 修正）：
- cache 字段名因 provider 而异：Anthropic 取 cache_read/cache_creation，
  Google 取 cached（映射到 cache_read），OpenAI 无 cache 键置 0。
- DeepSeek（openai 兼容）：prompt_cache_hit_tokens / prompt_cache_miss_tokens，
  prompt_tokens = hit + miss。命中部分映射到 cache_read，未命中作为 input。
- total_tokens 口径 = input + output + cache_creation（不含 cache_read，
  因为 cache_read 是命中已有缓存、不新增计费的部分，单独列示更有诊断价值）。
"""

from __future__ import annotations

from typing import Any


def normalize_usage(
    usage: dict[str, Any] | None,
    provider: str | None = None,
) -> dict[str, int] | None:
    """归一化一轮 usage，返回统一字段的 dict。

    返回 None 表示无可用 usage（HTTP 错误轮次 / 异常路径）。
    字段：input, output, cache_read, cache_creation, total, duration_ms。

    DeepSeek 缓存拆解：若 usage 同时含 prompt_cache_hit/miss，则
    - cache_read = prompt_cache_hit_tokens
    - input = prompt_cache_miss_tokens（新计费部分）
    否则 input 沿用 prompt_tokens（含缓存，回退口径）。
    """
    if not usage:
        return None
    # cache 字段因 provider 而异：Anthropic「cache_read/cache_creation」、
    # Google「cached」（映射到 cache_read）、OpenAI 无 cache 键。
    cache_read = usage.get("cache_read") or usage.get("cached") or 0
    cache_creation = usage.get("cache_creation") or 0
    # DeepSeek（openai 兼容）：prompt_cache_hit_tokens / prompt_cache_miss_tokens
    ds_hit = usage.get("prompt_cache_hit_tokens")
    ds_miss = usage.get("prompt_cache_miss_tokens")
    if ds_hit is not None:
        cache_read = int(ds_hit) or cache_read
    # OpenAI 兼容网关（Volcengine ARK 等）：prompt_tokens_details.cached_tokens
    details = usage.get("prompt_tokens_details") or {}
    openai_cached = (details.get("cached_tokens") or 0) or 0
    if openai_cached:
        cache_read = int(openai_cached) or cache_read
    # 兼容 Anthropic 的 input_tokens/output_tokens 长字段名
    input_tokens = usage.get("input") or usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output") or usage.get("output_tokens") or usage.get("completion_tokens") or 0
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)
    cache_read = int(cache_read)
    cache_creation = int(cache_creation)
    # DeepSeek 拆解：命中部分从 input 里剥出，避免虚高（缓存命中按 1/10 计费）
    if ds_hit is not None and ds_miss is not None:
        input_tokens = int(ds_miss)
    elif openai_cached:
        # prompt_tokens 已包含 cached_tokens，剥出命中部分（新计费仅未命中部分）
        input_tokens = max(0, input_tokens - int(openai_cached))
    total = input_tokens + output_tokens + cache_creation
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "total": total,
        "duration_ms": int(usage.get("duration_ms", 0) or 0),
    }