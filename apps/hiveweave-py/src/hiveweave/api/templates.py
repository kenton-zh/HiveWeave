"""Agent template registry endpoints (contract 19, group 10).

契约 19: Extra — Templates — Agent 模板库
- GET /api/agent-templates            列出模板（query: division?、role?）
- GET /api/agent-templates/divisions  列出所有部门
- GET /api/agent-templates/{id}       查单个模板
- POST /api/agent-templates           创建模板
- DELETE /api/agent-templates/{id}    删除模板
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.services.template import TemplateService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/agent-templates", tags=["templates"])

_template = TemplateService()


class TemplateCreate(BaseModel):
    """创建模板请求体。"""

    name: str
    source: str | None = None
    division: str | None = None
    role: str | None = None
    color: str | None = None
    emoji: str | None = None
    vibe: str | None = None
    description: str | None = None
    promptBody: str | None = None


def _template_response(t: dict) -> dict:
    """同时输出 snake_case 与 camelCase 字段。"""
    return {
        "id": t.get("id"),
        "source": t.get("source"),
        "division": t.get("division"),
        "name": t.get("name"),
        "role": t.get("role"),
        "color": t.get("color"),
        "emoji": t.get("emoji"),
        "vibe": t.get("vibe"),
        "description": t.get("description"),
        "prompt_body": t.get("prompt_body"),
        "promptBody": t.get("prompt_body"),
        "created_at": t.get("created_at"),
        "createdAt": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "updatedAt": t.get("updated_at"),
    }


@router.get("")
async def list_templates(
    division: str | None = Query(default=None),
    role: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict:
    """列出模板（支持 division / role / search 过滤）。"""
    opts: dict = {}
    if division:
        opts["division"] = division
    if search:
        opts["search"] = search
    templates = await _template.list_all(opts)
    # role 过滤（service 未支持 role，在此内存过滤）
    if role:
        templates = [t for t in templates if t.get("role") == role]
    return {"templates": [_template_response(t) for t in templates]}


@router.get("/divisions")
async def list_divisions() -> dict:
    """列出所有部门（去重排序）。"""
    templates = await _template.list_all()
    divisions = sorted({t.get("division") for t in templates if t.get("division")})
    return {"divisions": divisions}


@router.get("/{template_id}")
async def get_template(template_id: str) -> dict:
    """查单个模板。"""
    t = await _template.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": _template_response(t)}


@router.post("")
async def create_template(body: TemplateCreate) -> dict:
    """创建模板。"""
    attrs = body.model_dump(exclude_none=True)
    # camelCase → snake_case
    if "promptBody" in attrs:
        attrs["prompt_body"] = attrs.pop("promptBody")
    try:
        result = await _template.create(attrs)
    except Exception as e:
        log.error("create_template_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create template")
    return {"ok": True, "id": result["id"]}


@router.delete("/{template_id}")
async def delete_template(template_id: str) -> dict:
    """删除模板。"""
    t = await _template.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        await _template.delete(template_id)
    except Exception as e:
        log.error("delete_template_failed", template_id=template_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete template")
    return {"ok": True}
