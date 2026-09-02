"""LLM usage normalization helpers for token metering.

提供 provider 无关的 token 明细归一化：
- 桶不相交（DSH TokenUsage）：input = 未命中；cache_read / cache_creation 另列。
- DeepSeek / ARK 的 prompt_tokens 含命中，只在 normalize_usage 减一次。
- Anthropic input 已是未命中，normalize 不再减。
- total = input + output + cache_creation（不含 cache_read）。
- 命中率 = cache_read / (input + cache_read + cache_creation)。
"""

from __future__ import annotations

from typing import Any


def usage_int(value: Any, default: int = 0) -> int:
    """Coerce a provider usage field to a non-negative int; malformed → default."""
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, n)


def billed_prompt_tokens(
    input_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> int:
    """Prompt-side billed buckets (DSH billedInputTokens)."""
    return int(input_tokens or 0) + int(cache_read or 0) + int(cache_write or 0)


def cache_hit_percent(
    input_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    *,
    input_inclusive: bool = False,
) -> int | None:
    """Rounded cache-hit share of prompt-side input; None when nothing was billed.

    ``input_inclusive=True``（DeepSeek 系）：prompt_tokens 已包含 cache
    hit+miss（prompt = hit + miss），分母就是 input 本身——再加 read/write
    会把分子双计（39 审计 P1-2：命中率被腰斩的根因）。
    """
    if input_inclusive:
        denom = int(input_tokens or 0)
    else:
        denom = billed_prompt_tokens(input_tokens, cache_read, cache_write)
    if denom <= 0:
        return None
    return round(int(cache_read or 0) / denom * 100)


def openai_wire_cache_read(usage: dict[str, Any]) -> int:
    """DeepSeek hit field or OpenAI-compat ``prompt_tokens_details.cached_tokens``.

    Prefer a positive ``cached_tokens`` (ARK). Fall back to DeepSeek hit, then
    an explicit 0 in details, then already-mapped ``cache_read`` / ``cached``.
    """
    details = usage.get("prompt_tokens_details")
    details_cached = None
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        details_cached = usage_int(details.get("cached_tokens"))
        if details_cached > 0:
            return details_cached
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is not None:
        return usage_int(hit)
    if details_cached is not None:
        return details_cached
    return usage_int(usage.get("cache_read") or usage.get("cached"))


def _provider_reports_disjoint_input(provider: str | None) -> bool:
    """Anthropic ``input_tokens`` already excludes cache read/write."""
    p = (provider or "").strip().lower()
    return p == "anthropic" or p.startswith("anthropic")


def provider_input_inclusive(provider: str | None) -> bool:
    """DeepSeek 系：prompt_tokens = cache_hit + cache_miss（已含缓存读写）。

    命中率分母 = input 本身；再加 read/creation 会双计（39 审计 P1-2）。
    """
    p = (provider or "").strip().lower()
    return p.startswith("deepseek")


#: Wire fields ``normalize_usage`` reads for a cache-write (cache creation)
#: count. Anthropic's ``cache_creation_input_tokens`` is already mapped onto
#: ``cache_creation`` by the provider handler, so it is not listed here.
CACHE_CREATION_WIRE_FIELDS = (
    "cache_creation",
    "cache_creation_tokens",
)


def provider_reports_cache_creation(provider: str | None) -> bool:
    """Whether this provider family reports cache **writes** at all.

    Only Anthropic Messages exposes ``cache_creation_input_tokens``. The
    OpenAI wire families (openai / openai-compatible / openai-responses /
    ARK / DeepSeek / Google) report cache *reads* only — a 0 there means
    "not reported", not "no cache was written". Without this distinction a
    cache-hit-rate analysis cannot state its denominator (TEST_DSH_33: the
    91.42% figure was unattributable because cache_creation was silently 0).
    """
    return _provider_reports_disjoint_input(provider)


def usage_has_cache_creation_field(usage: dict[str, Any] | None) -> bool:
    """Whether the raw usage payload actually carried a cache-write count."""
    if not usage:
        return False
    return any(f in usage for f in CACHE_CREATION_WIRE_FIELDS)


def normalize_usage(
    usage: dict[str, Any] | None,
    provider: str | None = None,
) -> dict[str, int] | None:
    """归一化一轮 usage，返回统一字段的 dict。

    返回 None 表示无可用 usage（HTTP 错误轮次 / 异常路径）。
    字段：input, output, cache_read, cache_creation, total, duration_ms,
    cache_creation_reported。

    只在这里剥一次缓存。OpenAI/ARK/Google 的 prompt 含命中；Anthropic 已是
    未命中桶，禁止再减。不要用 ``input >= cache_read`` 当「尚未剥」——
    命中率 ≤50% 时已剥过的 input 仍会 ≥ cache_read。

    ``cache_creation_reported`` = 1/0 量程位：0 表示该 provider 根本不上报
    cache 写入，此时 ``cache_creation`` 的 0 是「无数据」而非「确实为 0」，
    命中率分母只覆盖 input + cache_read。
    """
    if not usage:
        return None
    cache_read = openai_wire_cache_read(usage)
    cache_creation = usage_int(
        usage.get("cache_creation") or usage.get("cache_creation_tokens")
    )
    reported = usage_has_cache_creation_field(usage) or (
        provider_reports_cache_creation(provider)
    )
    ds_miss = usage.get("prompt_cache_miss_tokens")
    input_tokens = usage_int(
        usage.get("input") or usage.get("input_tokens") or usage.get("prompt_tokens")
    )
    output_tokens = usage_int(
        usage.get("output") or usage.get("output_tokens") or usage.get("completion_tokens")
    )
    if not _provider_reports_disjoint_input(provider):
        if ds_miss is not None:
            input_tokens = usage_int(ds_miss)
        elif cache_read > 0:
            input_tokens = max(0, input_tokens - cache_read)
    total = input_tokens + output_tokens + cache_creation
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "total": total,
        "duration_ms": usage_int(usage.get("duration_ms")),
        "cache_creation_reported": 1 if reported else 0,
    }
