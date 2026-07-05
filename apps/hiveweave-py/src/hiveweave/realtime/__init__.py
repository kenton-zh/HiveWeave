"""Realtime WebSocket channels (contract 12).

实时通信层 — 进程内 pub/sub + WebSocket 3 channel。

模块:
- ``event_bus`` — StatusEventBus（进程内事件总线）
- ``pubsub`` — PubSub 广播层（进程内 + 可选 Redis）
- ``channels`` — WebSocket 端点（lobby / agent / chat）

主要导出:
- ``StatusEventBus`` / ``status_event_bus`` — 事件总线类 + 全局单例
- ``create_agent_callbacks`` — Agent 回调桥接辅助
- ``PubSub`` / ``pubsub`` — 广播层类 + 全局单例
- ``register_ws_routes`` — WebSocket 路由注册

用法::

    from hiveweave.realtime import (
        status_event_bus,
        register_ws_routes,
    )
    from hiveweave.main import app

    # 注册 WebSocket 路由
    register_ws_routes(app)

    # 发布事件
    await status_event_bus.publish("lobby", {"type": "status", ...})
"""

from hiveweave.realtime.event_bus import (
    StatusEventBus,
    create_agent_callbacks,
    status_event_bus,
)
from hiveweave.realtime.pubsub import PubSub, pubsub
from hiveweave.realtime.channels import register_ws_routes

__all__ = [
    "StatusEventBus",
    "status_event_bus",
    "create_agent_callbacks",
    "PubSub",
    "pubsub",
    "register_ws_routes",
]
