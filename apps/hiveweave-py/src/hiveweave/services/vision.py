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

    Prevents tool-loop context from accumulating multi-MB base64 blobs across
    many screenshot rounds. Does not mutate the input list in place.
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
