"""Main router registration (contract 19).

契约 19: HTTP API — 聚合 16 分组所有子路由。
``register_routes(app)`` 把所有 APIRouter 挂到 FastAPI app 上，并提供:
- GET / — 根端点（HTML 状态页）
- 内联 handoffs 路由（list + detail）
- 内联 skills 路由（available + bound）
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

import structlog

from hiveweave.api.health import router as health_router
from hiveweave.api.settings import router as settings_router
from hiveweave.api.models import router as models_router
from hiveweave.api.templates import router as templates_router
from hiveweave.api.projects import router as projects_router
from hiveweave.api.org import router as org_router
from hiveweave.api.chat import router as chat_router
from hiveweave.api.permissions import router as permissions_router
from hiveweave.api.communications import router as communications_router
from hiveweave.api.logs import router as logs_router
from hiveweave.api.alarms import router as alarms_router
from hiveweave.api.filesystem import router as filesystem_router
from hiveweave.api.filesystem import fs_router as fs_browse_router
from hiveweave.api.debug import router as debug_router
from hiveweave.api.tasks import router as tasks_router
from hiveweave.api.system import router as system_router

log = structlog.get_logger(__name__)

#: 所有子路由（按分组顺序）
_SUB_ROUTERS = [
    health_router,
    settings_router,
    models_router,
    templates_router,
    projects_router,
    org_router,
    chat_router,
    permissions_router,
    communications_router,
    logs_router,
    alarms_router,
    filesystem_router,
    fs_browse_router,  # /api/fs/browse — 全局文件系统浏览（新建项目用）
    debug_router,
    tasks_router,  # /api/projects/{project_id}/tasks — Task Ledger
    system_router,  # /api/system/restart-backend | restart-frontend
]


def _root_html() -> str:
    """根端点 HTML 状态页。"""
    return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>HiveWeave API</title></head>
<body style="font-family:system-ui;padding:2rem;max-width:48rem">
<h1>HiveWeave API</h1>
<p>Multi-agent orchestration server. See <code>/api/health</code> for status.</p>
<ul>
  <li><code>GET /api/health</code> — health check</li>
  <li><code>GET /api/version</code> — version info</li>
  <li><code>GET /api/projects</code> — list projects</li>
  <li><code>GET /api/org</code> — organization tree</li>
  <li><code>POST /api/chat</code> — send chat message</li>
</ul>
</body>
</html>"""


def register_routes(app: FastAPI) -> None:
    """把所有子路由注册到 FastAPI app。

    用法::

        from hiveweave.api.router import register_routes
        register_routes(app)
    """
    # 根端点
    app.add_api_route("/", _root, methods=["GET"], include_in_schema=False)

    # 内联 handoffs + skills 路由
    _register_handoffs_routes(app)
    _register_skills_routes(app)

    # 所有子路由
    for router in _SUB_ROUTERS:
        app.include_router(router)

    log.info("api_routes_registered", routers=len(_SUB_ROUTERS))


async def _root() -> HTMLResponse:
    return HTMLResponse(_root_html())


# ── Handoffs 路由（内联）──────────────────────────────────────


def _register_handoffs_routes(app: FastAPI) -> None:
    """注册 handoffs 路由（list + detail）。"""
    from hiveweave.db import meta as meta_db
    from hiveweave.db.project import ensure_project_db

    @app.get("/api/handoffs", tags=["handoffs"])
    async def list_handoffs(
        projectId: str = Query(...),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, le=500),
    ) -> dict:
        """列出项目交接记录（per-project handoffs 表）。"""
        workspace = await meta_db.get_project_workspace(projectId)
        if not workspace:
            return {"handoffs": []}
        try:
            conn = await ensure_project_db(workspace)
            sql = (
                "SELECT id, from_agent_id, to_agent_id, module_id, summary, "
                "status, expect_report, reported_up, context_delivered, "
                "created_at, updated_at FROM handoffs"
            )
            params: list = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return {"handoffs": [dict(r) for r in rows]}
        except Exception as e:
            log.warning("list_handoffs_failed", error=str(e))
            return {"handoffs": []}

    @app.get("/api/handoffs/{handoff_id}", tags=["handoffs"])
    async def get_handoff(handoff_id: str, projectId: str = Query(...)) -> dict:
        """查单条交接记录。"""
        workspace = await meta_db.get_project_workspace(projectId)
        if not workspace:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            conn = await ensure_project_db(workspace)
            cursor = await conn.execute(
                "SELECT id, from_agent_id, to_agent_id, module_id, summary, "
                "status, expect_report, reported_up, context_delivered, "
                "created_at, updated_at FROM handoffs WHERE id = ?",
                [handoff_id],
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise HTTPException(status_code=404, detail="Handoff not found")
            return {"handoff": dict(row)}
        except HTTPException:
            raise
        except Exception as e:
            log.warning("get_handoff_failed", error=str(e))
            raise HTTPException(status_code=500, detail="Failed to get handoff")


# ── Skills 路由（内联）────────────────────────────────────────


def _register_skills_routes(app: FastAPI) -> None:
    """注册 skills 路由（available + bound）。"""
    from hiveweave.services.skill_registry import SkillRegistryService

    _skills = SkillRegistryService()

    @app.get("/api/skills/available", tags=["skills"])
    async def list_available_skills(
        search: str | None = Query(default=None),
    ) -> dict:
        """列出所有可用技能（外部 + 内置 + ClawHub best-effort）。"""
        text = await _skills.list_available_skills(search)
        return {"skills": text, "search": search}

    @app.get("/api/skills/agents/{agent_id}/bound", tags=["skills"])
    async def get_bound_skills(agent_id: str) -> dict:
        """查 agent 已绑定的技能 slug 列表。"""
        slugs = await _skills.get_bound_skills(agent_id)
        return {"agentId": agent_id, "skills": slugs, "count": len(slugs)}
