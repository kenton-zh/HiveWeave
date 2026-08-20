"""Multimodal screenshot helpers — inject real pixels into LLM context.

Screenshots used to be path-only tool text. Multimodal models still cannot
"see" a file path. This module loads PNGs/JPEGs as base64 image payloads and
keeps history from exploding by retaining only the newest few images.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Soft cap — many gateways reject multi-MB inline images.
MAX_SCREENSHOT_BYTES = 2_000_000
# Keep only the newest N image-bearing messages in an active tool loop.
KEEP_LAST_IMAGES = 2

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def guess_media_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return mime
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def resolve_screenshot_under_project(
    project_root: str | None, raw: str | None
) -> Path | None:
    """Resolve a screenshot under the project root (incl. agent worktrees).

    Unlike :func:`resolve_screenshot_path` (agent-workspace sandbox), this
    allows ``<project>/.hiveweave/worktrees/<sid>/...`` so a reviewer can
    inspect an assignee screenshot stored on an attestation. ``..`` that
    escapes the project root is rejected.
    """
    return resolve_screenshot_path(project_root, raw)


def resolve_screenshot_path(workspace: str | None, raw: str | None) -> Path | None:
    """Resolve a screenshot path and sandbox it under ``workspace``.

    Absolute paths and ``..`` escapes outside the workspace are rejected
    (same contract as ``create_doc_review``).
    """
    if not raw or not str(raw).strip():
        return None
    if not workspace or not str(workspace).strip():
        return None
    try:
        root = Path(workspace).resolve()
    except OSError:
        return None
    candidate = Path(str(raw).strip().strip("\"'"))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        log.warning(
            "vision.path_escape",
            path=str(resolved),
            workspace=str(root),
        )
        return None
    return resolved


def load_image_for_llm(
    path: Path | str,
    *,
    max_bytes: int = MAX_SCREENSHOT_BYTES,
) -> dict[str, str] | None:
    """Load an image file into ``{media_type, data}`` (base64) for providers.

    Returns None when missing, non-image, or over size cap (caller should
    still keep the text path in the tool result).
    """
    p = Path(path)
    if not p.is_file():
        log.info("vision.image_missing", path=str(p))
        return None
    if p.suffix.lower() not in _IMAGE_SUFFIXES:
        log.info("vision.not_image_suffix", path=str(p), suffix=p.suffix)
        return None
    try:
        size = p.stat().st_size
    except OSError as e:
        log.warning("vision.stat_failed", path=str(p), error=str(e))
        return None
    if size <= 0:
        return None
    if size > max_bytes:
        log.warning(
            "vision.image_too_large",
            path=str(p),
            bytes=size,
            max_bytes=max_bytes,
        )
        return None
    try:
        raw = p.read_bytes()
    except OSError as e:
        log.warning("vision.read_failed", path=str(p), error=str(e))
        return None
    return {
        "media_type": guess_media_type(p),
        "data": base64.b64encode(raw).decode("ascii"),
        "path": str(p),
    }


def strip_images_from_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = KEEP_LAST_IMAGES,
) -> list[dict[str, Any]]:
    """Drop ``images`` from all but the newest ``keep_last`` image messages.

    Compaction-only: rewriting older message bodies (pixels + the
    ``[image stripped…]`` note) invalidates DeepSeek prefix cache from that
    token. The tool loop must call this at overflow, not every round.
    Does not mutate the input list in place.
    """
    if keep_last < 0:
        keep_last = 0
    indexed = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("images")
    ]
    if len(indexed) <= keep_last:
        return messages
    drop = set(indexed[: max(0, len(indexed) - keep_last)])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in drop and isinstance(m, dict) and "images" in m:
            cleaned = {k: v for k, v in m.items() if k != "images"}
            note = cleaned.get("content") or ""
            if "[image stripped" not in note:
                cleaned["content"] = (
                    f"{note}\n[image stripped from older context — "
                    f"re-screenshot if you still need pixels]"
                ).strip()
            out.append(cleaned)
        else:
            out.append(m)
    return out


def messages_without_images(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strip all image payloads (for conversation-store persistence)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict) and "images" in m:
            cleaned = {k: v for k, v in m.items() if k != "images"}
            out.append(cleaned)
        else:
            out.append(m)
    return out


def openai_image_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI Chat Completions image_url parts from internal image dicts."""
    parts: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        media = img.get("media_type") or "image/png"
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        })
    return parts


