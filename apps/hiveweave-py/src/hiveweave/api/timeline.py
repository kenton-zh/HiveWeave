"""Timeline REST API (Timeline v4 §4.3).

两个只读聚合端点，数据源唯一入口（WS 只作失效信号）：

- ``GET /api/projects/{project_id}/timeline/tasks/{task_id}``
  单任务全链路事件流（task_events / handoffs / inbox / work_logs 归并）。
- ``GET /api/projects/{project_id}/timeline/activity``
  团队活动段（泳道视图）+ active_assignments，支持 cursor 分页与
  if_changed_since 无变化短路。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
import structlog

from hiveweave.db.project import ProjectDbError
from hiveweave.services.tasks.timeline import timeline_service

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/timeline", tags=["timeline"]
)


@router.get("/tasks/{task_id}")
async def get_task_timeline(
    project_id: str,
    task_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """单任务全链路事件流（含归档任务 — task_id 直达）。"""
    try:
        result = await timeline_service.get_task_timeline(
            project_id, task_id, limit=limit
        )
    except ProjectDbError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("task") is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.get("/activity")
async def get_team_activity(
    project_id: str,
    since_ms: int = Query(..., description="窗口起点（现实毫秒）"),
    until_ms: int = Query(..., description="窗口终点（现实毫秒）"),
    limit: int = Query(default=2000, ge=1, le=5000),
    cursor_ts: int | None = Query(
        default=None, description="游标分页：替代 since_ms 加载更早"
    ),
    if_changed_since: int | None = Query(
        default=None,
        description="上次 max_event_ts；无变化时返回 {changed:false}",
    ),
) -> dict:
    """团队活动段 + 当前分配（泳道视图数据源）。"""
    try:
        return await timeline_service.get_team_activity(
            project_id,
            since_ms=since_ms,
            until_ms=until_ms,
            limit=limit,
            cursor_ts=cursor_ts,
            if_changed_since=if_changed_since,
        )
    except ProjectDbError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
