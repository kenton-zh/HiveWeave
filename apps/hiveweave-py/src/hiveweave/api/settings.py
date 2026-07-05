"""Global settings CRUD endpoints (contract 19, group 3).

契约 19: Settings — 全局键值设置 CRUD
- GET    /api/settings        列出所有设置
- GET    /api/settings/{key}  查单个设置
- POST   /api/settings        upsert
- PUT    /api/settings        upsert（同 POST）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import structlog

from hiveweave.services.settings import SettingsService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_settings = SettingsService()


class SettingUpsert(BaseModel):
    """upsert 请求体。"""

    key: str
    value: str | int | bool | None = None


class SettingOut(BaseModel):
    """单条设置响应。"""

    key: str
    value: str | None = None
    updated_at: int | None = None
    # camelCase 别名
    updatedAt: int | None = Field(default=None, alias="updated_at")


def _to_setting_out(key: str, value: str | None, updated_at: int | None) -> dict:
    """构造同时含 snake_case 与 camelCase 字段的响应 dict。"""
    return {
        "key": key,
        "value": value,
        "updated_at": updated_at,
        "updatedAt": updated_at,
    }


@router.get("")
async def list_settings() -> dict:
    """列出所有全局设置。"""
    try:
        all_kv = await _settings.list_all()
    except Exception as e:
        log.error("list_settings_failed", error=str(e))
        return {"settings": []}
    # list_all 仅返回 {key: value}，需补 updated_at
    settings_list = []
    for k, v in all_kv.items():
        settings_list.append(_to_setting_out(k, v, None))
    return {"settings": settings_list}


@router.get("/{key}")
async def get_setting(key: str) -> dict:
    """查单个设置。"""
    value = await _settings.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    return {"setting": _to_setting_out(key, value, None)}


@router.post("")
async def upsert_setting_post(body: SettingUpsert) -> dict:
    """upsert 设置（POST）。"""
    try:
        stored = await _settings.set(body.key, body.value if body.value is not None else "")
    except Exception as e:
        log.error("upsert_setting_failed", key=body.key, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to upsert setting")
    return {"setting": _to_setting_out(body.key, stored, None)}


@router.put("")
async def upsert_setting_put(body: SettingUpsert) -> dict:
    """upsert 设置（PUT，同 POST）。"""
    return await upsert_setting_post(body)
