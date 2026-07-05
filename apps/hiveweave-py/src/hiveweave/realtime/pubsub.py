"""PubSub — 广播层（进程内 + 可选 Redis）。

契约 12: PubSub 事件转发
- 默认进程内广播（通过 ``StatusEventBus``）
- 可选 Redis PubSub 后端（多实例部署，接口预留）
- 统一接口：``subscribe`` / ``unsubscribe`` / ``publish``

后端选择：
- ``InProcessBackend``（默认）— 委托给 ``StatusEventBus``，单实例部署
- ``RedisBackend``（预留）— 多实例部署，需要 ``redis.asyncio``

Redis 后端未实现 — 设置 ``HIVEWEAVE_REDIS_URL`` 启用（未来实现）。
当前所有操作走进程内后端，对调用方透明。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

import structlog

from hiveweave.realtime.event_bus import StatusEventBus, status_event_bus

log = structlog.get_logger(__name__)


# ── 后端协议 ────────────────────────────────────────────────


@runtime_checkable
class PubSubBackend(Protocol):
    """PubSub 后端协议 — 统一接口。"""

    async def subscribe(self, channel: str) -> asyncio.Queue[dict]:
        """订阅频道，返回 ``Queue``。"""
        ...

    async def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """取消订阅。"""
        ...

    async def publish(self, channel: str, message: dict) -> None:
        """发布消息到频道。"""
        ...


# ── 进程内后端 ──────────────────────────────────────────────


class InProcessBackend:
    """进程内 PubSub 后端 — 委托给 ``StatusEventBus``。

    默认后端，单实例部署足够。
    对应 Elixir ``Phoenix.PubSub`` 的进程内实现。
    """

    def __init__(self, bus: StatusEventBus | None = None) -> None:
        self._bus = bus or status_event_bus

    async def subscribe(self, channel: str) -> asyncio.Queue[dict]:
        """订阅频道 — 委托给 ``StatusEventBus.subscribe``。"""
        return await self._bus.subscribe(channel)

    async def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """取消订阅 — 委托给 ``StatusEventBus.unsubscribe``。"""
        await self._bus.unsubscribe(queue)

    async def publish(self, channel: str, message: dict) -> None:
        """发布消息 — 委托给 ``StatusEventBus.publish``。"""
        await self._bus.publish(channel, message)


# ── Redis 后端（预留）──────────────────────────────────────


class RedisBackend:
    """Redis PubSub 后端（预留 — 未实现）。

    多实例部署时使用。需要 ``redis.asyncio`` 包。
    设置 ``HIVEWEAVE_REDIS_URL`` 环境变量启用。

    未来实现要点：
    - ``subscribe``: ``redis.pubsub()`` + 订阅 channel + 启动 listener task
    - ``publish``: ``redis.publish(channel, json.dumps(message))``
    - listener task: 从 pubsub 监听 → put_nowait 到本地 queue
    - ``unsubscribe``: 取消 pubsub 订阅 + 取消 listener task
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Any = None
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        self._listener_tasks: dict[str, asyncio.Task] = {}
        raise NotImplementedError(
            "Redis PubSub backend not yet implemented. "
            "Use InProcessBackend for single-instance deployment. "
            "Set HIVEWEAVE_REDIS_URL to enable Redis in the future."
        )

    async def subscribe(self, channel: str) -> asyncio.Queue[dict]:
        """订阅频道 — 未实现。"""
        raise NotImplementedError

    async def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """取消订阅 — 未实现。"""
        raise NotImplementedError

    async def publish(self, channel: str, message: dict) -> None:
        """发布消息 — 未实现。"""
        raise NotImplementedError


# ── PubSub 统一入口 ─────────────────────────────────────────


class PubSub:
    """PubSub 广播层 — 统一接口，后端可切换。

    默认使用进程内后端（``InProcessBackend``）。
    未来可通过 ``HIVEWEAVE_REDIS_URL`` 环境变量切换到 Redis 后端。

    用法::

        from hiveweave.realtime.pubsub import pubsub

        # 订阅
        queue = await pubsub.subscribe("lobby")

        # 发布
        await pubsub.publish("lobby", {"type": "status", ...})

        # 取消订阅
        await pubsub.unsubscribe(queue)
    """

    def __init__(self, backend: PubSubBackend | None = None) -> None:
        if backend is not None:
            self._backend: PubSubBackend = backend
        else:
            # 默认进程内后端
            self._backend = InProcessBackend()
        log.info("pubsub_init", backend=type(self._backend).__name__)

    @property
    def backend(self) -> PubSubBackend:
        """当前后端实例。"""
        return self._backend

    async def subscribe(self, channel: str) -> asyncio.Queue[dict]:
        """订阅频道，返回 ``Queue`` 接收事件。

        Args:
            channel: 频道名（``"lobby"``, ``"chat"``, ``"agent:{id}"`` 等）

        Returns:
            ``asyncio.Queue`` — 订阅者从此 queue ``get()`` 事件
        """
        return await self._backend.subscribe(channel)

    async def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """取消订阅。

        Args:
            queue: ``subscribe`` 返回的 ``Queue``
        """
        await self._backend.unsubscribe(queue)

    async def publish(self, channel: str, message: dict) -> None:
        """发布消息到频道。

        Args:
            channel: 目标频道名
            message: 消息 dict
        """
        await self._backend.publish(channel, message)

    async def publish_json(self, channel: str, data: dict) -> None:
        """发布 JSON 消息（``publish`` 的 alias，语义一致）。"""
        await self._backend.publish(channel, data)


# ── 模块级单例 ──────────────────────────────────────────────

pubsub = PubSub()
"""全局 PubSub 实例。

默认使用进程内后端（``InProcessBackend`` → ``StatusEventBus``）。
所有 WebSocket channel 通过此实例进行广播。

对应 Elixir 的 ``Phoenix.PubSub`` 进程级注册名。
"""
