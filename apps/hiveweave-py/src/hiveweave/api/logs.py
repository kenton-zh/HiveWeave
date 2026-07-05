"""Work logs + event audit endpoints (contract 19, group 14 + 15).

契约 19: WorkLogs + Debug(events)
- GET /api/work-logs/{agentId}                agent 工作日志（限 50 条）
- GET /api/work-logs/{agentId}/subordinates   下属工作日志（聚合）
- GET /api/events/audit?agentId=&hours=       事件审计时间线
- GET /api/events?projectId=                  项目事件流（agent_events 全量）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.org import OrgService
from hiveweave.services.work_log import WorkLogService
from hiveweave.services.event_audit import event_audit

log = structlog.get_logger(__name__)

router = APIRouter(tags=["logs"])

_work_log = WorkLogService()
_org = OrgService()


@router.get("/api/work-logs/{agent_id}")
async def get_work_logs(agent_id: str, limit: int = Query(default=50, le=200)) -> dict:
    """agent 工作日志（newest first）。"""
    project_id = await meta_db.get_agent_project_id(agent_id)
    if not project_id:
        raise HTTPException(status_code=404, detail="Agent project not found")
    try:
        logs = await _work_log.get_logs(project_id, agent_id, limit=limit)
    except Exception as e:
        log.warning("get_work_logs_failed", agent_id=agent_id, error=str(e))
        logs = []
    return {"logs": logs, "agentId": agent_id}


@router.get("/api/work-logs/{agent_id}/subordinates")
async def get_subordinate_logs(
    agent_id: str, limit: int = Query(default=20, le=100)
) -> dict:
    """下属工作日志（聚合所有直接下属）。"""
    project_id = await meta_db.get_agent_project_id(agent_id)
    if not project_id:
        raise HTTPException(status_code=404, detail="Agent project not found")
    subordinates = await _org.get_subordinates(agent_id)
    if not subordinates:
        return {"logs": [], "subordinates": []}
    aggregated: list[dict] = []
    for sub in subordinates:
        sub_id = sub["id"]
        try:
            logs = await _work_log.get_logs(project_id, sub_id, limit=limit)
        except Exception as e:
            log.warning("get_subordinate_logs_failed", sub_id=sub_id, error=str(e))
            logs = []
        for entry in logs:
            entry["subordinate_id"] = sub_id
            entry["subordinate_name"] = sub.get("name")
            aggregated.append(entry)
    # 按时间倒序
    aggregated.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {
        "logs": aggregated,
        "subordinates": [{"id": s["id"], "name": s.get("name")} for s in subordinates],
    }


@router.get("/api/events/audit")
async def event_audit_timeline(
    agentId: str = Query(...),
    hours: int = Query(default=1, le=168),
    limit: int = Query(default=100, le=500),
) -> dict:
    """事件审计时间线（agent_events 表）。"""
    events = await event_audit.timeline(agentId, hours=hours, limit=limit)
    return {"events": events, "agentId": agentId}


@router.get("/api/events")
async def project_events(
    projectId: str = Query(...),
    limit: int = Query(default=100, le=500),
) -> dict:
    """项目事件流（agent_events 全量，按 created_at DESC）。"""
    try:
        workspace = await meta_db.get_project_workspace(projectId)
        if not workspace:
            return {"events": []}
        from hiveweave.db.project import ensure_project_db

        conn = await ensure_project_db(workspace)
        cursor = await conn.execute(
            "SELECT id, agent_id, event_type, payload, created_at "
            "FROM agent_events ORDER BY created_at DESC LIMIT ?",
            [limit],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        import json as _json

        out = []
        for r in rows:
            d = dict(r)
            if d.get("payload"):
                try:
                    d["payload"] = _json.loads(d["payload"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return {"events": out}
    except Exception as e:
        log.warning("project_events_failed", error=str(e))
        return {"events": []}
