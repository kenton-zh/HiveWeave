"""generate_image — Ark Agent Plan Seedream text-to-image.

Resolves model/base_url/api_key from Settings → 生图模型配置
(``image_gen_model_primary`` → llm_models row), POSTs to
``{plan_root}/images/generations``, downloads the result into the agent
workspace.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.services.ark_media import images_generations_url
from hiveweave.services.model import ModelService
from hiveweave.services.vision import resolve_screenshot_path
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger()

_DEFAULT_SIZE = "2K"
_DEFAULT_OUTPUT_FORMAT = "png"
_GEN_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB soft cap
_MAX_REDIRECTS = 5
_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

_SSRF_BLOCKED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",
    "metadata.google.internal",
})


class GenerateImageParams(BaseModel):
    """Parameters for generate_image."""

    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(
        description="Text prompt describing the image to generate.",
        json_schema_extra={"aliases": ["text", "description"]},
    )
    size: str = Field(
        default=_DEFAULT_SIZE,
        description='Output size: "2K", "4K", or WxH (e.g. "2048x2048"). Default 2K.',
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Optional workspace-relative path for the PNG "
            "(default: .hiveweave/generated/<timestamp>-<slug>.png)."
        ),
        json_schema_extra={"aliases": ["path", "file", "save_as"]},
    )
    watermark: bool = Field(
        default=False,
        description="Whether to add an AI watermark (default false).",
    )


def _slug_from_prompt(prompt: str, max_len: int = 40) -> str:
    cleaned = _SLUG_RE.sub("-", prompt.strip()).strip("-")
    if not cleaned:
        return "image"
    return cleaned[:max_len].rstrip("-") or "image"


def _default_rel_path(prompt: str, fmt: str = "png") -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return f".hiveweave/generated/{ts}-{_slug_from_prompt(prompt)}.{fmt}"


def _missing_config_err() -> ToolResult:
    return ToolResult.err(
        "No image-generation model configured. Open Settings → 模型配置 → "
        "生图模型设置, fill Model ID (Seedream id from Ark console), "
        "Base URL = Agent Plan root "
        "(https://ark.cn-beijing.volces.com/api/plan/v3), and API Key."
    )


def _is_ssrf_blocked(host: str) -> bool:
    """Block localhost / private / link-local / metadata hosts."""
    host_lower = host.lower().rstrip(".")
    if host_lower in _SSRF_BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host_lower)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
        )
    except ValueError:
        pass
    if host_lower.endswith(".internal") or host_lower.endswith(".local"):
        return True
    return False


def _validate_download_url(url: str) -> str | None:
    """Return error message if URL is unsafe; else None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Generated image URL has invalid scheme: {parsed.scheme!r}"
    if not parsed.hostname:
        return "Generated image URL has no hostname"
    if _is_ssrf_blocked(parsed.hostname):
        return f"Generated image URL blocked (internal host): {parsed.hostname}"
    return None


