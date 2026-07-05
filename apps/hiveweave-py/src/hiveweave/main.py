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

from contextlib import asynccontextmanager

from fastapi import FastAPI
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
                ws = p.get("workspace_path")
                if ws:
                    ToolExecutor.cleanup_tool_outputs(ws)
                    cleaned += 1
            except Exception as e:
                log.warning("tool_output_cleanup_failed", project_id=p["id"], error=str(e))
        log.info("tool_outputs_cleaned", projects=cleaned)
    except Exception as e:
        log.warning("tool_output_cleanup_init_failed", error=str(e))

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
    game_time_tasks: list = []
    try:
        from hiveweave.db import meta as meta_db
        projects = await meta_db.query("SELECT id FROM projects WHERE 1=1")
        for p in projects:
            try:
                gt = GameTimeService(p["id"])
                task = await gt.start_tick_loop()
                if task:
                    game_time_tasks.append(task)
            except Exception as e:
                log.warning("game_time_start_failed", project_id=p["id"], error=str(e))
        log.info("game_time_started", projects=len(projects))
    except Exception as e:
        log.warning("game_time_init_failed", error=str(e))

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
    for task in game_time_tasks:
        task.cancel()
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


# ── Route Registration ──────────────────────────────────────
from hiveweave.api.router import register_routes as _register_api
from hiveweave.realtime.channels import register_ws_routes as _register_ws

_register_api(app)
_register_ws(app)