def anthropic_image_blocks(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic content blocks for images."""
    blocks: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type") or "image/png",
                "data": data,
            },
        })
    return blocks


def gemini_image_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini generativeLanguage inlineData parts."""
    parts: list[dict[str, Any]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        data = img.get("data") or ""
        if not data:
            continue
        parts.append({
            "inlineData": {
                "mimeType": img.get("media_type") or "image/png",
                "data": data,
            },
        })
    return parts


def _text_from_message(msg: dict[str, Any]) -> str:
    """Prefer visible content; fall back to reasoning/thinking fields."""
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                bits.append(part["text"])
        joined = "".join(bits)
        if joined.strip():
            return joined
    for key in ("reasoning_content", "reasoning", "thinking"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def extract_nonstream_text(data: dict[str, Any]) -> str:
    """Pull assistant text from a non-streaming chat completion body.

    Supports OpenAI ``choices``, Anthropic ``content`` blocks, and Gemini
    ``candidates`` — same providers as ``provider_factory``.
    When ``message.content`` is empty (common with thinking models), falls
    back to ``reasoning_content`` / ``thinking``.
    """
    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = _text_from_message(msg)
            if text:
                return text

    content = data.get("content")
    if isinstance(content, list):
        bits = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                bits.append(block["text"])
        if bits:
            return "".join(bits)
    if isinstance(content, str) and content.strip():
        return content

    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        c0 = candidates[0] if isinstance(candidates[0], dict) else None
        parts = (c0 or {}).get("content", {}).get("parts") if c0 else None
        if isinstance(parts, list):
            bits = [
                p["text"]
                for p in parts
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            return "".join(bits)

    return ""


async def analyze_image(
    *,
    image: dict[str, str],
    prompt: str,
    model_config: dict[str, Any],
    timeout_s: float = 120.0,
) -> str:
    """One-shot non-streaming multimodal call. Stateless — no history.

    ``image`` is ``{media_type, data}`` from :func:`load_image_for_llm`.
    Returns the full assistant text (never streams to the caller).
    Thinking/reasoning mode is forced off so answers land in ``content``.
    """
    import httpx

    from hiveweave.llm.provider import provider_factory
    from hiveweave.llm.retry import (
        RetryHandler,
        RetryableError,
        is_retryable_status,
    )

    # Vision one-shot wants visible content, not a thinking-only body.
    cfg = dict(model_config)
    cfg["supports_thinking"] = False
    cfg["default_reasoning_effort"] = None
    # vision 槽位语义即多模态：强制放行图像注入，防止模型行 supports_images=0
    # （出厂默认值）把图片静默剥掉，导致 look_at_image 只发文字提示词。
    cfg["supports_images"] = True

    provider = provider_factory.create(cfg)
    body = provider.build_body(
        messages=[
            {
                "role": "user",
                "content": prompt.strip(),
                "images": [image],
            },
        ],
        stream=False,
        temperature=0.2,
        tools=None,
    )
    headers = provider.build_headers()
    headers["Accept"] = "application/json"

    async def _once() -> str:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=timeout_s, write=10.0, pool=10.0
            ),
        ) as client:
            resp = await client.post(
                provider.build_url(),
                json=body,
                headers=headers,
            )
            # 与流式路径同口径：429/5xx 瞬态错误交给 RetryHandler 指数退避重试
            # （含 Retry-After）。之前 raise_for_status 直接抛，视觉门禁一遇到
            # 限流就废掉 → 团队只能 waive visual/module_visual。
            if is_retryable_status(resp.status_code):
                raise RetryableError(
                    f"vision HTTP {resp.status_code}: {resp.text[:500]}",
                    status=resp.status_code,
                    headers=dict(resp.headers),
                )
            resp.raise_for_status()
            data = resp.json()
        text = extract_nonstream_text(data).strip()
        if not text:
            raise RuntimeError("Vision model returned empty content")
        return text

    return await RetryHandler(
        on_retry=lambda attempt, delay_ms, exc: log.info(
            "vision_http_retry",
            attempt=attempt,
            delay_ms=delay_ms,
            error=str(exc)[:200],
        )
    ).with_retry(_once)

