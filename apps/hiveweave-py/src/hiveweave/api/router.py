"""Main router registration (contract 19).

契约 19: HTTP API — 聚合 16 分组所有子路由。
``register_routes(app)`` 把所有 APIRouter 挂到 FastAPI app 上，并提供:
- GET / — 根端点（HTML 状态页）
- 内联 handoffs 路由（list + detail）
- 内联 skills 路由（available + bound）
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import structlog

from hiveweave.api.health import router as health_router
from hiveweave.api.settings import router as settings_router
from hiveweave.api.mcp import router as mcp_router
from hiveweave.api.models import router as models_router
from hiveweave.api.provider_presets import router as provider_presets_router
from hiveweave.api.templates import router as templates_router
from hiveweave.api.projects import router as projects_router
from hiveweave.api.org import router as org_router
from hiveweave.api.chat import router as chat_router
from hiveweave.api.permissions import router as permissions_router
from hiveweave.api.communications import router as communications_router
from hiveweave.api.llm_ops import router as llm_ops_router
from hiveweave.api.logs import router as logs_router
from hiveweave.api.alarms import router as alarms_router
from hiveweave.api.filesystem import router as filesystem_router
from hiveweave.api.filesystem import fs_router as fs_browse_router
from hiveweave.api.debug import router as debug_router
from hiveweave.api.tasks import router as tasks_router
from hiveweave.api.timeline import router as timeline_router
from hiveweave.api.meetings import router as meetings_router
from hiveweave.api.system import router as system_router
from hiveweave.api.token_usage import router as token_usage_router
from hiveweave.api.ball import router as ball_router  # /ball 静态 + /api/ball/*

log = structlog.get_logger(__name__)

#: 所有子路由（按分组顺序）
_SUB_ROUTERS = [
    health_router,
    settings_router,
    mcp_router,  # /api/mcp — MCP 服务器配置 CRUD + agent 绑定
    models_router,
    provider_presets_router,  # /api/provider-presets — 知名服务商预设（只填 Key）
    templates_router,
    projects_router,
    org_router,
    chat_router,
    permissions_router,
    communications_router,
    logs_router,
    llm_ops_router,
    alarms_router,
    filesystem_router,
    fs_browse_router,  # /api/fs/browse — 全局文件系统浏览（新建项目用）
    debug_router,
    tasks_router,  # /api/projects/{project_id}/tasks — Task Ledger
    timeline_router,  # /api/projects/{project_id}/timeline — 团队活动可视化
    meetings_router,  # /api/projects/{project_id}/meetings — 团队开会状态查询
    system_router,  # /api/system/restart-backend | restart-frontend
    token_usage_router,  # /api/projects/{project_id}/token-usage — LLM token 计量
    ball_router,  # /ball 悬浮球页面静态托管 + /api/ball/*（spec §10 P0）
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


class WebDistStaticFiles(StaticFiles):
    """带缓存纪律的 web dist 静态托管（EXE 白屏根因修复，09-08）。

    实测根因链：StaticFiles 只发 ETag/Last-Modified、无 Cache-Control →
    WebView2/浏览器启发式缓存跨构建直出旧 index.html → 页面按旧 hash 懒
    加载 chunk → 服务器已换代 404 → React.lazy 拒绝卸根白屏。因此：

    - ``*.html``（含 index.html）→ ``no-cache``：每次协商复验，构建换代
      立即可见；
    - ``/assets/*`` → 内容 hash 寻址、不可变 → ``immutable`` 一年长缓存。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):  # type: ignore[override]
        response = super().file_response(
            full_path, stat_result, scope, status_code
        )
        path = str(full_path).replace("\\", "/").lower()
        if path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        elif "/assets/" in path:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response


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


async def _root() -> Response:
    """根端点：web dist 已构建/已挂载时服务主前端 index.html（打包税 #2，
    UI 统一从 :4000 加载）；未构建时回落 API 状态页。根路由在导入期注册、
    优先于 lifespan 里 Mount("/") 的静态挂载，故 "/" 必须在这里分流。"""
    try:
        from fastapi.responses import FileResponse

        from hiveweave.config import resolve_web_dist

        web_dist = resolve_web_dist()
        # is_file 先验：FileResponse 的 stat 懒到响应期，dist 运行中被换
        # （robocopy /MIR 更新中）会变 500，这里必须当场回落
        if web_dist is not None and (web_dist / "index.html").is_file():
            # no-cache：index.html 每次协商复验（EXE 白屏根因修复 09-08，
            # 同 WebDistStaticFiles 口径）；避免启发式缓存直出跨构建旧页
            return FileResponse(
                web_dist / "index.html",
                headers={"Cache-Control": "no-cache"},
            )
    except Exception:
        pass  # 解析/读文件失败 → 回落 API 状态页
    return HTMLResponse(
        _root_html(), headers={"Cache-Control": "no-cache"}
    )


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
