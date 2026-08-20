"""Wire protocol vs Base URL.

Base URL is the gateway prefix (stop at /v1). Protocol is a first-class
field that selects Chat Completions / Responses / Anthropic Messages / Gemini.

If a user pastes a full endpoint, strip the path suffix and infer protocol.
Saved authority is the cleaned prefix + provider_type. Request-time leftover
suffixes are only a belt for old rows — not an allowlist of model ids.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_CHAT = "openai-compatible"
PROTOCOL_OPENAI = "openai"
PROTOCOL_RESPONSES = "openai-responses"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_GOOGLE = "google"

VALID_PROTOCOLS = frozenset({
    PROTOCOL_CHAT,
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GOOGLE,
})

# Longest-first so /chat/completions wins over a hypothetical /completions.
_PATH_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("/chat/completions", PROTOCOL_CHAT),
    ("/responses", PROTOCOL_RESPONSES),
    ("/messages", PROTOCOL_ANTHROPIC),
)


def split_wire_endpoint(base_url: str) -> tuple[str, str | None]:
    """Return ``(prefix, inferred_protocol)``.

    Prefix has no trailing slash. ``inferred_protocol`` is set only when the
    URL already includes a known transport path.
    """
    base = (base_url or "").strip()
    if not base:
        return "", None
    no_frag = base.split("#", 1)[0]
    no_query = no_frag.split("?", 1)[0].rstrip("/")
    lower = no_query.lower()
    for suffix, proto in _PATH_SUFFIXES:
        if lower.endswith(suffix):
            prefix = no_query[: -len(suffix)].rstrip("/")
            return prefix, proto
    return no_query, None


def apply_wire_endpoint(
    base_url: str, provider_type: str | None
) -> tuple[str, str]:
    """Save-time: strip transport suffix; suffix inference wins when present."""
    prefix, inferred = split_wire_endpoint(base_url)
    explicit = (provider_type or "").strip()
    if inferred:
        protocol = inferred
    elif explicit in VALID_PROTOCOLS:
        protocol = explicit
    else:
        protocol = PROTOCOL_CHAT
    return prefix, protocol


def looks_like_responses_endpoint(base_url: str) -> bool:
    _, inferred = split_wire_endpoint(base_url)
    return inferred == PROTOCOL_RESPONSES


def looks_like_messages_endpoint(base_url: str) -> bool:
    _, inferred = split_wire_endpoint(base_url)
    return inferred == PROTOCOL_ANTHROPIC


def probe_base_url(base_url: str) -> str:
    """Prefix for GET /models. Also peels a leftover /completions."""
    prefix, _ = split_wire_endpoint(base_url)
    lower = prefix.lower()
    if lower.endswith("/completions"):
        return prefix[: -len("/completions")].rstrip("/")
    return prefix


def extract_nonstream_text(data: dict) -> str:
    """Pull assistant text from Chat / Responses / Anthropic / Gemini JSON."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = _content_to_text(msg.get("content"))
            if text:
                return text
    texts = _responses_output_text(data.get("output"))
    if not texts:
        nested = data.get("response")
        if isinstance(nested, dict):
            texts = _responses_output_text(nested.get("output"))
    if texts:
        return texts
    blocks = data.get("content") or []
    if isinstance(blocks, list):
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(str(t))
        if parts:
            return "".join(parts)
    candidates = data.get("candidates") or []
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        parts_in = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts_in, list):
            gem: list[str] = []
            for part in parts_in:
                if isinstance(part, dict) and part.get("text"):
                    gem.append(str(part["text"]))
            if gem:
                return "".join(gem)
    return ""


def is_responses_envelope(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("object") == "response":
        return True
    return isinstance(data.get("output"), list)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(str(t))
        return "".join(parts)
    return ""


def _responses_output_text(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "output_text" and item.get("text"):
            parts.append(str(item["text"]))
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("output_text", "text") and part.get("text"):
                parts.append(str(part["text"]))
    return "".join(parts)
