"""Alarm + game-time endpoints (contract 19, group 13 + 16 game-time).

契约 19: Alarms + GameTime
- GET    /api/alarms?projectId=             列出项目闹钟
- POST   /api/alarms                         创建闹钟
- DELETE /api/alarms/{id}?projectId=         取消闹钟
- GET    /api/game-time/{projectId}          查游戏时间
- PUT    /api/game-time/{projectId}/speed    设置时间速度（此实现为固定 24x，仅返回）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.services.game_time import GameTimeService, REAL_SECONDS_PER_GAME_DAY

log = structlog.get_logger(__name__)

router = APIRouter(tags=["alarms"])

_game_time = GameTimeService()


class AlarmCreate(BaseModel):
    projectId: str
    fromAgentId: str
    toAgentId: str
    purpose: str
    fireAtGameSeconds: int


@router.get("/api/alarms")
async def list_alarms(projectId: str = Query(...)) -> dict:
    """列出项目闹钟。"""
    try:
        from hiveweave.services.game_time import is_project_tombstoned

        if is_project_tombstoned(projectId):
            return {"alarms": []}
        alarms = await _game_time.get_alarms(projectId)
    except Exception as e:
        # BUG-6: missing workspace is expected for deleted projects — debug only
        if "Workspace not found" in str(e):
            try:
                from hiveweave.services.game_time import mark_project_tombstoned
                mark_project_tombstoned(projectId)
            except Exception:
                pass
            log.debug("list_alarms_tombstone", project_id=projectId)
        else:
            log.warning("list_alarms_failed", error=str(e))
        alarms = []
    return {"alarms": alarms}


@router.post("/api/alarms")
async def create_alarm(body: AlarmCreate) -> dict:
    """创建闹钟。"""
    try:
        alarm_id = await _game_time.schedule_alarm(
            project_id=body.projectId,
            from_agent_id=body.fromAgentId,
            to_agent_id=body.toAgentId,
            purpose=body.purpose,
            fire_at_game_seconds=body.fireAtGameSeconds,
        )
    except Exception as e:
        log.error("create_alarm_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create alarm")
    return {"ok": True, "alarmId": alarm_id}


@router.delete("/api/alarms/{alarm_id}")
async def cancel_alarm(alarm_id: str, projectId: str = Query(default=None)) -> dict:
    """取消闹钟。"""
    try:
        await _game_time.cancel_alarm(alarm_id)
    except Exception as e:
        log.error("cancel_alarm_failed", alarm_id=alarm_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to cancel alarm")
    return {"ok": True, "alarmId": alarm_id}


@router.get("/api/game-time/{project_id}")
async def get_game_time(project_id: str) -> dict:
    """查游戏时间。

    BUG-005 修复：返回 real_started_at + 速率让前端本地计算，
    不再需要每秒 HTTP poll。
    """
    try:
        result = await _game_time.get_current_time(project_id)
    except Exception as e:
        log.error("get_game_time_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get game time")
    return {
        "projectId": project_id,
        "gameSeconds": result.get("game_seconds", 0),
        "formatted": result.get("formatted", ""),
        "realStartedAt": result.get("real_started_at"),
        "realSecondsPerGameDay": result.get("real_seconds_per_game_day", 3600),
    }


@router.put("/api/game-time/{project_id}/speed")
async def set_game_time_speed(project_id: str, speed: int = Query(...)) -> dict:
    """设置时间速度。

    此实现使用固定比例（1 真实小时 = 1 游戏日，即 24x），
    speed 参数记录但不生效，仅返回当前实际比例。
    """
    log.info("set_game_time_speed_called", project_id=project_id, requested=speed)
    return {
        "projectId": project_id,
        "requestedSpeed": speed,
        "actualSpeed": 24,
        "realSecondsPerGameDay": REAL_SECONDS_PER_GAME_DAY,
        "note": "speed is fixed at 24x (1 real hour = 1 game day) in this build",
    }


# ── 前端 RESTful 路径参数兼容路由 ─────────────────────────────
# 前端 api.ts 期望 /api/projects/{projectId}/... 风格的嵌套路径，
# 而原契约路由为扁平的 /api/alarms、/api/game-time/{id}。
# 以下路由复用已有处理函数，仅做路径适配。COMPAT: 前端 api.ts 期望的 RESTful 路径


@router.get("/api/projects/{project_id}/game-time")
async def get_game_time_compat(project_id: str) -> dict:
    """COMPAT: 前端 api.ts 期望的 RESTful 路径。

    BUG-020 fix: 优雅降级，避免 project 未初始化时返回 500。
    """
    try:
        from hiveweave.services.game_time import is_project_tombstoned

        if is_project_tombstoned(project_id):
            return {
                "projectId": project_id,
                "gameSeconds": 0,
                "formatted": "Day 0 00:00",
                "realStartedAt": None,
                "realSecondsPerGameDay": 3600,
            }
        result = await _game_time.get_current_time(project_id)
    except Exception as e:
        if "Workspace not found" in str(e):
            try:
                from hiveweave.services.game_time import mark_project_tombstoned
                mark_project_tombstoned(project_id)
            except Exception:
                pass
            log.debug("get_game_time_compat_tombstone", project_id=project_id)
        else:
            log.warning(
                "get_game_time_compat_failed",
                project_id=project_id,
                error=str(e),
            )
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


@router.get("/api/projects/{project_id}/alarms")
async def list_alarms_compat(project_id: str) -> dict:
    """COMPAT: 前端 api.ts 期望的 RESTful 路径"""
    return await list_alarms(projectId=project_id)


@router.post("/api/projects/{project_id}/alarms")
async def create_alarm_compat(project_id: str, body: AlarmCreate) -> dict:
    """COMPAT: 前端 api.ts 期望的 RESTful 路径"""
    body.projectId = project_id  # 覆盖为 path 参数
    return await create_alarm(body)


@router.delete("/api/projects/{project_id}/alarms/{alarm_id}")
async def cancel_alarm_compat(project_id: str, alarm_id: str) -> dict:
    """COMPAT: 前端 api.ts 期望的 RESTful 路径"""
    return await cancel_alarm(alarm_id, projectId=project_id)
