"""Permission management endpoints (contract 19, group 8).

契约 19: Permissions — 模式 + 规则 + 待审批
- GET  /api/permissions/{agentId}/mode       查 agent 权限模式
- PUT  /api/permissions/{agentId}/mode       更新权限模式
- GET  /api/permissions/{agentId}/rules      查工具规则（allow/deny/ask）
- GET  /api/permissions/pending/{agentId}    查 agent 待审批请求
- GET  /api/permissions/pending              查项目所有待审批（query: projectId）
- POST /api/permissions/requests/{id}/respond 响应审批请求
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.permission import permission_service
from hiveweave.services.approval import approval_service

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


class ModeUpdate(BaseModel):
    mode: str  # readonly | auto_approve | ask_user


class RespondBody(BaseModel):
    approved: bool
    remember: bool = False
    userNote: str | None = None


def _parse_json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@router.get("/{agent_id}/mode")
async def get_mode(agent_id: str) -> dict:
    """查 agent 权限模式。"""
    mode = await permission_service.get_permission_mode(agent_id)
    if mode is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"agentId": agent_id, "permissionMode": mode}


@router.put("/{agent_id}/mode")
async def update_mode(agent_id: str, body: ModeUpdate) -> dict:
    """更新 agent 权限模式。"""
    agent = await meta_db.get_agent_by_id(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    valid = {"readonly", "auto_approve", "ask_user"}
    if body.mode not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode; must be one of {sorted(valid)}",
        )
    await meta_db.execute(
        "UPDATE agents SET permission_mode = ?, updated_at = ? WHERE id = ?",
        [body.mode, int(time.time() * 1000), agent_id],
    )
    return {"ok": True, "agentId": agent_id, "permissionMode": body.mode}


@router.get("/{agent_id}/rules")
async def get_rules(agent_id: str) -> dict:
    """查 agent 工具规则（allow/deny/ask）。"""
    agent = await meta_db.get_agent_by_id(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "agentId": agent_id,
        "allowedTools": _parse_json_list(agent.get("allowed_tools")),
        "deniedTools": _parse_json_list(agent.get("denied_tools")),
        "askTools": _parse_json_list(agent.get("ask_tools")),
        "permissionMode": agent.get("permission_mode", "readonly"),
        "permissionType": agent.get("permission_type", "executor"),
    }


@router.get("/pending/project/{project_id}")
async def pending_for_project_path(project_id: str) -> dict:
    """COMPAT: 前端 api.ts 期望的 RESTful 路径。

    前端调用 GET /api/permissions/pending/project/{projectId}，
    原契约只提供 GET /api/permissions/pending?projectId=...。
    本路由必须定义在 /{agent_id} 之前，否则 "pending" 会被 {agent_id} 捕获。
    """
    requests = await approval_service.get_project_pending(project_id)
    return {"requests": requests}


@router.get("/pending/{agent_id}")
async def pending_for_agent(agent_id: str) -> dict:
    """查 agent 待审批请求。"""
    requests = await approval_service.get_pending_requests(agent_id)
    return {"requests": requests}


@router.get("/pending")
async def pending_for_project(projectId: str = Query(...)) -> dict:
    """查项目所有待审批。"""
    requests = await approval_service.get_project_pending(projectId)
    return {"requests": requests}


@router.post("/requests/{request_id}/respond")
async def respond_request(request_id: str, body: RespondBody) -> dict:
    """响应审批请求。"""
    result = await approval_service.resolve_request(
        request_id=request_id,
        approved=body.approved,
        remember=body.remember,
        user_note=body.userNote or "",
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400, detail=result.get("error", "Failed to resolve request")
        )
    return {"ok": True, "requestId": request_id}


# ── 前端 RESTful 路径参数兼容路由 ─────────────────────────────
# 前端期望 GET /api/permissions/{agentId}（无 /mode 后缀）风格；
# 保留现有 /{agentId}/mode 路由，额外提供 path 参数变体。
# 注意：本路由必须定义在所有字面量路径（/pending、/requests/...）之后，
# 以避免 {agent_id} 误捕获 "pending" 等字面量段。
# COMPAT: 前端 api.ts 期望的 RESTful 路径


@router.get("/{agent_id}")
async def get_mode_path(agent_id: str) -> dict:
    """查 agent 权限模式（path: agentId，无 /mode 后缀）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    return await get_mode(agent_id)
