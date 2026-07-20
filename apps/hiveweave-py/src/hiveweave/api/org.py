"""Organization / agent CRUD endpoints (contract 19, group 5).

契约 19: Org / Agents — Agent CRUD + 树 + 子节点 + 模块
- GET    /api/org                       组织树（query: projectId）
- GET    /api/org/agents                列出 agent（query: projectId）
- GET    /api/org/agents/{id}           查单个 agent
- GET    /api/org/agents/{id}/children  查直接子节点
- POST   /api/org/agents                创建 agent
- PATCH  /api/org/agents/{id}           更新 agent
- PUT    /api/org/agents/{id}           同 PATCH
- DELETE /api/org/agents/{id}           删除 agent
- GET    /api/org/modules               列出项目模块
- POST   /api/org/agents/{id}/dismiss   软删除（归档）
- POST   /api/org/agents/{id}/transfer  转移上级
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.api.auth import validate_id
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.org import OrgService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/org", tags=["org"])

_org = OrgService()


class AgentCreate(BaseModel):
    """创建 agent 请求体。"""

    name: str
    projectId: str
    role: str = "executor"
    parentId: str | None = None
    goal: str | None = None
    backstory: str | None = None
    permissionType: str = "executor"
    permissionMode: str | None = None
    modelId: str | None = None
    moduleId: str | None = None
    templateId: str | None = None
    # P0 Hard Gates: REST hire must name an actor with staffing capability
    actorAgentId: str | None = None


class AgentUpdate(BaseModel):
    """更新 agent 请求体（所有字段可选）。"""

    name: str | None = None
    goal: str | None = None
    status: str | None = None
    backstory: str | None = None
    modelId: str | None = None
    parentId: str | None = None
    permissionType: str | None = None
    permissionMode: str | None = None
    moduleId: str | None = None


class TransferBody(BaseModel):
    newParentId: str | None = None


async def _resolve_project_language(project_id: str | None) -> str:
    """从 per-project DB project_meta 读项目语言（真相源）。

    meta_db.projects 不再存此列 — 真相源归一到 per-project DB 的 project_meta.
    """
    if not project_id:
        return "en"
    try:
        pj_conn = await project_db.get_project_db_by_project_id(project_id)
    except project_db.ProjectDbError:
        return "en"
    try:
        pj_cursor = await pj_conn.execute(
            "SELECT language FROM project_meta WHERE project_id = ?",
            [project_id],
        )
        pj_row = await pj_cursor.fetchone()
        await pj_cursor.close()
        if pj_row and pj_row["language"]:
            return pj_row["language"]
    except Exception as e:
        log.warning("org.read_project_language_failed",
                    project_id=project_id, error=str(e))
    return "en"


def _agent_response(a: dict, *, project_language: str = "en") -> dict:
    """同时输出 snake_case 与 camelCase 字段。

    language 优先取 agents.language（向后兼容旧行），
    缺失时回退到 project_meta.language（真相源）。
    """
    lang = a.get("language") or project_language
    return {
        "id": a.get("id"),
        "short_id": a.get("short_id"),
        "shortId": a.get("short_id"),
        "project_id": a.get("project_id"),
        "projectId": a.get("project_id"),
        "name": a.get("name"),
        "role": a.get("role"),
        "parent_id": a.get("parent_id"),
        "parentId": a.get("parent_id"),
        "module_id": a.get("module_id"),
        "moduleId": a.get("module_id"),
        "status": a.get("status", "active"),
        "goal": a.get("goal", ""),
        "backstory": a.get("backstory", ""),
        "skills": a.get("skills", []),
        "model_id": a.get("model_id"),
        "modelId": a.get("model_id"),
        "permission_type": a.get("permission_type", "executor"),
        "permissionType": a.get("permission_type", "executor"),
        "permission_mode": a.get("permission_mode", "readonly"),
        "permissionMode": a.get("permission_mode", "readonly"),
        "allowed_tools": a.get("allowed_tools", []),
        "allowedTools": a.get("allowed_tools", []),
        "denied_tools": a.get("denied_tools", []),
        "deniedTools": a.get("denied_tools", []),
        "ask_tools": a.get("ask_tools", []),
        "askTools": a.get("ask_tools", []),
        "mcp_servers": a.get("mcp_servers", []),
        "mcpServers": a.get("mcp_servers", []),
        "bound_skills": a.get("bound_skills", []),
        "boundSkills": a.get("bound_skills", []),
        "reasoning_effort": a.get("reasoning_effort"),
        "reasoningEffort": a.get("reasoning_effort"),
        "workspace_path": a.get("workspace_path"),
        "workspacePath": a.get("workspace_path"),
        "language": lang,
        "created_at": a.get("created_at"),
        "createdAt": a.get("created_at"),
        "updated_at": a.get("updated_at"),
        "updatedAt": a.get("updated_at"),
        "last_active_at": a.get("last_active_at"),
        "lastActiveAt": a.get("last_active_at"),
    }


def _normalize_agent_attrs(body: BaseModel) -> dict:
    """camelCase 请求体 → snake_case service 层 dict。"""
    data = body.model_dump(exclude_none=True)
    mapping = {
        "projectId": "project_id",
        "parentId": "parent_id",
        "permissionType": "permission_type",
        "permissionMode": "permission_mode",
        "modelId": "model_id",
        "moduleId": "module_id",
        "templateId": "template_id",
    }
    out: dict = {}
    for k, v in data.items():
        out[mapping.get(k, k)] = v
    return out


@router.get("")
async def get_tree(projectId: str | None = Query(default=None)) -> dict:
    """组织树（query: projectId）。"""
    if not projectId:
        return {"tree": []}
    tree = await _org.get_full_tree(projectId)
    return {"tree": tree}


@router.get("/agents")
async def list_agents(
    projectId: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
) -> dict:
    """列出 agent（query: projectId 或 project_id）。"""
    pid = projectId or project_id
    agents = await _org.list_agents(pid)
    pl = await _resolve_project_language(pid)
    return {"agents": [_agent_response(a, project_language=pl) for a in agents]}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """查单个 agent（支持 short_id / UUID / UUID 前缀）。"""
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    pl = await _resolve_project_language(agent.get("project_id"))
    return {"agent": _agent_response(agent, project_language=pl)}


@router.get("/agents/{agent_id}/children")
async def get_children(agent_id: str) -> dict:
    """查直接子节点。"""
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    pl = await _resolve_project_language(agent.get("project_id"))
    children = await _org.get_subordinates(agent["id"])
    return {"children": [_agent_response(c, project_language=pl) for c in children]}


@router.post("/agents")
async def create_agent(body: AgentCreate) -> dict:
    """创建 agent（short_id 自动生成）。

    P0: requires actorAgentId with staffing capability; validate_hire runs
    inside OrgService.create_agent (unless bootstrap seed).
    """
    actor_id = (body.actorAgentId or "").strip()
    if not actor_id:
        raise HTTPException(
            status_code=400,
            detail="actorAgentId is required to create agents (staffing gate)",
        )
    actor = await _org.resolve_agent(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"Actor agent not found: {actor_id}")

    from hiveweave.services.policy import policy_service

    hard = policy_service.hard_check(actor, "hire_agent", {})
    if hard:
        raise HTTPException(status_code=403, detail=hard)

    attrs = _normalize_agent_attrs(body)
    attrs.pop("actorAgentId", None)
    attrs.pop("actor_agent_id", None)
    if "template_id" in attrs:
        attrs.pop("template_id", None)  # 模板预填由 HR 工具处理，此处忽略
    try:
        agent = await _org.create_agent(attrs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.error("create_agent_failed", error=str(e))
        raise HTTPException(status_code=422, detail=f"Failed to create agent: {e}")
    pl = await _resolve_project_language(agent.get("project_id"))
    return {"agent": _agent_response(agent, project_language=pl)}


async def _do_update_agent(agent_id: str, body: AgentUpdate) -> dict:
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    attrs = _normalize_agent_attrs(body)
    try:
        updated = await _org.update_agent(agent["id"], attrs)
    except Exception as e:
        log.error("update_agent_failed", agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=422, detail=f"Failed to update agent: {e}")
    pl = await _resolve_project_language(agent.get("project_id"))
    return {"agent": _agent_response(updated or {}, project_language=pl)}


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: AgentUpdate) -> dict:
    """更新 agent（PATCH）。"""
    return await _do_update_agent(agent_id, body)


@router.put("/agents/{agent_id}")
async def put_agent(agent_id: str, body: AgentUpdate) -> dict:
    """更新 agent（PUT，同 PATCH）。"""
    return await _do_update_agent(agent_id, body)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict:
    """删除 agent（硬删除，拒绝有下属的 agent）。"""
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    result = await _org.delete_agent(agent["id"])
    if not result.get("success", False):
        raise HTTPException(status_code=500, detail=result.get("message", "Failed"))
    return {"ok": True}


@router.post("/agents/{agent_id}/dismiss")
async def dismiss_agent(agent_id: str) -> dict:
    """软删除（归档）agent。"""
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    project_id = agent.get("project_id") or ""
    result = await _org.dismiss_agent(project_id, agent["id"])
    if not result.get("success", False):
        raise HTTPException(
            status_code=400, detail=result.get("message", "Failed to dismiss")
        )
    pl = await _resolve_project_language(project_id)
    return {"ok": True, "agent": _agent_response(result.get("agent", {}), project_language=pl)}


@router.post("/agents/{agent_id}/transfer")
async def transfer_agent(agent_id: str, body: TransferBody) -> dict:
    """转移 agent 到新上级（带环检测）。"""
    validate_id(agent_id, "agent_id")
    agent = await _org.resolve_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    project_id = agent.get("project_id") or ""
    result = await _org.transfer_agent(project_id, agent["id"], body.newParentId)
    if isinstance(result, dict) and result.get("success") is False:
        raise HTTPException(status_code=400, detail=result.get("message", "Failed"))
    pl = await _resolve_project_language(project_id)
    return {"agent": _agent_response(result or {}, project_language=pl)}


@router.get("/modules")
async def list_modules(projectId: str = Query(...)) -> dict:
    """列出项目模块（per-project DB modules 表）。"""
    workspace = await meta_db.get_project_workspace(projectId)
    if not workspace:
        return {"modules": []}
    try:
        conn = await project_db.ensure_project_db(workspace)
        cursor = await conn.execute(
            "SELECT id, project_id, name, path, description, created_at, "
            "updated_at FROM modules WHERE project_id = ? ORDER BY name",
            [projectId],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {"modules": [dict(r) for r in rows]}
    except Exception as e:
        log.warning("list_modules_failed", project_id=projectId, error=str(e))
        return {"modules": []}


# ── 前端 RESTful 路径参数兼容路由 ─────────────────────────────
# 前端（TS/Elixir）期望 /api/org/{projectId}/... 风格；保留现有 query 风格路由，
# 额外提供 path 参数变体。所有兼容路由直接委托给现有处理函数。
# COMPAT: 前端 api.ts 期望的 RESTful 路径


@router.get("/{project_id}/tree")
async def get_tree_path(project_id: str) -> dict:
    """组织树（path: projectId）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    validate_id(project_id, "project_id")
    return await get_tree(projectId=project_id)


@router.get("/{project_id}/agents")
async def list_agents_path(project_id: str) -> dict:
    """列出 agent（path: projectId）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    validate_id(project_id, "project_id")
    return await list_agents(projectId=project_id)


@router.post("/{project_id}/agents")
async def create_agent_path(project_id: str, body: AgentCreate) -> dict:
    """创建 agent（path: projectId 覆盖 body projectId）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    validate_id(project_id, "project_id")
    overridden = body.model_copy(update={"projectId": project_id})
    return await create_agent(overridden)
