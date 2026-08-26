"""Known-provider preset endpoints (model config UI).

契约 19: Provider Presets — 知名服务商预设
- GET /api/provider-presets        列出全部预设（含模型清单与能力标记）
- GET /api/provider-presets/{id}   查单个预设

预设数据见 ``llm/provider_presets.py``（来源 pi-ai 内置目录，非猜测）。
知名服务商只需 API Key；base_url / 模型能力全部预置。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import structlog

from hiveweave.llm.provider_presets import get_preset, list_presets

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/provider-presets", tags=["provider-presets"])


@router.get("")
async def list_provider_presets() -> dict:
    """列出全部服务商预设（展示顺序即宫格顺序）。"""
    return {"presets": list_presets()}


@router.get("/{preset_id}")
async def get_provider_preset(preset_id: str) -> dict:
    """查单个服务商预设。"""
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"preset": preset}
