"""HiveWeave HTTP API layer (contract 19).

16 分组、62+ 端点的 FastAPI 路由聚合。所有子路由用 ``APIRouter``，
请求/响应用 Pydantic v2 模型，端点用 ``async def``。

公共导出:
- :func:`register_routes` — 把所有路由挂到 FastAPI app
- :class:`ApiKeyMiddleware` — API Key 认证中间件
- :func:`verify_api_key` — FastAPI 依赖形式的认证
"""

from hiveweave.api.auth import ApiKeyMiddleware, verify_api_key
from hiveweave.api.router import register_routes

__all__ = ["ApiKeyMiddleware", "verify_api_key", "register_routes"]
