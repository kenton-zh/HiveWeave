"""API Key authentication middleware (contract 19).

契约 19: ApiKeyAuth
- 凭据来源三选一: ``Authorization: Bearer <key>`` / ``X-API-Key`` header / ``?api_key=`` query
- 预期值从 ``settings.api_key`` 读取；未设置（空串）时跳过校验（dev/test 默认全放行）
- 用 ``secrets.compare_digest`` 防时序攻击
- 免认证路径: ``GET /``、``GET /api/health``
- 失败返回 401 ``{"error": "Unauthorized — invalid or missing API key"}``
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import structlog

from hiveweave.config import settings

log = structlog.get_logger(__name__)

#: 免认证路径前缀（精确匹配）
_PUBLIC_PATHS: frozenset[str] = frozenset({"/", "/api/health"})

#: 401 错误响应体
_UNAUTHORIZED_BODY = {"error": "Unauthorized — invalid or missing API key"}


def _extract_provided_key(request: Request) -> str | None:
    """从请求中提取调用方提供的 API key（三选一）。

    优先级: Authorization Bearer → X-API-Key → ?api_key=
    """
    # 1. Authorization: Bearer <key>
    auth_header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    # 2. X-API-Key header (case-insensitive)
    for key in ("x-api-key", "X-API-Key", "X-Api-Key"):
        val = request.headers.get(key)
        if val:
            return val.strip()
    # 3. ?api_key= query param
    raw_qs = request.url.query
    if raw_qs:
        qs = parse_qs(raw_qs, keep_blank_values=False)
        for k in ("api_key", "apiKey"):
            vals = qs.get(k)
            if vals:
                return vals[0].strip()
    return None


def _is_public(path: str) -> bool:
    """路径是否免认证。"""
    if path in _PUBLIC_PATHS:
        return True
    # /api/health 之后的 query string 已被 request.url.path 去掉
    return False


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """ASGI 中间件 — 校验所有 ``/api/*`` 端点（除 ``/api/health``）的 API key。

    用法::

        from hiveweave.api.auth import ApiKeyMiddleware
        app.add_middleware(ApiKeyMiddleware)
    """

    async def dispatch(
        self, request: Request, call_next
    ) -> Response:  # type: ignore[override]
        path = request.url.path

        # 非 /api 路径放行（含 GET /）
        if not path.startswith("/api"):
            return await call_next(request)

        # 免认证路径
        if _is_public(path):
            return await call_next(request)

        # settings.api_key 未设置 → dev/test 全放行
        expected = settings.api_key
        if not expected:
            return await call_next(request)

        provided = _extract_provided_key(request)
        if provided is None or not secrets.compare_digest(provided, expected):
            log.warning(
                "api_key_auth_failed", path=path, method=request.method
            )
            return JSONResponse(
                status_code=401, content=_UNAUTHORIZED_BODY
            )

        return await call_next(request)


async def verify_api_key(request: Request) -> None:
    """FastAPI 依赖形式 — 用于需要显式声明认证的路由。

    与 ``ApiKeyMiddleware`` 等价，但允许在 ``Depends`` 中使用。
    中间件已全局校验，此依赖通常只在未挂载中间件时生效。
    """
    path = request.url.path
    if not path.startswith("/api") or _is_public(path):
        return
    expected = settings.api_key
    if not expected:
        return
    provided = _extract_provided_key(request)
    if provided is None or not secrets.compare_digest(provided, expected):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_BODY["error"])


# ── 路径参数校验 ────────────────────────────────────────────

#: 安全 ID 格式 — 仅允许字母、数字、下划线、短横线（防 ``../`` 注入）
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_id(id_value: str, name: str = "id") -> None:
    """校验路径参数 ID 格式，防止 ``../`` 等特殊字符注入。

    仅允许字母、数字、下划线、短横线。UUID、short_id、project slug 均符合。

    Args:
        id_value: 路径参数值
        name: 参数名（用于错误消息）

    Raises:
        HTTPException: 400 — 含非法字符
    """
    if not id_value or not _ID_PATTERN.match(id_value):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid {name}: must be alphanumeric, dash, or underscore "
                f"(no slashes, dots, or special chars)"
            ),
        )
