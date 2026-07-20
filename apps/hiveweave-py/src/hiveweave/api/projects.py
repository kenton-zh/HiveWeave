"""Project CRUD endpoints (contract 19, group 2).

契约 19: Projects — 项目元数据 + 目标 + 工作空间
- GET    /api/projects              列出项目（query: status?）
- POST   /api/projects              创建项目（含 charter 三段 + CEO 归零 + HR 天线）
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
import re
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


def _expand_windows_env_vars(path: str) -> str:
    """Expand only %VAR% patterns (Windows cmd.exe syntax).

    Unlike os.path.expandvars() which also expands $var/${var} on Python 3.9+,
    this ONLY handles %VAR% — safe for Windows paths where '$' is a valid
    filename character. Unknown variables are left unchanged.
    """
    def _replacer(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))  # leave %VAR% unchanged if not found
    return re.sub(r'%(\w+)%', _replacer, path)


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
    # Expand ONLY %VAR% patterns (Windows cmd.exe style), NOT $var (sh style).
    # os.path.expandvars() on Python 3.9+ also expands $var/${var}, turning
    # legitimate paths like D:\Project\$null into unresolved garbage.
    expanded = _expand_windows_env_vars(raw.strip())
    # 原始路径段级检查 — 拒绝任何 ".." 段
    parts = Path(expanded).parts
    if ".." in parts:
        raise HTTPException(
            status_code=400,
            detail="workspace_path must not contain '..' path traversal",
        )
    resolved = Path(expanded).resolve()
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
    # BUG-033 fix: reject when the workspace root directory itself is a known
    # tool/artifact directory that agents should never use as a project root.
    # Only block names that are ALWAYS tool-generated (never legitimate project
    # roots): .git, node_modules, __pycache__, .claude.
    # src/apps/docs/dist/build/out can all be legitimate project root names.
    _SOURCE_SEGMENTS = {"node_modules", "__pycache__", ".git", ".claude"}
    if resolved.name.lower() in _SOURCE_SEGMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"workspace_path directory '{resolved.name}' is a reserved "
                    f"source-code/build directory name. "
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
    is_started = bool(row.get("is_started") or 0)
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
        "is_started": is_started,
        "isStarted": is_started,
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


async def _fetch_project_meta(project_id: str) -> dict | None:
    """从 per-project DB 的 project_meta 表读取项目元数据。

    Meta DB slimming 后, description / org_paradigm / charter_json /
    goals_json / language 等字段存储在 per-project DB 的 project_meta 表中。
    """
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
    except project_db.ProjectDbError:
        return None
    cursor = await conn.execute(
        "SELECT * FROM project_meta WHERE project_id = ?", [project_id]
    )
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row else None


async def _seed_default_agents(project_id: str) -> list[str]:
    """新项目自动创建 CEO + HR 两个初始角色（幂等）。

    CEO 归零负责全局，HR 天线负责招聘。QA 等角色由 CEO 按需自行决策。

    Returns:
        创建的 agent ID 列表（第一个是 CEO）。
    """
    log.info("seed_start", project_id=project_id)
    org = OrgService()

    # ── Sync stale project_id ──────────────────────────────
    # 当 per-project DB 从旧项目残留（项目重建时 rmtree 失败或 Meta DB 被重置），
    # agents 表中的 project_id 可能仍是旧值。list_agents 按 project_id 过滤，
    # 查不到这些 agent → 会创建重复的 CEO/HR。此处先同步 project_id。
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        cursor = await conn.execute(
            "SELECT id, project_id FROM agents WHERE project_id != ?",
            [project_id],
        )
        stale = await cursor.fetchall()
        await cursor.close()
        if stale:
            log.warning(
                "seed_stale_project_id",
                project_id=project_id,
                stale_count=len(stale),
            )
            await conn.execute(
                "UPDATE agents SET project_id = ? WHERE project_id != ?",
                [project_id, project_id],
            )
            await conn.commit()
            # agent_router (内存路由) 在启动时 rebuild，此处无需同步
            log.info(
                "seed_stale_project_id_fixed",
                fixed_count=len(stale),
            )
    except Exception as e:
        log.warning("seed_sync_project_id_failed", error=str(e))

    existing = await org.list_agents(project_id)
    log.info("seed_existing_agents", project_id=project_id, count=len(existing))

    # 获取默认模型 ID — 与 org_tools.hire_agent 保持一致:
    # 1. 优先读 global_settings.default_coordinator_model (管理员显式配置)
    # 2. 回退:选最新添加的 active 模型 (不再硬编码 step- 前缀偏好)
    default_model_id = None
    try:
        from hiveweave.services.settings import SettingsService
        configured = await SettingsService().get("default_coordinator_model")
        if configured:
            default_model_id = configured
            log.info("seed_default_model_from_setting", default_model_id=default_model_id)
    except Exception as e:
        log.warning("seed_default_model_setting_read_failed", error=str(e))

    if not default_model_id:
        try:
            from hiveweave.services.model import ModelService
            ms = ModelService()
            active_models = await ms.list_active()
            if active_models:
                chosen = active_models[-1]
                default_model_id = chosen.get("model_id") or chosen.get("id")
                log.info("seed_default_model_fallback", default_model_id=default_model_id, total_models=len(active_models))
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
                "name": "归零",
                "role": "ceo",
                "goal": "掌控项目全局，管理团队，交付项目目标。",
                "backstory": "曾在三家创业公司担任技术负责人，经历过从0到1的完整周期。喜欢用最少的资源做最多的事，对过度设计过敏。咖啡成瘾，桌上永远摆着一本翻旧了的《人月神话》。话少但精准，开会时经常一句话终结三十分钟的争论。",
                "permission_type": "coordinator",
                "status": "active",
                "model_id": default_model_id,
                "skills": ["spec-driven-development", "planning-and-task-breakdown", "context-engineering"],
            },
            bootstrap=True,
        )
        ceo_id = ceo["id"]
        created_ids.append(ceo_id)
        log.info("seed_ceo_created", ceo_id=ceo_id, model_id=default_model_id, returned_model_id=ceo.get("model_id"))
        await org.create_agent(
            {
                "project_id": project_id,
                "name": "天线",
                "role": "hr",
                "goal": "根据项目需求精准招聘人才，管理组织架构和人员变动。",
                "backstory": "前猎头顾问转行 AI HR，自称「人形天线」——总能接收到哪里有合适人才的信号。面试过上千人，练就了从三句话判断候选人是否靠谱的本事。相信好的团队不是管出来的，是招出来的。桌面上养了一盆仙人掌，说是唯一不需要她照顾的东西。",
                "permission_type": "coordinator",
                "status": "active",
                "parent_id": ceo_id,
                "model_id": default_model_id,
                "skills": ["interview-me", "documentation-and-adrs"],
            },
            bootstrap=True,
        )
    except Exception as e:
        log.warning("seed_default_agents_failed", project_id=project_id, error=str(e))
    return created_ids


@router.get("")
async def list_projects(status: str | None = Query(default=None)) -> dict:
    """列出项目（支持 status 过滤: active/inactive）。

    Meta DB slimming 后只返回 id, name, workspace_path, created_at。
    前端可通过 GET /api/projects/{id} 获取完整详情（含 charter 等）。
    """
    rows = await meta_db.query(
        "SELECT id, name, workspace_path, is_started, created_at FROM projects ORDER BY created_at DESC"
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
    - 自动创建 CEO 归零 + HR 天线两个初始角色
    """
    workspace = body.workspacePath
    # R2 fix: 校验 workspace_path 安全性（绝对路径 + 无 .. 穿越段）
    ws = _validate_workspace_path(workspace)

    # 唯一性检查：同一 workspace_path 不允许创建多个项目
    # 前端去重逻辑依赖 workspace_path 匹配，如果存在重复会导致用户
    # "选中旧项目但没有 .hiveweave" 的困惑。
    ws_normalized = str(ws).replace("\\", "/").lower()
    existing_rows = await meta_db.query(
        "SELECT id, name, workspace_path FROM projects"
    )
    for row in existing_rows:
        row_dict = dict(row)
        row_ws = (row_dict.get("workspace_path") or "").replace("\\", "/").lower()
        if row_ws == ws_normalized:
            raise HTTPException(
                status_code=409,
                detail=f"Workspace already used by project '{row_dict.get('name')}' "
                       f"(id: {row_dict.get('id')}). Delete it first or choose a "
                       f"different directory.",
            )

    # 清除可能的旧驱逐标记 — 同路径重建项目时恢复 DB 访问
    project_db.clear_evicted_workspace(str(ws))

    # 修复：如果旧 .hiveweave 目录残留（删除项目时 rmtree 可能因 Windows 文件锁失败），
    # 先 evict 旧 aiosqlite 连接再 rmtree，防止旧 data.db 污染新项目。
    # 症状：旧 agents/chat_messages 残留，新 agent 数据写入 aiosqlite 内存但 commit 不落盘。
    hw_old = ws / ".hiveweave"
    if hw_old.exists():
        try:
            await project_db.evict_project_db(str(ws))
        except Exception:
            pass
        await asyncio.sleep(0.3)
        import shutil as _shutil
        import stat as _stat

        def _rmtree_on_error(func, path, exc_info):
            try:
                os.chmod(path, _stat.S_IWRITE)
            except Exception:
                pass
            try:
                func(path)
            except Exception:
                pass

        for _attempt in range(3):
            try:
                _shutil.rmtree(hw_old, onerror=_rmtree_on_error)
                break
            except Exception as e:
                log.warning("cleanup_old_hiveweave_failed",
                            workspace=str(ws), error=str(e), attempt=_attempt + 1)
                if _attempt < 2:
                    await asyncio.sleep(0.5)
        if hw_old.exists():
            log.warning("cleanup_old_hiveweave_still_exists",
                        workspace=str(ws),
                        remaining=[str(p) for p in list(hw_old.rglob("*"))[:10]])

    # BUG-FIX: evict_project_db 在清理旧 .hiveweave 时设置了 eviction flag，
    # 如果不在 rmtree 后清除，后续 ensure_project_db 会返回 None → 项目无 DB。
    project_db.clear_evicted_workspace(str(ws))

    # 确保工作空间存在（带重试，处理 Windows 文件锁 / 实时扫描瞬时拦截）
    # 之前无重试，遇到瞬时 WinError 5（如刚浏览过该目录、Defender 扫描）
    # 会直接 400 失败，用户无法创建项目。
    _mkdir_err = ""
    for attempt in range(3):
        try:
            ws.mkdir(parents=True, exist_ok=True)
            hw = ws / ".hiveweave"
            hw.mkdir(parents=True, exist_ok=True)
            # 团队共享空间 — 所有 agent 可读可写（文档、计划、临时文件、脚本）
            (hw / "shared").mkdir(exist_ok=True)
            break
        except Exception as e:
            _mkdir_err = str(e)
            if attempt < 2:
                await asyncio.sleep(0.5)
            else:
                log.error(
                    "ensure_workspace_failed",
                    workspace=workspace,
                    error=_mkdir_err,
                    attempts=3,
                    parent_exists=ws.exists(),
                    parent_writable=os.access(str(ws), os.W_OK),
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"无法创建工作空间目录（可能被其他进程锁定或权限不足）: {e}",
                )

    # 初始化 per-project DB（建表）
    try:
        conn = await project_db.ensure_project_db(workspace)
    except Exception as e:
        log.error("init_project_db_failed", workspace=workspace, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to initialize project DB: {e}")

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
            "INSERT INTO projects (id, name, workspace_path, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                project_id,
                body.name,
                str(ws),
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

    # 写入 per-project 元数据到 project_meta 表
    # (description, org_paradigm, charter_json, language 等字段从 Meta DB 迁移到 per-project DB)
    try:
        conn = await project_db.ensure_project_db(str(ws))
        await conn.execute(
            "INSERT INTO project_meta (project_id, description, org_paradigm, "
            "charter_json, goals_json, language, game_time_accumulated_seconds, "
            "updated_at) VALUES (?, ?, ?, ?, '[]', ?, 0, ?)",
            [
                project_id,
                body.description or "",
                charter.get("orgPattern", "solo"),
                json.dumps(charter, ensure_ascii=False),
                body.language or "en",
                now_ms,
            ],
        )
        await conn.commit()
    except Exception as e:
        log.warning("create_project_meta_failed", project_id=project_id, error=str(e))

    # 启动 agent + game time（C3/C4 fix: 新项目创建后立即启动运行时资源）
    # 同时设置 is_started=1 — 创建项目即"上班"，后端重启后自动恢复
    try:
        await meta_db.execute(
            "UPDATE projects SET is_started = 1 WHERE id = ?", [project_id]
        )
    except Exception as e:
        log.warning("set_is_started_after_create_failed", project_id=project_id, error=str(e))
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

    row = await meta_db.query_one(
        "SELECT id, name, workspace_path, is_started, created_at FROM projects WHERE id = ?",
        [project_id],
    )
    # 合并 project_meta (per-project DB) 到响应中
    row_dict = dict(row) if row else {"id": project_id}
    meta = await _fetch_project_meta(project_id)
    if meta:
        # project_meta 的字段覆盖 row 中的同名字段 (description, charter_json 等)
        row_dict.update({k: v for k, v in meta.items() if k != "project_id"})
    log.info("project_created", project_id=project_id, name=body.name, workspace=str(ws))
    return {
        "project": _project_response(row_dict),
        "mainAgentId": agent_ids[0] if agent_ids else None,
    }


@router.get("/{project_id}")
async def get_project(project_id: str) -> dict:
    """查单个项目（汇总 agents/roster）。

    Meta DB 只存 id/name/workspace_path/created_at，
    其余字段（description, charter_json, language 等）从 per-project DB project_meta 表读取。
    """
    row = await meta_db.query_one(
        "SELECT id, name, workspace_path, is_started, created_at FROM projects WHERE id = ?",
        [project_id],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # 合并 project_meta (per-project DB) 到行数据中
    row_dict = dict(row)
    meta = await _fetch_project_meta(project_id)
    if meta:
        row_dict.update({k: v for k, v in meta.items() if k != "project_id"})

    active_id = await _get_active_project_id()
    resp = _project_response(row_dict, active_id)

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
        "SELECT id, name, workspace_path, is_started, created_at FROM projects WHERE id = ?",
        [project_id],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    existing = dict(row)
    old_workspace = existing.get("workspace_path") or ""

    # ── Meta DB fields (name, workspace_path) ──────────────
    meta_sets: list[str] = []
    meta_vals: list = []
    if body.name is not None:
        meta_sets.append("name = ?")
        meta_vals.append(body.name)
    if body.workspacePath is not None and body.workspacePath != old_workspace:
        # 迁移工作空间
        # R2 fix: 校验新 workspace_path 安全性
        new_ws_dir = _validate_workspace_path(body.workspacePath)
        new_ws = str(new_ws_dir)
        # 新路径清除驱逐标记（旧路径的标记保留，防止 rename 过程中旧 DB 被重连）
        project_db.clear_evicted_workspace(new_ws)
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
        meta_sets.append("workspace_path = ?")
        meta_vals.append(new_ws)

    if meta_sets:
        meta_vals.append(project_id)
        await meta_db.execute(
            f"UPDATE projects SET {', '.join(meta_sets)} WHERE id = ?", meta_vals
        )

    # ── Per-project DB fields (description, charter_json) ──
    proj_sets: list[str] = []
    proj_vals: list = []
    if body.description is not None:
        proj_sets.append("description = ?")
        proj_vals.append(body.description)
    if body.operatorName is not None:
        # 从 project_meta 读取现有 charter
        meta = await _fetch_project_meta(project_id)
        charter_raw = (meta or {}).get("charter_json")
        charter: dict = {}
        if charter_raw:
            try:
                charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
            except (json.JSONDecodeError, TypeError):
                charter = {}
        charter["operatorName"] = body.operatorName
        proj_sets.append("charter_json = ?")
        proj_vals.append(json.dumps(charter, ensure_ascii=False))

    if proj_sets:
        proj_sets.append("updated_at = ?")
        proj_vals.append(int(time.time() * 1000))
        proj_vals.append(project_id)
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            await conn.execute(
                f"UPDATE project_meta SET {', '.join(proj_sets)} "
                f"WHERE project_id = ?",
                proj_vals,
            )
            await conn.commit()
        except Exception as e:
            log.warning("update_project_meta_failed", project_id=project_id, error=str(e))

    # ── Build response ─────────────────────────────────────
    updated = await meta_db.query_one(
        "SELECT id, name, workspace_path, is_started, created_at FROM projects WHERE id = ?",
        [project_id],
    )
    row_dict = dict(updated) if updated else {}
    meta = await _fetch_project_meta(project_id)
    if meta:
        row_dict.update({k: v for k, v in meta.items() if k != "project_id"})
    active_id = await _get_active_project_id()
    return {"project": _project_response(row_dict) if updated else {}}


@router.patch("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate) -> dict:
    """更新项目（PATCH）。"""
    return await _do_update_project(project_id, body)


@router.put("/{project_id}")
async def put_project(project_id: str, body: ProjectUpdate) -> dict:
    """更新项目（PUT，同 PATCH）。"""
    return await _do_update_project(project_id, body)


async def _deferred_cleanup_hiveweave(hw_dir: str, project_id: str) -> None:
    """后台延迟清理 .hiveweave 目录。

    delete_project 的 rmtree 可能因 Windows 文件锁（aiosqlite 连接未完全释放）
    而失败。此任务在后台持续重试，直到文件锁解除后成功删除。

    重试策略：每 15 秒一次，持续 5 分钟（共 20 次）。
    """
    import shutil as _shutil
    import os as _os
    from pathlib import Path as _Path

    hw = _Path(hw_dir)
    max_retries = 20
    for i in range(max_retries):
        await asyncio.sleep(15)
        if not hw.exists():
            log.info("deferred_cleanup_success",
                     hw_dir=hw_dir, project_id=project_id, attempts=i + 1)
            return
        try:
            _shutil.rmtree(hw, ignore_errors=True)
            if not hw.exists():
                log.info("deferred_cleanup_success",
                         hw_dir=hw_dir, project_id=project_id, attempts=i + 1)
                return
        except Exception as e:
            log.debug("deferred_cleanup_retry",
                      hw_dir=hw_dir, attempt=i + 1, error=str(e))

    log.error("deferred_cleanup_exhausted",
              hw_dir=hw_dir, project_id=project_id,
              attempts=max_retries)


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
    # 关键: 必须停止 write worker，否则后台 worker 会在 evict 后重新打开 DB
    try:
        from hiveweave.conversation.store import conversation_store
        from hiveweave.realtime.event_bus import status_event_bus
        from hiveweave.services.agent_router import agent_router
        agent_ids = agent_router.get_project_agent_ids(project_id)
        # 停止所有 write worker + 清理缓存（防止 evict 后重连）
        conversation_store.stop_project_workers(project_id)
        for aid in agent_ids:
            status_event_bus.set_processing(aid, False)
    except Exception:
        pass

    # 等待 agent task 完全取消 + DB 连接释放（Windows 文件锁延迟）
    # worker 收到哨兵值后需要时间退出，给足缓冲
    await asyncio.sleep(2.0)

    # 强制关闭该项目的所有 DB 连接（必须在所有内存清理之后，否则会重新打开）
    if workspace:
        try:
            await project_db.evict_project_db(workspace)
        except Exception:
            pass
        # Windows: retry eviction to ensure all handles are released
        await asyncio.sleep(0.5)
        try:
            await project_db.evict_project_db(workspace)
        except Exception:
            pass
        try:
            from hiveweave.services.agent_router import agent_router
            agent_ids = agent_router.get_project_agent_ids(project_id)
            for aid in agent_ids:
                try:
                    await project_db.evict_project_db_for_agent(aid)
                except Exception:
                    pass
        except Exception:
            pass

    # 若是当前激活项目，取消激活
    active_id = await _get_active_project_id()
    if active_id == project_id:
        await _set_active_project_id(None)

    # 先删 Meta DB 记录（projects 表）
    # 这样即使 rmtree 失败，get_project_db_for_agent 也会因 Meta DB 无记录而返回 None
    # 避免 write worker 在 evict 后重新打开 DB
    try:
        await meta_db.execute("DELETE FROM projects WHERE id = ?", [project_id])
    except Exception as e:
        log.error("delete_project_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete project")

    # 清理 agent_router 内存路由（替代旧 agent_index 表删除）
    try:
        from hiveweave.services.agent_router import agent_router
        agent_router.clear_project(project_id)
    except Exception:
        pass

    # 再删除 .hiveweave 目录（DB 文件 + worktrees + 临时文件）
    # 此时 Meta DB 已无记录，任何残留的 worker 都不会重连 DB
    if workspace:
        await asyncio.sleep(1.0)  # 延长等待，确保 Windows 文件锁释放
        hw_dir = Path(workspace) / ".hiveweave"
        if hw_dir.exists():
            import gc
            import shutil
            import stat
            import subprocess
            import time as _time

            def _on_error(func, path, exc_info):
                """强制删除：去除只读属性后重试。重试失败则抛异常（不吞）。"""
                try:
                    os.chmod(path, stat.S_IWRITE)
                except Exception:
                    pass
                func(path)  # 不 catch — 让异常传播触发外层重试

            _rmtree_ok = False
            _rmtree_err = ""

            # 强制 GC：释放可能悬空的 aiosqlite 连接对象，
            # 避免其底层文件句柄阻止 Windows 删除 data.db
            gc.collect()

            for attempt in range(3):
                try:
                    shutil.rmtree(hw_dir, onerror=_on_error)
                    _rmtree_ok = True
                    break
                except Exception as e:
                    _rmtree_err = str(e)
                    if attempt < 2:
                        _time.sleep(2.0)  # 延长等待，给 Windows 足够时间释放文件锁
                        gc.collect()       # 每次重试前再 GC 一次
                    else:
                        log.warning("delete_hiveweave_dir_failed",
                                    workspace=workspace, error=_rmtree_err)

            # Windows 兜底：shutil.rmtree 全部失败后，用原生 cmd /c rmdir 强制删除
            # Windows 原生命令对文件锁的容忍度有时高于 Python 的 os.unlink
            if not _rmtree_ok and hw_dir.exists():
                try:
                    # ignore_errors=True: 即使部分文件失败也继续，避免中途退出
                    shutil.rmtree(str(hw_dir), ignore_errors=True)
                    # 再用 rmdir 清理残留的空目录结构
                    from hiveweave.util.win_subprocess import (
                        windows_no_window_kwargs,
                    )

                    result = subprocess.run(
                        ["cmd", "/c", "rmdir", "/s", "/q", str(hw_dir)],
                        capture_output=True, text=True, timeout=30,
                        # cmd 输出跟随系统 ANSI 代码页（中文机为 GBK），
                        # 显式 locale 解码 + replace 防 illegal sequence 崩线程
                        errors="replace",
                        **windows_no_window_kwargs(),
                    )
                    if not hw_dir.exists():
                        _rmtree_ok = True
                    else:
                        log.warning("windows_rmdir_fallback_failed",
                                    workspace=workspace,
                                    returncode=result.returncode,
                                    stderr=result.stderr.strip())
                except Exception as e:
                    log.warning("windows_rmdir_fallback_error",
                                workspace=workspace, error=str(e))

            # 验证目录是否真的删掉了（rmtree 可能因 _on_error 部分吞异常而"假成功"）
            if hw_dir.exists():
                _remaining = [str(p) for p in hw_dir.rglob("*")][:10]
                log.error("hiveweave_dir_residue",
                          workspace=workspace,
                          rmtree_ok=_rmtree_ok,
                          rmtree_err=_rmtree_err,
                          remaining_files=_remaining)

                # 启动后台延迟清理任务：文件锁会在连接释放后解除
                # 持续重试 5 分钟，每 15 秒一次
                asyncio.create_task(
                    _deferred_cleanup_hiveweave(str(hw_dir), project_id)
                )

                return {
                    "ok": True,
                    "warning": f".hiveweave 目录将在后台清理: {hw_dir}",
                }

    log.info("project_deleted", project_id=project_id)
    return {"ok": True}


@router.get("/{project_id}/activate")
async def activate_project(project_id: str) -> dict:
    """激活项目（上班）。

    设置 is_started=1，启动 agents 和 game time。
    后端重启后所有项目默认 is_started=0，需要用户手动 activate。
    复工时把下班期间 park 的 inbox 合并成每 agent 一条 briefing，避免踩踏。
    """
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await _set_active_project_id(project_id)

    # 先 park 积压（此时仍 is_started=0，残留 watcher 不会开火）
    try:
        from hiveweave.services.project_lifecycle import park_project_inbox

        await park_project_inbox(project_id)
    except Exception as e:
        log.warning(
            "activate_prepark_failed",
            project_id=project_id,
            error=str(e),
        )

    # 再标记上班
    await meta_db.execute(
        "UPDATE projects SET is_started = 1 WHERE id = ?", [project_id]
    )

    # 启动 agents（如果未运行）— start_project_agents 内部会跳过已存在的
    try:
        from hiveweave.agents.supervisor import agent_manager
        await agent_manager.start_project_agents(project_id)
    except Exception as e:
        log.warning("activate_start_agents_failed", project_id=project_id, error=str(e))

    # 合并已 park 的 inbox → 每 agent 一条 wake briefing
    briefing_stats: dict = {}
    try:
        from hiveweave.services.project_lifecycle import deliver_resume_briefings

        briefing_stats = await deliver_resume_briefings(project_id)
    except Exception as e:
        log.warning(
            "activate_resume_briefings_failed",
            project_id=project_id,
            error=str(e),
        )

    # 启动 game time（如果未运行）
    try:
        gt = GameTimeService(project_id)
        await gt.start(project_id)
    except Exception as e:
        log.warning("activate_start_game_time_failed", project_id=project_id, error=str(e))

    return {
        "ok": True,
        "projectId": project_id,
        "is_started": True,
        "resumeBriefings": briefing_stats,
    }


@router.get("/{project_id}/deactivate")
async def deactivate_project(project_id: str) -> dict:
    """取消激活项目（下班）。

    1) is_started=0  2) park 未读 wake inbox  3) 停 game time
    4) 彻底停光 agent + watcher（off_duty graceful cancel）
    """
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Bug K fix: 标记项目为"下班"状态（先落库，阻断新 trigger）
    await meta_db.execute(
        "UPDATE projects SET is_started = 0 WHERE id = ?", [project_id]
    )

    parked = 0
    try:
        from hiveweave.services.project_lifecycle import park_project_inbox

        parked = await park_project_inbox(project_id)
    except Exception as e:
        log.warning("deactivate_park_inbox_failed", project_id=project_id, error=str(e))

    # 停止 game time
    try:
        gt = GameTimeService(project_id)
        await gt.stop(project_id)
    except Exception as e:
        log.warning("deactivate_stop_game_time_failed", project_id=project_id, error=str(e))

    stop_stats: dict = {}
    try:
        from hiveweave.services.project_lifecycle import stop_project_cleanly

        stop_stats = await stop_project_cleanly(project_id)
    except Exception as e:
        log.warning("deactivate_stop_agents_failed", project_id=project_id, error=str(e))

    active_id = await _get_active_project_id()
    if active_id == project_id:
        await _set_active_project_id(None)
    return {
        "ok": True,
        "projectId": project_id,
        "is_started": False,
        "parkedInbox": parked,
        "stoppedAgents": stop_stats,
    }


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

    优先从 goals_json（agent 写）读，兼容 charter_json.goals（前端写）。
    数据源: per-project DB project_meta 表。
    """
    # 先检查项目是否存在（Meta DB）
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    DEFAULT_GOALS = {
        "objective": "",
        "focus": "",
        "keyResults": [],
        "userInvolvement": "宏观决策+技术选型",
    }

    # 从 per-project DB project_meta 表读取
    meta = await _fetch_project_meta(project_id)
    goals_raw = (meta or {}).get("goals_json")
    charter_raw = (meta or {}).get("charter_json")

    # 1. Try goals_json (agent-facing, flat format)
    if goals_raw:
        try:
            goals = json.loads(goals_raw) if isinstance(goals_raw, str) else goals_raw
            if isinstance(goals, dict) and goals:
                return {"goals": goals, "projectId": project_id}
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Fallback: charter_json.goals (human-facing, nested format)
    if charter_raw:
        try:
            charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
            goals = charter.get("goals") if isinstance(charter, dict) else None
            if goals and isinstance(goals, dict):
                return {"goals": {**DEFAULT_GOALS, **goals}, "projectId": project_id}
            if isinstance(goals, str):
                return {"goals": {**DEFAULT_GOALS, "focus": goals}, "projectId": project_id}
        except (json.JSONDecodeError, TypeError):
            pass

    return {"goals": DEFAULT_GOALS, "projectId": project_id}


@router.put("/{project_id}/goals")
async def update_project_goals(project_id: str, body: CharterGoalsUpdate) -> dict:
    """更新 charter goals 段（合并写入 charter_json）。

    BUG-016 修复：POST → PUT，与前端 updateProjectGoals 的 PUT 对齐。
    保留 POST 别名以防旧客户端。
    数据源: per-project DB project_meta 表。
    """
    # 先检查项目是否存在（Meta DB）
    row = await meta_db.query_one(
        "SELECT id FROM projects WHERE id = ?", [project_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # 从 project_meta 读取现有 charter
    meta = await _fetch_project_meta(project_id)
    charter_raw = (meta or {}).get("charter_json")
    charter: dict = {}
    if charter_raw:
        try:
            charter = json.loads(charter_raw) if isinstance(charter_raw, str) else charter_raw
        except (json.JSONDecodeError, TypeError):
            charter = {}
    charter["goals"] = body.goals
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        await conn.execute(
            "UPDATE project_meta SET charter_json = ?, updated_at = ? "
            "WHERE project_id = ?",
            [json.dumps(charter, ensure_ascii=False), int(time.time() * 1000), project_id],
        )
        await conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("update_goals_failed", project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update goals")
    # 推送 WebSocket 事件 — 前端 GoalsPanel 监听后重新拉取
    try:
        from hiveweave.realtime.event_bus import status_event_bus
        await status_event_bus.publish_goals_updated(project_id)
    except Exception as e:
        log.warning("goals_updated_push_failed", project_id=project_id, error=str(e))
    return {"ok": True}


# BUG-016 兼容：保留 POST 别名
@router.post("/{project_id}/goals")
async def update_project_goals_post(project_id: str, body: CharterGoalsUpdate) -> dict:
    return await update_project_goals(project_id, body)
