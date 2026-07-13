"""FastAPI application entry point.

契约 15: SystemState + Application — startup/shutdown lifecycle.
契约 19: HTTP API — route registration + ApiKeyAuth middleware.
契约 12: Realtime — WebSocket route registration.

Startup sequence (对齐 Elixir Application.start/2):
1. init Meta DB (tables, indexes, WAL mode)
2. Clear zombie streaming messages (is_streaming=true from prior crashes)
3. Seed default LLM model (OPENCODE_API_KEY → DeepSeek V4 Flash Free)
4. Start game time tick loop (5s interval)
5. Recover projects from agents table (boot-time repair)
6. Start all active agents (AgentManager.start_project_agents)

Shutdown sequence:
1. Stop game time tick loop
2. Stop all agent tasks
3. Close per-project DBs
4. Close Meta DB
"""

import sys
from pathlib import Path

# Force UTF-8 for stdout/stderr on Windows — prevents GBK encoding crashes
# when logging Unicode characters (emoji, CJK names) via structlog.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass  # Best-effort; may fail on redirected pipes

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import structlog

from hiveweave.config import settings

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown.

    契约 15: SystemState + Application
    """
    from hiveweave.db.meta import init_meta_db, close_meta_db
    from hiveweave.db.project import close_all as close_project_dbs
    from hiveweave.services.system_state import SystemState
    from hiveweave.services.model import ModelService
    from hiveweave.services.game_time import GameTimeService
    from hiveweave.services.chat_message import ChatMessageService
    from hiveweave.services.approval import approval_service
    from hiveweave.agents.supervisor import agent_manager

    # ── Startup ──────────────────────────────────────────────
    log.info("app_starting", port=settings.port)

    # 1. Init Meta DB
    # R5 fix: 每个步骤独立 try/except — init_meta_db 失败不阻塞后续步骤
    try:
        await init_meta_db()
        log.info("meta_db_initialized")
    except Exception as e:
        log.error("meta_db_init_failed", error=str(e))

    # 2. Clear zombie streaming messages
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.db.project import ensure_project_db
        projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
        for p in projects:
            try:
                conn = await ensure_project_db(p["workspace_path"])
                svc = ChatMessageService(p["id"])
                await svc.clear_stuck_streaming()
            except Exception as e:
                log.warning("zombie_clear_failed", project_id=p["id"], error=str(e))
        log.info("zombie_streaming_cleared", projects=len(projects))
    except Exception as e:
        log.warning("zombie_streaming_clear_failed", error=str(e))

    # 2b. R12 fix: 清理过期工具输出临时文件（7 天保留期）
    try:
        from hiveweave.tools.executor import ToolExecutor
        projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
        cleaned = 0
        for p in projects:
            try:
                # R13 fix: p 是 aiosqlite.Row，不支持 .get()，改用 [] 索引
                # （查询显式 SELECT workspace_path，列一定存在；NULL 时返回 None）
                ws = p["workspace_path"]
                if ws:
                    ToolExecutor.cleanup_tool_outputs(ws)
                    cleaned += 1
            except Exception as e:
                log.warning("tool_output_cleanup_failed", project_id=p["id"], error=str(e))
        log.info("tool_outputs_cleaned", projects=cleaned)
    except Exception as e:
        log.warning("tool_output_cleanup_init_failed", error=str(e))

    # 2b-migration: Legacy agent migration from meta_db to per-project DB
    # has been removed. The old 'agents' table and 'agent_index' table are
    # cleaned up by _migrate_meta_schema() in meta.py (DROP TABLE IF EXISTS).
    # Agent routing is now handled by AgentRouter (in-memory) rebuilt at startup.

    # 2c. Recover stale git worktrees for executor agents
    # If a worktree directory was deleted (e.g., by sandbox cleanup or manual
    # deletion), the git branch ref remains and blocks re-creation with -b.
    # Step 1: prune stale worktree metadata. Step 2: re-create missing worktrees
    # for active executor agents and update their workspace_path in DB.
    try:
        from hiveweave.services.git_worktree import GitWorktreeService, _git
        from hiveweave.db import meta as meta_db
        from hiveweave.db import project as project_db
        import time as _wt_time
        projects = await meta_db.query(
            "SELECT id, workspace_path FROM projects WHERE 1=1"
        )
        recovered = 0
        for p in projects:
            ws = p["workspace_path"]
            if not ws or not (Path(ws) / ".git").exists():
                continue
            # Prune stale worktree metadata
            await _git(["worktree", "prune"], ws)
            # Find executor agents with missing worktrees (agents 表在 per-project DB)
            proj_conn = await project_db.get_project_db_by_project_id(p["id"])
            if proj_conn is None:
                continue
            agent_cursor = await proj_conn.execute(
                "SELECT id, name, role, short_id, workspace_path "
                "FROM agents WHERE project_id=? AND status='active' "
                "AND role NOT IN ('ceo', 'hr')",
                [p["id"]],
            )
            agents = await agent_cursor.fetchall()
            await agent_cursor.close()
            gwt = GitWorktreeService()
            for a in agents:
                short_id = a["short_id"]
                cur_ws = a["workspace_path"] or ""
                # Check if worktree directory exists
                if cur_ws and Path(cur_ws).exists() and (Path(cur_ws) / ".git").exists():
                    continue  # Worktree is fine
                # Recreate
                role = a["role"] or "developer"
                result = await gwt.create(
                    workspace_path=ws,
                    short_id=short_id,
                    task_name=role,
                )
                if result.get("success") and result.get("path"):
                    # BUG-FIX: 直接用 proj_conn 更新，不走 project_db.execute(agent_id)。
                    # 后者依赖 agent_router 内存映射，启动恢复时映射可能尚未包含
                    # 新创建的 agent，导致 "No project DB found for agent" 错误。
                    await proj_conn.execute(
                        "UPDATE agents SET workspace_path=?, updated_at=? WHERE id=?",
                        [result["path"], int(_wt_time.time() * 1000), a["id"]],
                    )
                    await proj_conn.commit()
                    recovered += 1
                    log.info("worktree_recovered",
                             agent_id=a["id"], short_id=short_id,
                             path=result["path"])
                else:
                    log.warning("worktree_recover_failed",
                                agent_id=a["id"], short_id=short_id,
                                error=result.get("message"))
        log.info("worktree_recovery_done", recovered=recovered)
    except Exception as e:
        log.warning("worktree_recovery_init_failed", error=str(e))

    # 3. Seed default model
    try:
        model_svc = ModelService()
        await model_svc.seed_default_model()
        log.info("default_model_seeded")
    except Exception as e:
        log.warning("default_model_seed_failed", error=str(e))

    # R4: 恢复/清理 pending approval 请求（重启后 _pending 丢失）
    try:
        await approval_service.cleanup_orphaned_requests()
        log.info("approval_requests_restored")
    except Exception as e:
        log.warning("approval_restore_failed", error=str(e))

    # 4. Start game time tick loop
    game_time_projects: list[str] = []
    try:
        from hiveweave.db import meta as meta_db
        projects = await meta_db.query("SELECT id FROM projects WHERE 1=1")
        for p in projects:
            try:
                gt = GameTimeService(p["id"])
                await gt.start(p["id"])
                game_time_projects.append(p["id"])
            except Exception as e:
                log.warning("game_time_start_failed", project_id=p["id"], error=str(e))
        log.info("game_time_started", projects=len(projects))
    except Exception as e:
        log.warning("game_time_init_failed", error=str(e))

    # 4b. Rebuild agent_router (in-memory agent_id → project_id routing)
    # Must run after init_meta_db and before start_project_agents,
    # so that agent_manager can resolve agent_id → project_id via agent_router.
    try:
        from hiveweave.services.agent_router import agent_router
        total = await agent_router.rebuild()
        log.info("agent_router_rebuilt", total_agents=total)
    except Exception as e:
        log.warning("agent_router_rebuild_failed", error=str(e))

    # 5. Recover + start agents for all projects
    try:
        projects = await meta_db.query("SELECT id FROM projects WHERE 1=1")
        for p in projects:
            try:
                await agent_manager.start_project_agents(p["id"])
            except Exception as e:
                log.warning("agent_start_failed", project_id=p["id"], error=str(e))
        log.info("agents_started", projects=len(projects))
    except Exception as e:
        log.warning("agent_recovery_failed", error=str(e))

    log.info("app_started")

    yield

    # ── Shutdown ─────────────────────────────────────────────
    log.info("app_stopping")

    # Stop game time tick loops
    for pid in game_time_projects:
        try:
            gt = GameTimeService(pid)
            await gt.stop(pid)
        except Exception as e:
            log.warning("game_time_stop_failed", project_id=pid, error=str(e))
    log.info("game_time_stopped")

    # Stop all agents
    try:
        all_agents = agent_manager.list_all()
        # R10: list_all() 返回 Agent 对象，stop_agent 期望 agent_id 字符串
        agent_ids = [
            a.id if hasattr(a, "id") else str(a) for a in all_agents
        ]
        for agent_id in agent_ids:
            await agent_manager.stop_agent(agent_id)
        log.info("agents_stopped", count=len(agent_ids))
    except Exception as e:
        log.warning("agent_stop_failed", error=str(e))

    # Close DBs
    await close_project_dbs()
    await close_meta_db()
    log.info("app_stopped")


app = FastAPI(
    title="HiveWeave API",
    version="0.1.0",
    description="HiveWeave — multi-agent AI workspace (Python port from Elixir/Phoenix)",
    lifespan=lifespan,
)

# ── Middleware ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 契约 19: ApiKeyAuth — 校验所有 /api/* 端点（settings.api_key 为空时全放行）
from hiveweave.api.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)

# BUG-009/012/013 fix: ensure all JSON responses carry charset=utf-8
# to prevent CJK mojibake when browsers/ proxies treat JSON as Latin-1
@app.middleware("http")
async def charset_middleware(request: Request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "application/json" in ct and "charset" not in ct:
        response.headers["content-type"] = f"{ct}; charset=utf-8"
    return response

# 请求日志中间件 — 记录每个 API 调用的耗时和状态码
import time as _time

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # 跳过高频轮询请求
    path = request.url.path
    skip_prefixes = (
        "/api/projects/", "/api/chat/questions", "/api/user-pings",
        "/api/communications", "/api/permissions/pending",
    )
    is_polling = any(path.startswith(p) for p in skip_prefixes) and request.method == "GET"

    if is_polling:
        return await call_next(request)

    start = _time.monotonic()
    try:
        response = await call_next(request)
        elapsed_ms = round((_time.monotonic() - start) * 1000)
        # 记录关键 API 调用
        if response.status_code >= 400 or path.startswith("/api/chat") or path.startswith("/api/org"):
            log.info(
                "http_request",
                method=request.method,
                path=path,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        return response
    except Exception as e:
        elapsed_ms = round((_time.monotonic() - start) * 1000)
        log.error(
            "http_request_error",
            method=request.method,
            path=path,
            error=str(e),
            elapsed_ms=elapsed_ms,
        )
        raise


# ── Route Registration ──────────────────────────────────────
from hiveweave.api.router import register_routes as _register_api
from hiveweave.realtime.channels import register_ws_routes as _register_ws
from hiveweave.realtime.phoenix_adapter import register_phoenix_route as _register_phoenix

_register_api(app)
_register_ws(app)
_register_phoenix(app)  # /socket/websocket — 前端 phoenix.js 兼容
