"""Token usage REST API (token metering F5).

每个 agent 的 LLM token 消耗量化端点，数据源为 per-project ``llm_usage`` 表
（TokenMeter 服务聚合）。全部只读，best-effort —— 表/库不存在时返回空而非报错。

- ``GET /api/projects/{project_id}/token-usage``
  按 agent × request_type 拆分的汇总（main / compaction_* / subagent）。
- ``GET /api/projects/{project_id}/token-usage/daily``
  按天然日分组的汇总（近 N 天，默认 30）。
- ``GET /api/projects/{project_id}/token-usage/agents/{agent_id}``
  单 agent 汇总（可带时间窗）。
- ``GET /api/token-usage/runs/{run_id}``
  单次 run 的 token 归因（跨项目扫描）。
- ``GET /api/token-usage/platform``
  平台级跨项目聚合（按 agent 汇总）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
import structlog

from hiveweave.services.token_meter import token_meter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["token-usage"])


@router.get("/projects/{project_id}/token-usage")
async def get_project_token_usage(
    project_id: str,
    since_ms: int | None = Query(default=None, description="窗口起点（现实毫秒）"),
) -> dict:
    """按 agent × request_type 投影的 token 汇总。"""
    try:
        rows = await token_meter.project_by_agent(project_id, since=since_ms)
    except Exception as e:
        log.warning("token_usage.project_failed",
                    project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to query token usage")
    return {"project_id": project_id, "entries": rows}


@router.get("/projects/{project_id}/token-usage/daily")
async def get_project_token_daily(
    project_id: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """按天然日晚粒度汇总（近 N 天）。"""
    try:
        rows = await token_meter.daily_summary(project_id, since_days=days)
    except Exception as e:
        log.warning("token_usage.daily_failed",
                    project_id=project_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to query daily usage")
    return {"project_id": project_id, "days": days, "entries": rows}


@router.get("/projects/{project_id}/token-usage/agents/{agent_id}")
async def get_agent_token_usage(
    project_id: str,
    agent_id: str,
    since_ms: int | None = Query(default=None, description="窗口起点（现实毫秒）"),
    until_ms: int | None = Query(default=None, description="窗口终点（现实毫秒）"),
) -> dict:
    """单 agent 的 token 汇总（可带时间窗）。"""
    try:
        summary = await token_meter.agent_summary(
            project_id, agent_id, since=since_ms, until=until_ms
        )
    except Exception as e:
        log.warning("token_usage.agent_failed",
                    project_id=project_id, agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to query agent usage")
    return {"project_id": project_id, "agent_id": agent_id, "summary": summary}


@router.get("/token-usage/runs/{run_id}")
async def get_run_token_usage(run_id: str) -> dict:
    """单次 run 的 token 归因（跨项目扫描，按 run_id 定位）。"""
    try:
        result = await token_meter.run_summary(run_id)
    except Exception as e:
        log.warning("token_usage.run_failed", run_id=run_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to query run usage")
    if result is None:
        raise HTTPException(status_code=404, detail="Run token usage not found")
    return result


@router.get("/token-usage/platform")
async def get_platform_token_usage(
    since_ms: int | None = Query(default=None, description="窗口起点（现实毫秒）"),
) -> dict:
    """平台级跨项目聚合（按 agent 汇总）。"""
    try:
        rows = await token_meter.platform_overview(since=since_ms)
    except Exception as e:
        log.warning("token_usage.platform_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to query platform usage")
    return {"entries": rows}