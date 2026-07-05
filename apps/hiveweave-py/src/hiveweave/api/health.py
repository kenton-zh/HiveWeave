"""Health check endpoints (contract 19, group 1).

契约 19: Health
- GET /api/health — 免认证，返回 {status, version, timestamp}
- GET /api/version — 返回版本信息
"""

from __future__ import annotations

import time

from fastapi import APIRouter

import structlog

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

#: 应用版本号（对齐 Elixir 0.2.0）
APP_VERSION = "0.2.0"


@router.get("/api/health")
async def health() -> dict:
    """健康检查（免认证）。

    契约 19: ``{status: "ok", version: "0.2.0", timestamp: <ms>}``
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "timestamp": int(time.time() * 1000),
    }


@router.get("/api/version")
async def version() -> dict:
    """版本信息。"""
    return {
        "version": APP_VERSION,
        "name": "HiveWeave API",
        "timestamp": int(time.time() * 1000),
    }
