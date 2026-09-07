"""平台级助理 — 隐藏系统工作区的 CEO（spec §7，决策 D6）。

实体：固定 project_id 的隐藏系统工作区（不出现在项目列表），助理即其
CEO；部署首启自动创建（幂等），随部署存在、永不随项目归档。

复用全部 agent 基础设施（chat / inbox / turn / 工具 / 记忆 / 模型 tier
解析）：不发明新物种——助理就是一个普通 Agent 行，落在系统工作区的
per-project DB 里；模型配置走现有 ModelService（model_id 留空 →
management tier 动态解析）。

助理红线（spec §4.4）：不进项目指挥链——role=ceo（行政 family）天然无
SOURCE_WRITE/bash，可以传话、不写业务代码。

P0 最小形态：用户↔助理对话入口 + 项目 CEO 解析（悬浮球切换标签用）。
装环境/引导飞书/盯梢属 P1+（spec §7 四职责的其余三项）。
"""

from __future__ import annotations

import time

import structlog

from hiveweave.config import get_assistant_workspace
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

log = structlog.get_logger(__name__)

#: 固定系统工作区 project_id（spec §7「固定 project_id」）。带 hw- 前缀
#: 与 UUID 形态的用户项目区分，列表过滤/排重都以它为准。
ASSISTANT_PROJECT_ID = "hw-system-assistant"

#: 助理 agent 固定 id —— 重启用同一行，红点桥/球端引用稳定。
ASSISTANT_AGENT_ID = "hw-system-assistant-agent"

ASSISTANT_AGENT_NAME = "助理"


async def ensure_assistant() -> dict:
    """确保助理系统工作区与助理 agent 存在（幂等，可多次调用）。

    Returns:
        {"project_id", "agent_id", "workspace", "created_project", "created_agent"}
    """
    workspace = str(get_assistant_workspace())
    now_ms = int(time.time() * 1000)

    # ── 1. Meta DB 项目行（隐藏系统工作区）────────────────────
    # is_started=1：助理不随全局「下班」停摆（lifespan 启动时会把所有
    # 项目 is_started 归零——Bug K 语义；每次 ensure 都拉回 1，助理
    # 常驻平台，随部署存在）。
    row = await meta_db.query_one(
        "SELECT id, workspace_path FROM projects WHERE id = ? LIMIT 1",
        [ASSISTANT_PROJECT_ID],
    )
    created_project = False
    if row is None:
        try:
            await meta_db.execute(
                "INSERT INTO projects (id, name, workspace_path, created_at, "
                "is_started) VALUES (?, ?, ?, ?, 1)",
                [ASSISTANT_PROJECT_ID, "__assistant__", workspace, now_ms],
            )
            created_project = True
            log.info("assistant_project_created", workspace=workspace)
        except Exception as e:  # noqa: BLE001 — 审计 M5：并发 seed 双 INSERT
            if "UNIQUE" not in str(e):
                raise
            log.info("assistant_project_seed_race_lost")  # 对方已建，继续用
    else:
        # 审计 H1：lifespan 每次启动把全表 is_started 归零（Bug K 语义），
        # 助理是常驻系统项目必须拉回 1——否则重启后 agent.chat 撞
        # project_not_started 门，助理永久回「已下班」。幂等写。
        await meta_db.execute(
            "UPDATE projects SET is_started = 1 WHERE id = ?",
            [ASSISTANT_PROJECT_ID],
        )
        cur_ws = row["workspace_path"]
        if cur_ws != workspace:
            # 数据根迁移（env 改变/换机器）→ 工作区跟随数据根
            await meta_db.execute(
                "UPDATE projects SET workspace_path = ? WHERE id = ?",
                [workspace, ASSISTANT_PROJECT_ID],
            )
            from hiveweave.db.project import evict_project_db

            if cur_ws:
                try:
                    await evict_project_db(cur_ws)
                except Exception as e:
                    log.warning(
                        "assistant_workspace_evict_failed",
                        workspace=cur_ws,
                        error=str(e),
                    )
            log.info(
                "assistant_workspace_moved",
                old=cur_ws,
                new=workspace,
            )

    # ── 2. per-project DB（agents/chat_messages/... 都在这里）────
    await project_db.ensure_project_db(workspace)

    # ── 3. 助理 agent（系统工作区的 CEO，D6）────────────────────
    from hiveweave.services.org import OrgService

    org = OrgService()
    existing = await org.get_agent(ASSISTANT_AGENT_ID)
    created_agent = False
    if existing is None:
        try:
            await org.create_agent(
                {
                    "id": ASSISTANT_AGENT_ID,
                    "project_id": ASSISTANT_PROJECT_ID,
                    "name": ASSISTANT_AGENT_NAME,
                    "role": "ceo",
                    "goal": (
                        "作为平台级助理服务用户：答疑、指路、按用户名义向项目 "
                        "CEO 传话；解释平台功能与组织状态。不写业务代码、不替 "
                        "用户拍板（传话可以，决策必须用户本人）。"
                    ),
                    "backstory": (
                        "平台常驻助理，跨项目存在，熟悉每个项目的组织与任务 "
                        "状态。口吻直接友好，回答简短，习惯先给结论再给依据。"
                    ),
                    "permission_type": "coordinator",
                    "status": "active",
                    # model_id 留空 → 运行时按 management tier 经 ModelService
                    # 动态解析（模型配置走现有 ModelService，spec P0 要求）
                    "language": "zh",
                    "skills": [],
                },
                bootstrap=True,
            )
            created_agent = True
            log.info("assistant_agent_created", agent_id=ASSISTANT_AGENT_ID)
        except Exception as e:  # noqa: BLE001 — 审计 M5：并发 seed 竞态
            if "UNIQUE" not in str(e):
                raise
            log.info("assistant_agent_seed_race_lost")

    return {
        "project_id": ASSISTANT_PROJECT_ID,
        "agent_id": ASSISTANT_AGENT_ID,
        "workspace": workspace,
        "created_project": created_project,
        "created_agent": created_agent,
    }


