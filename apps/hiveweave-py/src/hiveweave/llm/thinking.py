"""Thinking dialect vs effort.

Protocol picks the HTTP path. Thinking dialect picks which JSON fields carry
reasoning. They are not the same: Chat Completions gateways still disagree
(``reasoning_effort`` vs ``thinking.type`` vs ``enable_thinking``).

Empty ``thinking_format`` + ``supports_thinking`` infers from protocol (legacy
checkbox). Explicit ``off`` never sends thinking fields. Checkbox off wins over
a leftover dialect so unchecking actually turns thinking off.
"""

from __future__ import annotations

from typing import Any

FORMAT_OFF = "off"
FORMAT_OPENAI_EFFORT = "openai-effort"
FORMAT_RESPONSES = "openai-responses"
FORMAT_DEEPSEEK = "deepseek"
FORMAT_ANTHROPIC = "anthropic"
FORMAT_GEMINI = "gemini"
FORMAT_QWEN = "qwen"

VALID_FORMATS = frozenset({
    FORMAT_OFF,
    FORMAT_OPENAI_EFFORT,
    FORMAT_RESPONSES,
    FORMAT_DEEPSEEK,
    FORMAT_ANTHROPIC,
    FORMAT_GEMINI,
    FORMAT_QWEN,
})

_PROTOCOL_DEFAULT = {
    "openai-responses": FORMAT_RESPONSES,
    "anthropic": FORMAT_ANTHROPIC,
    "google": FORMAT_GEMINI,
}

BUDGET_BY_EFFORT = {
    "none": 0,
    "minimal": 2_048,
    "low": 4_096,
    "medium": 8_192,
    "high": 16_000,
    "xhigh": 32_000,
    "max": 32_000,
}

# Wire spellings this dialect may send. Not a global enum — muse-spark
# Responses rejects ``max``; DeepSeek Chat wants it.
EFFORTS_OPENAI = ("none", "minimal", "low", "medium", "high", "xhigh")
EFFORTS_DEEPSEEK = ("high", "max")
EFFORTS_BUDGET = ("low", "medium", "high", "max")
EFFORTS_CHAT = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Old UI stored 最大 as ``max``. Responses' top slot is ``xhigh`` (same
# control, new spelling). Do not rewrite DeepSeek ``max``.
_LEFTOVER_EFFORT = {
    FORMAT_RESPONSES: {"max": "xhigh"},
}

DEFAULT_EFFORT = "high"

_ANSWER_FLOOR = 1_024


def normalize_thinking_format(value: str | None) -> str:
    """Empty string means auto. Unknown values become auto, not a guess dialect."""
    v = (value or "").strip().lower()
    if v in VALID_FORMATS:
        return v
    return ""


def resolve_thinking_format(
    thinking_format: str | None,
    *,
    supports_thinking: bool,
    protocol: str | None,
) -> str:
    """Return a concrete dialect (never empty)."""
    explicit = normalize_thinking_format(thinking_format)
    if explicit == FORMAT_OFF:
        return FORMAT_OFF
    if not supports_thinking:
        return FORMAT_OFF
    if explicit:
        return explicit
    proto = (protocol or "").strip().lower()
    if proto == "openai":
        proto = "openai-compatible"
    return _PROTOCOL_DEFAULT.get(proto, FORMAT_OPENAI_EFFORT)


def thinking_enabled(fmt: str) -> bool:
    return fmt != FORMAT_OFF


def offered_efforts(fmt: str) -> tuple[str, ...]:
    if fmt == FORMAT_RESPONSES:
        return EFFORTS_OPENAI
    if fmt == FORMAT_DEEPSEEK:
        return EFFORTS_DEEPSEEK
    if fmt in (FORMAT_ANTHROPIC, FORMAT_GEMINI, FORMAT_QWEN):
        return EFFORTS_BUDGET
    if fmt == FORMAT_OPENAI_EFFORT:
        return EFFORTS_CHAT
    return ()


