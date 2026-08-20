"""look_at_image — 帮你看图片：无状态多模态识图工具。

Agent 传入图片路径 + prompt；工具加载图片，一次性非流式调用
Settings 里配置的多模态模型，把完整文本结果返回给调用方后结束。
不把像素注入主对话历史。主用失败时自动切备用（同 api_key 跳过）。
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.services.model import ModelService
from hiveweave.services.vision import (
    analyze_image,
    load_image_for_llm,
    resolve_screenshot_path,
    resolve_screenshot_under_project,
)
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger()


class LookAtImageParams(BaseModel):
    """Parameters for look_at_image (帮你看图片)."""

    model_config = ConfigDict(populate_by_name=True)

    image_path: str | None = Field(
        default=None,
        description=(
            "Path to the image file (PNG/JPEG/GIF/WebP/BMP). "
            "Relative to the agent workspace, or absolute under the workspace. "
            "Optional if attestation_id is set."
        ),
        json_schema_extra={"aliases": ["path", "file", "screenshot", "image"]},
    )
    attestation_id: str | None = Field(
        default=None,
        alias="attestationId",
        description=(
            "tool_attestations id whose artifact_hashes.screenshot_path "
            "points at the PNG (cross-worktree). Prefer this over copying "
            "another agent's absolute path."
        ),
        json_schema_extra={"aliases": ["attestationId", "att_id"]},
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
    "帮你看图片 — Optional one-shot: load an image and ask a vision-capable "
    "model (dedicated vision slot, else the management chat model). "
    "Does not replace screenshots already injected into your chat. "
    "Pass image_path + prompt, or attestation_id for another agent's PNG. "
    "Returns text only; does not inject pixels into chat history.",
    requires_workspace=True,
    security_level="standard",
)
async def look_at_image_tool(
    params: LookAtImageParams,
    agent_id: str,
    workspace: str,
) -> ToolResult:
    """帮你看图片 — one-shot vision analyze with primary→backup failover."""
    prompt = (params.prompt or "").strip()
    if not prompt:
        return ToolResult.err(
            "look_at_image requires a non-empty prompt "
            "(what should the vision model look for / how to answer)."
        )

    resolved = await _resolve_look_at_image_path(params, agent_id, workspace)
    if isinstance(resolved, str):
        return ToolResult.err(resolved)

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
            "look_at_image has no model to call (dedicated vision slots "
            "empty/stale, and the management chat model could not be "
            "resolved). Browse screenshots still inject into the main "
            "chat when that model accepts images; this tool is optional."
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


async def _resolve_look_at_image_path(
    params: LookAtImageParams,
    agent_id: str,
    workspace: str,
) -> Path | str:
    """Return a sandboxed Path, or an error string.

    ``attestation_id`` loads screenshot_path from artifact_hashes and
    sandboxes under the project root (incl. ``.hiveweave/worktrees/``).
    Bare ``image_path`` stays sandboxed to this agent's workspace.
    """
    att_id = (params.attestation_id or "").strip()
    if att_id:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.attestation import (
            attestation_service,
            screenshot_path_from_artifact_hashes,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if not project_id:
            return "Cannot resolve project for this agent."
        row = await attestation_service.get(project_id, att_id)
        if not row:
            return f"Attestation not found: {att_id}"
        shot = screenshot_path_from_artifact_hashes(row.get("artifact_hashes"))
        if not shot:
            return (
                f"Attestation {att_id} has no screenshot_path in "
                "artifact_hashes. Re-run browse screenshot so the path "
                "is stored."
            )
        project_root = await meta_db.get_project_workspace(project_id)
        resolved = resolve_screenshot_under_project(project_root, shot)
        if resolved is None:
            return (
                "Screenshot path from attestation is outside the project "
                f"(no .. escape): {shot!r}."
            )
        return resolved

    raw_path = (params.image_path or "").strip()
    if not raw_path:
        return (
            "look_at_image requires image_path, or attestation_id "
            "to load another agent's screenshot."
        )
    resolved = resolve_screenshot_path(workspace, raw_path)
    if resolved is None:
        return (
            f"Invalid or out-of-workspace image_path: {raw_path!r}. "
            "Path must be under the agent workspace (no .. escape). "
            "To inspect another agent's screenshot, pass attestation_id."
        )
    return resolved


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
