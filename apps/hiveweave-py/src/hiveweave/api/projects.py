"""Project CRUD endpoints (contract 19, group 2).

契约 19: Projects — 项目元数据 + 目标 + 工作空间
- GET    /api/projects              列出项目（query: status?）
- POST   /api/projects              创建项目（含 charter 三段 + CEO/HR/QA 自动创建）
- GET    /api/projects/{id}         查单个项目（汇总 agents/roster）
- PATCH  /api/projects/{id}         更新项目（workspacePath 变化时迁移 + 失效缓存）
- DELETE /api/projects/{id}         删除项目（删库文件 + meta 删行）
- GET    /api/projects/{id}/activate    激活项目
- GET    /api/projects/{id}/deactivate  取消激活
- POST   /api/projects/{id}/goals   更新 charter goals 段
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.org import OrgService
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.roster import RosterService
from hiveweave.services.git_worktree import GitWorktreeService

log = structlog.get_logger(__name__)

# BUG-020 修复：projects router 需要自己的 GameTimeService 实例，
# 用于 /{project_id}/game-time 端点。
_game_time = GameTimeService()

router = APIRouter(prefix="/api/projects", tags=["projects"])

#: meta_index key 用于记录当前激活的项目
_ACTIVE_KEY = "active_project_id"


class ProjectCreate(BaseModel):
    """创建项目请求体。"""

    name: str
    workspacePath: str
    description: str | None = None
    charterVision: str | None = None
    charterGoals: str | None = None
    charterConstraints: str | None = None
    userInvolvement: str | None = None
    orgPattern: str | None = None
    operatorName: str | None = None
    language: str | None = None


class ProjectUpdate(BaseModel):
    """更新项目请求体（所有字段可选）。"""

    name: str | None = None
    workspacePath: str | None = None
    description: str | None = None
    operatorName: str | None = None


class CharterGoalsUpdate(BaseModel):
    """charter goals 段更新请求体。"""

    goals: dict


def _build_charter_dict(body: ProjectCreate) -> dict:
    """组装 charter dict（vision/goals/constraints 三段）。"""
    raw_goals = body.charterGoals
    if isinstance(raw_goals, str) and raw_goals.strip():
        try:
            goals = json.loads(raw_goals)
        except (json.JSONDecodeError, TypeError):
            goals = {
                "objective": "",
                "focus": raw_goals,
                "keyResults": [],
                "userInvolvement": "宏观决策+技术选型",
            }
    elif isinstance(raw_goals, dict):
        goals = raw_goals
    else:
        goals = {
            "objective": "",
            "focus": "",
            "keyResults": [],
            "userInvolvement": "宏观决策+技术选型",
        }
    return {
        "vision": body.charterVision or f"Build and operate the {body.name} project.",
        "goals": goals,
        "constraints": body.charterConstraints or "",
        "userInvolvement": body.userInvolvement or "medium",
        "orgPattern": body.orgPattern or "solo",
        "operatorName": body.operatorName or "operator",
    }


def _validate_workspace_path(raw: str) -> Path:
    """校验 workspace_path 安全性（R2 fix）。

    检查：
    1. 非空字符串
    2. resolve() 后是绝对路径
    3. 原始路径不含 ``..`` 路径穿越段
    4. resolve() 后的路径不含 ``..`` 段（符号链接解析后）

    Args:
        raw: 用户传入的 workspace_path 字符串

    Returns:
        解析后的绝对 Path 对象

    Raises:
        HTTPException(400): 路径非法或含穿越段
    """
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="workspace_path is required")
    # 原始路径段级检查 — 拒绝任何 ".." 段
    parts = Path(raw).parts
    if ".." in parts:
        raise HTTPException(
            status_code=400,
            detail="workspace_path must not contain '..' path traversal",
        )
    resolved = Path(raw).resolve()
    # resolve 后必须为绝对路径
    if not resolved.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="workspace_path must resolve to an absolute path",
        )
    # 二次校验：resolve 后的路径段也不含 ".."
    if ".." in resolved.parts:
        raise HTTPException(
            status_code=400,
            detail="workspace_path resolves to a path with '..' segments",
        )
    # BUG-033 fix: reject source-code/documentation directories to prevent
    # per-project DBs from polluting the repo. .hiveweave/data.db should
    # only live in real project workspaces, never in src/docs/tests/apps.
    _SOURCE_SEGMENTS = {"src", "apps", "tests", "test", "docs", "doc", "node_modules",
                        "__pycache__", ".git", ".claude", "dist", "build", "out"}
    offending = [p for p in resolved.parts if p.lower() in _SOURCE_SEGMENTS]
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"workspace_path appears to be a source/documentation directory "
                    f"(contains: {', '.join(offending)}). "
                    f"Please choose a real project workspace instead.",
        )
    return resolved


def _project_response(row: dict, active_id: str | None = None) -> dict:
    """把 DB 行转为响应 dict（同时含 snake_case 与 camelCase）。"""
    charter_raw = row.get("charter_json")
    charter: dict | None = None
    if charter_raw:
        try:
            charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
        except (json.JSONDecodeError, TypeError):
            charter = None
    is_active = (row.get("id") == active_id) if active_id is not None else False
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "workspace_path": row.get("workspace_path"),
        "workspacePath": row.get("workspace_path"),
        "org_paradigm": row.get("org_paradigm"),
        "orgParadigm": row.get("org_paradigm"),
        "language": row.get("language"),
        "charter": charter,
        "is_active": is_active,
        "isActive": is_active,
        "created_at": row.get("created_at"),
        "createdAt": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updatedAt": row.get("updated_at"),
    }


async def _get_active_project_id() -> str | None:
    """从 meta_index 读取当前激活的项目 id。"""
    row = await meta_db.query_one(
        "SELECT value FROM meta_index WHERE key = ? LIMIT 1", [_ACTIVE_KEY]
    )
    return row["value"] if row else None


async def _set_active_project_id(project_id: str | None) -> None:
    if project_id is None:
        await meta_db.execute(
            "DELETE FROM meta_index WHERE key = ?", [_ACTIVE_KEY]
        )
    else:
        await meta_db.execute(
            "INSERT OR REPLACE INTO meta_index (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            [_ACTIVE_KEY, project_id, int(time.time() * 1000)],
        )


async def _seed_default_agents(project_id: str) -> list[str]:
    """新项目自动创建 CEO / HR / QA 三角色（幂等）。

    Returns:
        创建的 agent ID 列表（第一个是 CEO）。
    """
    log.info("seed_start", project_id=project_id)
    org = OrgService()
    existing = await org.list_agents(project_id)
    log.info("seed_existing_agents", project_id=project_id, count=len(existing))

    # 获取默认模型 ID（优先选择 step 系列，其次非 free 模型）
    default_model_id = None
    try:
        from hiveweave.services.model import ModelService
        ms = ModelService()
        active_models = await ms.list_active()
        if active_models:
            # 优先选择 step 系列模型
            step_models = [m for m in active_models if "step" in (m.get("model_id") or "").lower()]
            non_free = [m for m in active_models if "free" not in (m.get("model_id") or "").lower()]
            if step_models:
                chosen = step_models[0]
            elif non_free:
                chosen = non_free[0]
            else:
                chosen = active_models[0]
            default_model_id = chosen.get("model_id") or chosen.get("id")
            log.info("seed_default_model", default_model_id=default_model_id, total_models=len(active_models))
        else:
            log.warning("seed_no_active_models")
    except Exception as e:
        log.warning("seed_default_model_failed", error=str(e))

    # 如果已有 agent，更新它们的 model_id（可能来自旧项目残留）
    if any(a.get("role") == "ceo" for a in existing):
        # 更新现有 agent 的 model_id（如果为空或需要修正）
        for a in existing:
            current_model = a.get("model_id") or a.get("config", {}).get("model_id")
            if not current_model and default_model_id:
                try:
                    await org.update_agent(a["id"], {"model_id": default_model_id})
                    print(f"[SEED] updated {a.get('name')} model_id={default_model_id}", flush=True)
                except Exception as e:
                    print(f"[SEED] update {a.get('name')} model_id FAILED: {e}", flush=True)
            # 返回 CEO ID
            if a.get("role") == "ceo":
                return [a["id"]]
        return [existing[0]["id"]] if existing else []

    created_ids: list[str] = []
    try:
        ceo = await org.create_agent(
            {
                "project_id": project_id,
                "name": "CEO",
                "role": "ceo",
                "goal": "Coordinate the project, manage subordinates, deliver the charter.",
                "backstory": "The chief executive officer of the project.",
                "permission_type": "coordinator",
                "status": "active",
                "model_id": default_model_id,
            }
        )
        ceo_id = ceo["id"]
        created_ids.append(ceo_id)
        log.info("seed_ceo_created", ceo_id=ceo_id, model_id=default_model_id, returned_model_id=ceo.get("model_id"))
        await org.create_agent(
            {
                "project_id": project_id,
                "name": "HR",
                "role": "hr",
                "goal": "Manage personnel: hire, transfer, dismiss agents per CEO requests.",
                "backstory": "The human resources lead.",
                "permission_type": "coordinator",
                "status": "active",
                "parent_id": ceo_id,
                "model_id": default_model_id,
            }
        )
        await org.create_agent(
            {
                "project_id": project_id,
                "name": "QA",
                "role": "qa",
                "goal": "Guard quality gates; review and test work before merge.",
                "backstory": "The quality assurance lead.",
                "permission_type": "coordinator",
                "status": "active",
                "parent_id": ceo_id,
                "model_id": default_model_id,
            }
        )
    except Exception as e:
        log.warning("seed_default_agents_failed", project_id=project_id, error=str(e))
    return created_ids


@router.get("")
async def list_projects(status: str | None = Query(default=None)) -> dict:
    """列出项目（支持 status 过滤: active/inactive）。"""
    rows = await meta_db.query(
        "SELECT * FROM projects ORDER BY created_at DESC"
    )
    active_id = await _get_active_project_id()
    projects = [_project_response(dict(r), active_id) for r in rows]
    if status == "active":
        projects = [p for p in projects if p["is_active"]]
    elif status == "inactive":
        projects = [p for p in projects if not p["is_active"]]
    return {"projects": projects}


@router.post("")
async def create_project(body: ProjectCreate) -> dict:
    """创建项目。

    契约 19 特别流程 1: 三段 charter 校验；创建后:
    - 确保工作空间目录存在（含 .hiveweave/）
    - 初始化 per-project DB（schema）
    - 自动创建 CEO / HR / QA 三个角色
    """
    workspace = body.workspacePath
    # R2 fix: 校验 workspace_path 安全性（绝对路径 + 无 .. 穿越段）
    ws = _validate_workspace_path(workspace)
    # 确保工作空间存在
    try:
        ws.mkdir(parents=True, exist_ok=True)
        (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.error("ensure_workspace_failed", workspace=workspace, error=str(e))
        raise HTTPException(status_code=400, detail=f"Invalid workspace path: {e}")

    # 初始化 per-project DB（建表）
    try:
        await project_db.ensure_project_db(workspace)
    except Exception as e:
        log.error("init_project_db_failed", workspace=workspace, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to initialize project DB")

    # BUG-034: 初始化 git 仓库，确保后续 agent 可以通过
    # GitWorktreeService 创建独立的工作区（worktree）进行隔离开发。
    # 之前此步骤缺失，导致 .hiveweave/worktrees/ 目录从未创建。
    try:
        gwt = GitWorktreeService()
        result = await gwt.ensure_git_repo(str(ws))
        if result.get("initialized"):
            log.info("project_git_initialized", workspace=str(ws))
    except Exception as e:
        log.warning("project_git_init_failed", workspace=str(ws), error=str(e))
        # 非致命 — 项目仍可正常工作，只是 worktree 功能不可用

    charter = _build_charter_dict(body)
    project_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)

    try:
        await meta_db.execute(
            "INSERT INTO projects (id, name, description, workspace_path, "
            "org_paradigm, charter_json, language, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                project_id,
                body.name,
                body.description or "",
                str(ws),
                charter.get("orgPattern", "solo"),
                json.dumps(charter, ensure_ascii=False),
                body.language or "en",
                now_ms,
                now_ms,
            ],
        )
    except Exception as e:
        log.error("create_project_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create project")

    # 自动创建默认 agent
    import sys
    sys.stderr.write(f"[CREATE] calling _seed_default_agents for project={project_id}\n")
    sys.stderr.flush()
    agent_ids = await _seed_default_agents(project_id)
    sys.stderr.write(f"[CREATE] _seed_default_agents returned {len(agent_ids)} agents\n")
    sys.stderr.flush()

    # 启动 agent + game time（C3/C4 fix: 新项目创建后立即启动运行时资源）
    try:
        from hiveweave.agents.supervisor import agent_manager
        await agent_manager.start_project_agents(project_id)
    except Exception as e:
        log.warning("start_agents_after_create_failed", project_id=project_id, error=str(e))
    try:
        gt = GameTimeService(project_id)
        await gt.start(project_id)
    except Exception as e:
        log.warning("start_game_time_after_create_failed", project_id=project_id, error=str(e))

    row = await meta_db.query_one("SELECT * FROM projects WHERE id = ?", [project_id])
    log.info("project_created", project_id=project_id, name=body.name, workspace=str(ws))
    return {
        "project": _project_response(dict(row)) if row else {"id": project_id},
        "mainAgentId": agent_ids[0] if agent_ids else None,
    }


@router.get("/{project_id}")
async def get_project(project_id: str) -> dict:
    """查单个项目（汇总 agents/roster）。"""
    row = await meta_db.query_one(
        "SELECT * FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    active_id = await _get_active_project_id()
    resp = _project_response(dict(row), active_id)

    # 汇总 agents + roster
    try:
        org = OrgService()
        resp["agents"] = await org.list_agents(project_id)
        resp["agentCount"] = len(resp["agents"])
    except Exception as e:
        log.warning("list_agents_for_project_failed", error=str(e))
        resp["agents"] = []
        resp["agentCount"] = 0
    try:
        roster_svc = RosterService()
        resp["roster"] = await roster_svc.list_by_project(project_id)
    except Exception as e:
        log.warning("list_roster_for_project_failed", error=str(e))
        resp["roster"] = []
    return {"project": resp}


async def _do_update_project(project_id: str, body: ProjectUpdate) -> dict:
    row = await meta_db.query_one(
        "SELECT * FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    existing = dict(row)
    old_workspace = existing.get("workspace_path") or ""

    sets: list[str] = []
    vals: list = []
    if body.name is not None:
        sets.append("name = ?")
        vals.append(body.name)
    if body.description is not None:
        sets.append("description = ?")
        vals.append(body.description)
    if body.workspacePath is not None and body.workspacePath != old_workspace:
        # 迁移工作空间
        # R2 fix: 校验新 workspace_path 安全性
        new_ws_dir = _validate_workspace_path(body.workspacePath)
        new_ws = str(new_ws_dir)
        try:
            new_ws_dir.mkdir(parents=True, exist_ok=True)
            (new_ws_dir / ".hiveweave").mkdir(parents=True, exist_ok=True)
            # 移动旧 .hiveweave 内容（若存在）
            old_hw = Path(old_workspace) / ".hiveweave"
            new_hw = new_ws_dir / ".hiveweave"
            if old_hw.exists():
                for item in old_hw.iterdir():
                    target = new_hw / item.name
                    if not target.exists():
                        item.rename(target)
        except Exception as e:
            log.error(
                "migrate_workspace_failed",
                old=old_workspace,
                new=new_ws,
                error=str(e),
            )
            raise HTTPException(
                status_code=400, detail=f"Workspace migration failed: {e}"
            )
        # 失效 project db 缓存
        try:
            await project_db.evict_project_db(old_workspace)
            await project_db.evict_project_db(new_ws)
        except Exception:
            pass
        sets.append("workspace_path = ?")
        vals.append(new_ws)
    if body.operatorName is not None:
        charter_raw = existing.get("charter_json")
        charter: dict = {}
        if charter_raw:
            try:
                charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
            except (json.JSONDecodeError, TypeError):
                charter = {}
        charter["operatorName"] = body.operatorName
        sets.append("charter_json = ?")
        vals.append(json.dumps(charter, ensure_ascii=False))

    if sets:
        sets.append("updated_at = ?")
        vals.append(int(time.time() * 1000))
        vals.append(project_id)
        await meta_db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals
        )

    updated = await meta_db.query_one(
        "SELECT * FROM projects WHERE id = ?", [project_id]
    )
    active_id = await _get_active_project_id()
    return {"project": _project_response(dict(updated)) if updated else {}}


@router.patch("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate) -> dict:
    """更新项目（PATCH）。"""
    return await _do_update_project(project_id, body)


@router.put("/{project_id}")
async def put_project(project_id: str, body: ProjectUpdate) -> dict:
    """更新项目（PUT，同 PATCH）。"""
    return await _do_update_project(project_id, body)


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict:
    """删除项目（停 agent + 停 game time + 删库文件 + meta 删行 + 清内存）。"""
    row = await meta_db.query_one(
        "SELECT workspace_path FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace = row["workspace_path"] or ""

    # C1 fix: 先停止该项目所有 agent（取消 LLM task + 清理内存对象）
    try:
        from hiveweave.agents.supervisor import agent_manager
        await agent_manager.stop_project_agents(project_id)
    except Exception as e:
        log.warning("stop_agents_before_delete_failed", project_id=project_id, error=str(e))

    # C2 fix: 停止 game time tick loop
    try:
        gt = GameTimeService(project_id)
        await gt.stop(project_id)
    except Exception as e:
        log.warning("stop_game_time_before_delete_failed", project_id=project_id, error=str(e))

    # M2 fix: 清理 game_time 内存状态
    try:
        from hiveweave.services.game_time import _states, _alarm_project
        _states.pop(project_id, None)
        stale_alarms = [aid for aid, pid in _alarm_project.items() if pid == project_id]
        for aid in stale_alarms:
            _alarm_project.pop(aid, None)
    except Exception:
        pass

    # M3 fix: 清理 approval_service 中该项目的 pending 请求
    try:
        from hiveweave.services.approval import approval_service
        approval_service.cleanup_project(project_id)
    except Exception:
        pass

    # M4+M5 fix: 清理 conversation_store 缓存 + status_event_bus processing 状态
    # 注意: 只清内存缓存，不调 conversation_store.clear()（它会重新打开 DB 连接）
    try:
        from hiveweave.conversation.store import conversation_store
        from hiveweave.realtime.event_bus import status_event_bus
        agents = await meta_db.query(
            "SELECT id FROM agents WHERE project_id = ?", [project_id]
        )
        for a in agents:
            aid = a["id"]
            try:
                key = (project_id, aid)
                conversation_store._cache.pop(key, None)
                conversation_store._prefix_cache.pop(key, None)
            except Exception:
                pass
            status_event_bus.set_processing(aid, False)
    except Exception:
        pass

    # 等待 agent task 完全取消 + DB 连接释放（Windows 文件锁延迟）
    await asyncio.sleep(1.0)

    # 强制关闭该项目的所有 DB 连接（必须在所有内存清理之后，否则会重新打开）
    if workspace:
        try:
            await project_db.evict_project_db(workspace)
        except Exception:
            pass
        try:
            agents = await meta_db.query(
                "SELECT id FROM agents WHERE project_id = ?", [project_id]
            )
            for a in agents:
                try:
                    await project_db.evict_project_db_for_agent(a["id"])
                except Exception:
                    pass
        except Exception:
            pass

    if workspace:
        # DB 连接已在前面关闭，直接删除 .hiveweave 目录
        # 等待 Windows 文件锁完全释放
        await asyncio.sleep(0.3)
        # 删除整个 .hiveweave 目录（DB 文件 + worktrees + 临时文件）
        # 带重试机制处理 Windows 文件锁
        hw_dir = Path(workspace) / ".hiveweave"
        if hw_dir.exists():
            import shutil
            import stat
            import time as _time

            def _on_error(func, path, exc_info):
                """强制删除：去除只读属性后重试。"""
                try:
                    os.chmod(path, stat.S_IWRITE)
                except Exception:
                    pass
                try:
                    func(path)
                except Exception:
                    pass

            for attempt in range(3):
                try:
                    shutil.rmtree(hw_dir, onerror=_on_error)
                    break
                except Exception as e:
                    if attempt < 2:
                        _time.sleep(0.5)
                    else:
                        log.warning("delete_hiveweave_dir_failed",
                                    workspace=workspace, error=str(e))

    # 若是当前激活项目，取消激活
    active_id = await _get_active_project_id()
    if active_id == project_id:
        await _set_active_project_id(None)

    try:
        await meta_db.execute("DELETE FROM projects WHERE id = ?", [project_id])
        # 同时删除该项目下的 agents 记录
        await meta_db.execute(
            "DELETE FROM agents WHERE project_id = ?", [project_id]
        )
    except Exception as e:
        log.error("delete_project_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete project")

    log.info("project_deleted", project_id=project_id)
    return {"ok": True}


@router.get("/{project_id}/activate")
async def activate_project(project_id: str) -> dict:
    """激活项目。"""
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await _set_active_project_id(project_id)
    return {"ok": True, "projectId": project_id}


@router.get("/{project_id}/deactivate")
async def deactivate_project(project_id: str) -> dict:
    """取消激活项目。"""
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    active_id = await _get_active_project_id()
    if active_id == project_id:
        await _set_active_project_id(None)
    return {"ok": True, "projectId": project_id}


@router.get("/{project_id}/game-time")
async def get_project_game_time(project_id: str) -> dict:
    """查项目游戏时间。

    BUG-020 修复：前端请求的是 /api/projects/{id}/game-time，
    但 endpoint 原本只在 /api/game-time/{id}。在此补一个 projects 前缀的路由。
    """
    try:
        result = await _game_time.get_current_time(project_id)
    except Exception as e:
        # 优雅降级：workspace 不存在或 DB 未初始化时返回 0 而非 500，
        # 避免前端 ProjectTimeBadge 崩溃（BUG-020）
        log.warning("get_project_game_time_failed", project_id=project_id, error=str(e))
        return {
            "projectId": project_id,
            "gameSeconds": 0,
            "formatted": "Day 0 00:00",
            "realStartedAt": None,
            "realSecondsPerGameDay": 3600,
        }
    return {
        "projectId": project_id,
        "gameSeconds": result.get("game_seconds", 0),
        "formatted": result.get("formatted", ""),
        "realStartedAt": result.get("real_started_at"),
        "realSecondsPerGameDay": result.get("real_seconds_per_game_day", 3600),
    }


@router.get("/{project_id}/goals")
async def get_project_goals(project_id: str) -> dict:
    """读取 charter goals 段。

    BUG-016 修复：之前只有 POST 没有 GET，前端 GET /goals 返回 405。
    """
    row = await meta_db.query_one(
        "SELECT charter_json FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    charter_raw = row["charter_json"]
    charter: dict = {}
    if charter_raw:
        try:
            charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
        except (json.JSONDecodeError, TypeError):
            charter = {}
    goals = charter.get("goals") if charter else None
    # 兼容旧数据：goals 可能是空字符串或缺失，转成对象
    if not goals:
        goals = {
            "objective": "",
            "focus": "",
            "keyResults": [],
            "userInvolvement": "宏观决策+技术选型",
        }
    elif isinstance(goals, str):
        try:
            goals = json.loads(goals) or {
                "objective": "",
                "focus": "",
                "keyResults": [],
                "userInvolvement": "宏观决策+技术选型",
            }
        except (json.JSONDecodeError, TypeError):
            goals = {
                "objective": "",
                "focus": goals,
                "keyResults": [],
                "userInvolvement": "宏观决策+技术选型",
            }
    return {"goals": goals, "projectId": project_id}


@router.put("/{project_id}/goals")
async def update_project_goals(project_id: str, body: CharterGoalsUpdate) -> dict:
    """更新 charter goals 段（合并写入 charter_json）。

    BUG-016 修复：POST → PUT，与前端 updateProjectGoals 的 PUT 对齐。
    保留 POST 别名以防旧客户端。
    """
    row = await meta_db.query_one(
        "SELECT charter_json FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    charter_raw = row["charter_json"]
    charter: dict = {}
    if charter_raw:
        try:
            charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
        except (json.JSONDecodeError, TypeError):
            charter = {}
    charter["goals"] = body.goals
    try:
        await meta_db.execute(
            "UPDATE projects SET charter_json = ?, updated_at = ? WHERE id = ?",
            [json.dumps(charter, ensure_ascii=False), int(time.time() * 1000), project_id],
        )
    except Exception as e:
        log.error("update_goals_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update goals")
    return {"ok": True}


# BUG-016 兼容：保留 POST 别名
@router.post("/{project_id}/goals")
async def update_project_goals_post(project_id: str, body: CharterGoalsUpdate) -> dict:
    return await update_project_goals(project_id, body)
