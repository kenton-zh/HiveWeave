"""look_at_image — 帮你看图片：无状态多模态识图工具。

Agent 传入图片路径 + prompt；工具加载图片，一次性非流式调用
Settings 里配置的多模态模型，把完整文本结果返回给调用方后结束。
不把像素注入主对话历史。主用失败时自动切备用（同 api_key 跳过）。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.services.model import ModelService
from hiveweave.services.vision import (
    analyze_image,
    load_image_for_llm,
    resolve_screenshot_path,
)
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger()


class LookAtImageParams(BaseModel):
    """Parameters for look_at_image (帮你看图片)."""

    model_config = ConfigDict(populate_by_name=True)

    image_path: str = Field(
        description=(
            "Path to the image file (PNG/JPEG/GIF/WebP/BMP). "
            "Relative to the agent workspace, or absolute under the workspace."
        ),
        json_schema_extra={"aliases": ["path", "file", "screenshot", "image"]},
    )
    prompt: str = Field(
        description=(
            "Instruction for the multimodal model: what to focus on, "
            "how to answer, required format, etc."
        ),
        json_schema_extra={"aliases": ["question", "query", "instruction"]},
    )


@tool(
    "look_at_image",
    "帮你看图片 — Load an image and ask the configured multimodal model to "
    "describe or analyze it. Stateless one-shot: waits for the full answer "
    "(non-streaming) then returns text only. Pass image_path + prompt "
    "(focus areas / output format). Does NOT inject pixels into your chat "
    "history. Configure the vision model in Settings → 多模态模型配置.",
    requires_workspace=True,
    security_level="standard",
)
async def look_at_image_tool(
    params: LookAtImageParams,
    agent_id: str,
    workspace: str,
) -> ToolResult:
    """帮你看图片 — one-shot vision analyze with primary→backup failover."""
    del agent_id  # required by @tool signature; unused
    prompt = (params.prompt or "").strip()
    if not prompt:
        return ToolResult.err(
            "look_at_image requires a non-empty prompt "
            "(what should the vision model look for / how to answer)."
        )

    resolved = resolve_screenshot_path(workspace, params.image_path)
    if resolved is None:
        return ToolResult.err(
            f"Invalid or out-of-workspace image_path: {params.image_path!r}. "
            "Path must exist under the agent workspace (no .. escape)."
        )

    image = load_image_for_llm(resolved)
    if image is None:
        return ToolResult.err(
            f"Could not load image at {resolved}: missing, not an image "
            "suffix, empty, or over size cap (2MB)."
        )

    svc = ModelService()
    model = await svc.resolve_vision_model()
    if model is None:
        return ToolResult.err(
            "No multimodal model configured. Open Settings → 模型配置 → "
            "多模态模型配置, set 主用模型 (vision_model_primary), then retry."
        )

    try:
        text = await analyze_image(
            image=image,
            prompt=prompt,
            model_config=model,
        )
    except Exception as primary_err:
        text, model, failover_err = await _try_vision_backup(  # type: ignore[assignment]
            svc,
            primary=model,
            image=image,
            prompt=prompt,
            primary_err=primary_err,
        )
        if failover_err is not None:
            return ToolResult.err(failover_err)

    assert text is not None and model is not None
    return ToolResult.ok(
        text,
        model_name=model.get("name"),
        model_id=model.get("model_id"),
        image_path=str(resolved),
    )


async def _try_vision_backup(
    svc: ModelService,
    *,
    primary: dict,
    image: dict[str, str],
    prompt: str,
    primary_err: BaseException,
) -> tuple[str | None, dict | None, str | None]:
    """On primary failure, try backup once. Returns (text, model, error)."""
    primary_id = primary.get("id")
    skip: set[str] = {primary_id} if primary_id else set()
    backup = await svc.resolve_vision_model(skip_model_ids=skip)
    primary_label = primary.get("name") or primary.get("model_id") or primary_id

    if backup is None:
        return None, None, (
            f"Vision model call failed ({primary_label}): {primary_err}"
        )

    primary_key = primary.get("api_key") or ""
    backup_key = backup.get("api_key") or ""
    if primary_key and backup_key == primary_key:
        return None, None, (
            f"Vision model call failed ({primary_label}): {primary_err}. "
            "Backup shares the same api_key — skipped (would not help quota)."
        )

    backup_label = backup.get("name") or backup.get("model_id") or backup.get("id")
    log.info(
        "vision_failover_backup",
        primary=primary_label,
        backup=backup_label,
        error=str(primary_err)[:200],
    )
    try:
        text = await analyze_image(
            image=image,
            prompt=prompt,
            model_config=backup,
        )
        return text, backup, None
    except Exception as backup_err:
        return None, None, (
            f"Vision primary and backup failed. "
            f"primary ({primary_label}): {primary_err}; "
            f"backup ({backup_label}): {backup_err}"
        )