async def _download_image_bytes(url: str) -> tuple[bytes | None, str | None]:
    """SSRF-safe download with manual redirect checks and size cap.

    Returns ``(content, error)``.
    """
    err = _validate_download_url(url)
    if err:
        return None, err

    current = url
    try:
        async with httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT,
            trust_env=True,
            follow_redirects=False,
        ) as client:
            resp = await client.get(current)
            redirects = 0
            while resp.is_redirect and redirects < _MAX_REDIRECTS:
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                nxt = str(httpx.URL(current).join(loc))
                err = _validate_download_url(nxt)
                if err:
                    return None, f"Redirect blocked: {err}"
                current = nxt
                resp = await client.get(current)
                redirects += 1

            if resp.status_code >= 400:
                return None, (
                    f"Download of generated image failed "
                    f"HTTP {resp.status_code}."
                )

            cl = resp.headers.get("content-length")
            if cl:
                try:
                    if int(cl) > _MAX_DOWNLOAD_BYTES:
                        return None, (
                            f"Generated image Content-Length "
                            f"{cl} exceeds size cap "
                            f"({_MAX_DOWNLOAD_BYTES} bytes)."
                        )
                except ValueError:
                    pass

            # Stream into memory with running cap
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    return None, (
                        f"Generated image exceeds size cap "
                        f"({_MAX_DOWNLOAD_BYTES} bytes)."
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
    except httpx.HTTPError as e:
        return None, f"Generated image URL download failed: {e}"

    if not content:
        return None, "Downloaded generated image was empty."
    return content, None


@tool(
    "generate_image",
    "Generate an image via Ark Agent Plan Seedream (text-to-image). "
    "Pass prompt (+ optional size / output_path / watermark). Downloads the "
    "PNG into the workspace and returns the local path. Requires Settings → "
    "模型配置 → 生图模型配置. Only available to agents with source-write "
    "capability (executor / builder coordinator / QA).",
    requires_workspace=True,
    security_level="standard",
)
async def generate_image_tool(
    params: GenerateImageParams,
    agent_id: str,
    workspace: str,
) -> ToolResult:
    """Text-to-image via Ark Plan images/generations; save under workspace."""
    del agent_id
    prompt = (params.prompt or "").strip()
    if not prompt:
        return ToolResult.err("generate_image requires a non-empty prompt.")

    svc = ModelService()
    model = await svc.resolve_image_gen_model()
    if model is None:
        return _missing_config_err()

    api_key = (model.get("api_key") or "").strip()
    model_id = (model.get("model_id") or "").strip()
    endpoint = images_generations_url(model.get("base_url"))
    if not api_key or not model_id:
        return ToolResult.err(
            "Image-gen model row is incomplete (need model_id and api_key). "
            "Edit the model in Settings → 模型配置."
        )
    if not endpoint:
        return ToolResult.err(
            "Image-gen Base URL must be the Agent Plan root "
            "(https://ark.cn-beijing.volces.com/api/plan/v3). "
            "Do not use /api/v3 or /api/coding/v3. "
            f"Got: {(model.get('base_url') or '')[:120]!r}"
        )

    size = (params.size or _DEFAULT_SIZE).strip() or _DEFAULT_SIZE
    fmt = _DEFAULT_OUTPUT_FORMAT
    rel = (params.output_path or "").strip() or _default_rel_path(prompt, fmt)
    dest = resolve_screenshot_path(workspace, rel)
    if dest is None:
        return ToolResult.err(
            f"Invalid or out-of-workspace output_path: {rel!r}."
        )

    payload = {
        "model": model_id,
        "prompt": prompt,
        "size": size,
        "output_format": fmt,
        "response_format": "url",
        "watermark": bool(params.watermark),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=_GEN_TIMEOUT,
            trust_env=True,
        ) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
    except httpx.TimeoutException:
        return ToolResult.err(
            f"Image generation timed out talking to {endpoint}."
        )
    except httpx.HTTPError as e:
        return ToolResult.err(f"Image generation request failed: {e}")

    if resp.status_code >= 400:
        # Prefer structured message; never log Authorization
        body_preview = (resp.text or "")[:300]
        try:
            err_obj = resp.json()
            if isinstance(err_obj, dict):
                msg = (
                    err_obj.get("error", {}).get("message")
                    if isinstance(err_obj.get("error"), dict)
                    else err_obj.get("message") or err_obj.get("error")
                )
                if msg:
                    body_preview = str(msg)[:300]
        except Exception:
            pass
        return ToolResult.err(
            f"Image generation API returned HTTP {resp.status_code}: "
            f"{body_preview}"
        )

    try:
        data = resp.json()
    except ValueError:
        return ToolResult.err(
            "Image generation API returned non-JSON response."
        )

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return ToolResult.err(
            f"Image generation API response missing data[0]: {str(data)[:300]}"
        )
    first = items[0] if isinstance(items[0], dict) else {}
    image_url = (first.get("url") or "").strip()
    if not image_url:
        b64 = (first.get("b64_json") or "").strip()
        if b64:
            return _save_bytes(
                dest, base64.b64decode(b64, validate=False),
                workspace, model, size, endpoint, url_hint=None,
            )
        return ToolResult.err(
            f"Image generation response has no url: {str(first)[:300]}"
        )

    content, dl_err = await _download_image_bytes(image_url)
    if dl_err or content is None:
        return ToolResult.err(dl_err or "Download failed.")

    return _save_bytes(
        dest, content, workspace, model, size, endpoint, url_hint=image_url,
    )


def _save_bytes(
    dest: Path,
    content: bytes,
    workspace: str,
    model: dict,
    size: str,
    endpoint: str,
    *,
    url_hint: str | None,
) -> ToolResult:
    if not content:
        return ToolResult.err("Decoded/downloaded image was empty.")
    if len(content) > _MAX_DOWNLOAD_BYTES:
        return ToolResult.err(
            f"Generated image exceeds size cap "
            f"({len(content)} > {_MAX_DOWNLOAD_BYTES} bytes)."
        )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError as e:
        return ToolResult.err(f"Failed to write image to {dest}: {e}")

    root = Path(workspace).resolve()
    try:
        rel_out = str(dest.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        rel_out = str(dest).replace("\\", "/")

    log.info(
        "generate_image_ok",
        model=model.get("model_id"),
        path=rel_out,
        bytes=len(content),
        endpoint=endpoint[:80],
    )
    # Omit full signed CDN URL from tool history — local path is enough.
    extra: dict = {
        "path": rel_out,
        "model_id": model.get("model_id"),
        "model_name": model.get("name"),
        "size": size,
    }
    if url_hint:
        extra["url_host"] = urlparse(url_hint).hostname
    return ToolResult.ok(
        f"Image generated and saved to {rel_out} ({len(content)} bytes).",
        **extra,
    )