async def ensure_agent_started(agent_id: str) -> bool:
    """确保任意 agent 已在 AgentManager 中启动（幂等，挂总线回调）。

    与 api/chat.py 的 ``_ensure_agent_started`` 同口径，但回调固定连到
    事件总线（create_agent_callbacks）——网页端实时渲染与悬浮球红点桥
    都消费总线事件。助理与悬浮球 CEO 标签共用。
    """
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.realtime.event_bus import create_agent_callbacks

    if agent_manager.get_agent(agent_id) is not None:
        return True

    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        return False
    project_id = config.get("project_id") or (
        await meta_db.get_agent_project_id(agent_id)
    )
    if not project_id:
        return False
    on_status, on_stream = create_agent_callbacks(agent_id, project_id)
    await agent_manager.start_agent(
        agent_id,
        project_id,
        config,
        on_status_change=on_status,
        on_stream_event=on_stream,
    )
    return True


async def ensure_assistant_agent_started() -> bool:
    """确保助理 agent 已启动（幂等；先保证工作区/agent 行存在）。"""
    await ensure_assistant()
    return await ensure_agent_started(ASSISTANT_AGENT_ID)


async def chat_with_assistant(
    content: str,
    *,
    source: str = "ball",
) -> dict:
    """用户↔助理对话入口（spec §7 前端入口的服务侧）。

    Args:
        content: 用户消息文本。
        source: 来源面（``ball`` | ``web`` | ``feishu``）。

    Returns:
        ``deliver_user_message`` 的结构化结果 + ``agent_id``。
    """
    from hiveweave.services.user_message import deliver_user_message

    started = await ensure_assistant_agent_started()
    if not started:
        return {
            "ok": False,
            "outcome": "not_found",
            "user_message_id": None,
            "assistant_message_id": None,
            "assistant_content": None,
            "error": "Assistant agent unavailable",
            "agentId": ASSISTANT_AGENT_ID,
        }
    result = await deliver_user_message(ASSISTANT_AGENT_ID, content, source)
    result["agentId"] = ASSISTANT_AGENT_ID
    return result


async def resolve_project_ceo(project_id: str) -> dict | None:
    """动态解析项目 CEO（D2 口径：role=ceo 且 active，ship_nudge 同款）。"""
    row = await meta_db.query_one(
        "SELECT workspace_path FROM projects WHERE id = ? LIMIT 1",
        [project_id],
    )
    if row is None or not row["workspace_path"]:
        return None
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
    except project_db.ProjectDbError:
        return None
    cursor = await conn.execute(
        "SELECT id, name, role, status FROM agents "
        "WHERE project_id = ? AND lower(role) = 'ceo' AND status = 'active' "
        "LIMIT 1",
        [project_id],
    )
    agent_row = await cursor.fetchone()
    await cursor.close()
    return dict(agent_row) if agent_row else None


def _ceo_from_router(project_id: str) -> dict | None:
    """内存路由直取 CEO（381dbe5 P1 备忘：/api/ball/state 轮询热路径消 N+1）。

    前端红点轮询周期性打该端点；原先每项目 2 条 DB 查询（53 项目 ≈ 106
    条/轮）。路由未命中（收养未重启/路由降级）回落 resolve_project_ceo。
    """
    try:
        from hiveweave.services.agent_router import agent_router

        for aid in agent_router.get_project_agent_ids(project_id):
            route = agent_router.get_route(aid)
            if (
                route is not None
                and str(route.role or "").lower() == "ceo"
                and str(route.status or "") == "active"
            ):
                return {"id": route.agent_id, "name": route.name or route.agent_id}
    except Exception:
        pass
    return None


async def list_ball_targets() -> dict:
    """悬浮球展开态的对话目标（spec §10 顶部切换标签）。

    Returns:
        {
          "assistant": {"agentId", "name"},
          "projects": [{"projectId", "name", "isStarted",
                        "ceo": {"agentId", "name"} | None}, ...]
        }
    """
    await ensure_assistant()
    rows = await meta_db.query(
        "SELECT id, name, is_started FROM projects "
        "WHERE id != ? ORDER BY created_at DESC",
        [ASSISTANT_PROJECT_ID],
    )
    projects: list[dict] = []
    for r in rows:
        pid = r["id"]
        ceo = _ceo_from_router(pid) or await resolve_project_ceo(pid)
        projects.append(
            {
                "projectId": pid,
                "name": r["name"],
                "isStarted": bool(r["is_started"]),
                "ceo": (
                    {"agentId": ceo["id"], "name": ceo["name"]}
                    if ceo
                    else None
                ),
            }
        )
    return {
        "assistant": {
            "agentId": ASSISTANT_AGENT_ID,
            "name": ASSISTANT_AGENT_NAME,
        },
        "projects": projects,
    }