def resolve_effort(effort: str | None, fmt: str) -> str | None:
    """Map stored effort onto this dialect's wire spelling.

    Empty → dialect default (``high``). Leftover ``max`` on Responses becomes
    ``xhigh`` (old 最大 slot). Unknown leftovers fall back to default so we
    never send a variant the dialect does not offer.
    """
    if fmt == FORMAT_OFF:
        return None
    offered = offered_efforts(fmt)
    e = (effort or "").strip().lower()
    if e == "":
        return DEFAULT_EFFORT if DEFAULT_EFFORT in offered else (offered[0] if offered else None)
    e = _LEFTOVER_EFFORT.get(fmt, {}).get(e, e)
    if e == "off":
        return "off"
    if offered and e not in offered:
        return DEFAULT_EFFORT if DEFAULT_EFFORT in offered else offered[0]
    return e


def budget_tokens(effort: str | None) -> int:
    e = (effort or "high").strip().lower()
    return BUDGET_BY_EFFORT.get(e, 16_000)


def clamp_budget(effort: str | None, max_tokens: int) -> int:
    """Keep budget_tokens / thinkingBudget strictly below the output cap."""
    wanted = budget_tokens(effort)
    cap = int(max_tokens or 0)
    if cap <= 1:
        return 1
    limit = cap - _ANSWER_FLOOR if cap > _ANSWER_FLOOR else max(1, cap // 2)
    return min(wanted, limit)


def emit_thinking_fields(
    body: dict[str, Any],
    fmt: str,
    effort: str | None,
    *,
    max_tokens: int,
) -> None:
    """Write this dialect's JSON onto ``body``. Auto aliases belong in resolve, not here."""
    if fmt == FORMAT_OFF or effort == "off":
        return
    if fmt == FORMAT_OPENAI_EFFORT:
        if effort:
            body["reasoning_effort"] = effort
        return
    if fmt == FORMAT_RESPONSES:
        if effort:
            body["reasoning"] = {"effort": effort}
        return
    if fmt == FORMAT_DEEPSEEK:
        body["thinking"] = {"type": "enabled"}
        if effort:
            body["reasoning_effort"] = effort
        return
    if fmt == FORMAT_QWEN:
        body["enable_thinking"] = True
        body["thinking_budget"] = clamp_budget(effort, max_tokens)
        return
    if fmt == FORMAT_ANTHROPIC:
        body["thinking"] = {
            "type": "enabled",
            "budget_tokens": clamp_budget(effort, max_tokens),
        }
        return
    if fmt == FORMAT_GEMINI:
        body["thinkingConfig"] = {
            "includeThoughts": True,
            "thinkingBudget": clamp_budget(effort, max_tokens),
        }


def apply_chat_thinking(
    body: dict[str, Any],
    fmt: str,
    effort: str | None,
    *,
    max_tokens: int,
) -> None:
    emit_thinking_fields(body, fmt, effort, max_tokens=max_tokens)


def apply_responses_thinking(
    body: dict[str, Any],
    fmt: str,
    effort: str | None,
    temperature: float,
    *,
    max_tokens: int,
) -> None:
    """Responses omits temperature while a thinking dialect is active."""
    if fmt == FORMAT_OFF or effort == "off":
        body["temperature"] = temperature
        return
    emit_thinking_fields(body, fmt, effort, max_tokens=max_tokens)


def apply_anthropic_thinking(
    body: dict[str, Any],
    fmt: str,
    effort: str | None,
    *,
    max_tokens: int,
) -> None:
    if fmt == FORMAT_OFF or effort == "off":
        return
    emit_thinking_fields(body, fmt, effort, max_tokens=max_tokens)
    if "thinking" in body:
        # 扩展思考与采样参数修改互斥：temperature 只能为 1（不发），
        # top_k 只能为 1、top_p 只能 0.95~1 —— 与其钳制不如直接不发。
        body.pop("temperature", None)
        body.pop("top_p", None)
        body.pop("top_k", None)


def apply_gemini_thinking(
    body: dict[str, Any],
    fmt: str,
    effort: str | None,
    *,
    max_tokens: int,
) -> None:
    if fmt == FORMAT_OFF or effort == "off":
        return
    extra: dict[str, Any] = {}
    emit_thinking_fields(extra, fmt, effort, max_tokens=max_tokens)
    cfg = extra.pop("thinkingConfig", None)
    if cfg is not None:
        gen = body.setdefault("generationConfig", {})
        gen["thinkingConfig"] = cfg
    body.update(extra)
