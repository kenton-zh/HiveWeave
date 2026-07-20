"""Agent communications + user ping endpoints (contract 19, group 11 + 12).

契约 19: Communications + UserPings
- GET  /api/communications?projectId=...     列出 agent 间通信
- POST /api/communications                   发送 agent 间消息
- GET  /api/user-pings?projectId=...         列出用户 ping 通知
- POST /api/user-pings/{id}/read             标记用户 ping 已读
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)

router = APIRouter(tags=["communications"])

_inbox = InboxService()


class CommunicationCreate(BaseModel):
    fromAgentId: str
    toAgentId: str
    content: str
    type: str | None = "normal"
    subject: str | None = None
    priority: str | None = "normal"
    metadata: dict | None = None


@router.get("/api/communications")
async def list_communications(
    projectId: str | None = Query(default=None),
    agentId: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> dict:
    """列出 agent 间通信（per-project inbox 表）。"""
    if not projectId and not agentId:
        return {"communications": []}
    try:
        if agentId:
            rows = await project_db.query(
                agentId,
                "SELECT * FROM inbox WHERE to_agent_id = ? OR from_agent_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                [agentId, agentId, limit],
            )
        else:
            assert projectId is not None  # 上面已确认 projectId or agentId
            workspace = await meta_db.get_project_workspace(projectId)
            if not workspace:
                return {"communications": []}
            from hiveweave.db.project import ensure_project_db

            conn = await ensure_project_db(workspace)
            cursor = await conn.execute(
                "SELECT * FROM inbox ORDER BY created_at DESC LIMIT ?",
                [limit],
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return {"communications": [dict(r) for r in rows]}
    except Exception as e:
        log.warning("list_communications_failed", error=str(e))
        return {"communications": []}


@router.post("/api/communications")
async def create_communication(body: CommunicationCreate) -> dict:
    """发送 agent 间消息（写 inbox 表）。"""
    try:
        msg = await _inbox.send_message(
            from_agent_id=body.fromAgentId,
            to_agent_id=body.toAgentId,
            message=body.content,
            message_type=body.type or "normal",
            priority=body.priority or "normal",
        )
    except Exception as e:
        log.error("create_communication_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create communication")
    return {"ok": True, "communication": msg}


# ── User pings ───────────────────────────────────────────────


async def _ensure_user_pings_table(agent_or_project_key: str, *, project_id: str | None = None) -> None:
    """确保 user_pings 表存在（per-project DB）。"""
    sql = (
        "CREATE TABLE IF NOT EXISTS user_pings ("
        "id TEXT PRIMARY KEY, "
        "project_id TEXT, "
        "from_agent_id TEXT, "
        "message TEXT, "
        "is_read INTEGER DEFAULT 0, "
        "created_at INTEGER, "
        "read_at INTEGER"
        ")"
    )
    if project_id:
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            return
        from hiveweave.db.project import ensure_project_db

        conn = await ensure_project_db(workspace)
        await conn.execute(sql)
        await conn.commit()
    else:
        await project_db.execute(agent_or_project_key, sql, [])


@router.get("/api/user-pings")
async def list_user_pings(
    projectId: str | None = Query(default=None),
    agentId: str | None = Query(default=None),
    unreadOnly: bool = Query(default=False),
) -> dict:
    """列出用户 ping 通知。"""
    if not projectId and not agentId:
        return {"pings": []}
    try:
        if projectId:
            await _ensure_user_pings_table(projectId, project_id=projectId)
            workspace = await meta_db.get_project_workspace(projectId)
            if not workspace:
                return {"pings": []}
            from hiveweave.db.project import ensure_project_db

            conn = await ensure_project_db(workspace)
            sql = "SELECT * FROM user_pings WHERE project_id = ?"
            params: list = [projectId]
            if unreadOnly:
                sql += " AND is_read = 0"
            sql += " ORDER BY created_at DESC LIMIT 100"
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
        else:
            assert agentId is not None  # 上面已确认 projectId or agentId
            await _ensure_user_pings_table(agentId)
            sql = "SELECT * FROM user_pings WHERE from_agent_id = ?"
            params = [agentId]
            if unreadOnly:
                sql += " AND is_read = 0"
            sql += " ORDER BY created_at DESC LIMIT 100"
            rows = await project_db.query(agentId, sql, params)
        return {"pings": [dict(r) for r in rows]}
    except Exception as e:
        log.warning("list_user_pings_failed", error=str(e))
        return {"pings": []}


@router.post("/api/user-pings/{ping_id}/read")
async def mark_ping_read(ping_id: str, agentId: str | None = Query(default=None)) -> dict:
    """标记用户 ping 已读。"""
    # 需要 agentId 来定位 per-project DB
    key = agentId
    if not key:
        # 尝试所有缓存？不实际。要求传 agentId。
        raise HTTPException(
            status_code=422, detail="agentId query param required to locate project DB"
        )
    try:
        await project_db.execute(
            key,
            "UPDATE user_pings SET is_read = 1, read_at = ? WHERE id = ?",
            [int(time.time() * 1000), ping_id],
        )
    except Exception as e:
        log.error("mark_ping_read_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to mark ping read")
    return {"ok": True}


# ── 前端 RESTful 路径参数兼容路由 ─────────────────────────────
# 前端期望 /api/communications/{agentId}/inbox 风格；保留现有 query 风格路由，
# 额外提供 path 参数变体。
# COMPAT: 前端 api.ts 期望的 RESTful 路径


@router.get("/api/communications/{agent_id}/inbox")
async def list_communications_inbox_path(
    agent_id: str, limit: int = Query(default=100, le=500)
) -> dict:
    """agent 收件箱（path: agentId）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    return await list_communications(agentId=agent_id, limit=limit)
