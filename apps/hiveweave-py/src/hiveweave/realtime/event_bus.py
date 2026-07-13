"""StatusEventBus — 进程内 pub/sub 事件总线（契约 12）。

核心功能：
- 频道订阅/取消订阅（返回 asyncio.Queue）
- 事件发布（广播到所有匹配频道的订阅者，set 去重避免重复投递）
- agent 处理状态跟踪（processing agents 集合，lobby 连接时发送快照）
- 最近活动缓冲（100 条，契约 12: recentActivity 缓冲区）

不依赖 Redis — 单实例部署足够。Redis PubSub 接口在 pubsub.py 预留。

频道命名约定：
- ``"lobby"`` — 全局 lobby 频道（状态变更 + 活动流）
- ``"agent:{agent_id}"`` — 单 agent 频道（流式 token + 状态变更）
- ``"chat"`` — 聊天频道（用户/agent 聊天消息推送）
- ``"project:{project_id}"`` — 项目频道（可选，game_time / agent_hired / dispatch）

事件转发规则（契约 12: PubSub 事件转发）：
- text_delta / thinking_delta / start → 仅 agent 频道
- tool_call_start / tool_call_end / done / error → agent + lobby 频道
- status_change → lobby + agent + project 频道
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

# ── 常量（契约 12）──────────────────────────────────────────

RECENT_ACTIVITY_BUFFER = 100
"""最近活动缓冲区大小。对齐 Elixir recentActivity 缓冲 100 条。"""

AGENT_REPLAY_BUFFER = 50
"""单 agent 事件重放缓冲区大小。参考 DeepTutor StreamBus replay 模式：
新订阅者加入 agent 频道时，立即重放缓冲的最近事件，避免因 WebSocket
join 延迟而丢失 stream_chunk / tool_call 等关键事件。"""

MAX_SUBSCRIBERS = 100
"""最大订阅者总数（跨所有频道）。R3 fix: 防止恶意客户端创建大量订阅耗尽内存。"""

# 流事件中仅发往 agent 频道（不转发到 lobby）的类型。
# 契约 12: text_delta / thinking_delta 不转发到 lobby（避免重复渲染）。
# start 同理 — 流式生命周期事件，仅 agent 频道关心。
_DELTA_ONLY_TYPES: frozenset[str] = frozenset(
    {"text_delta", "thinking_delta", "start", "thinking"}
)


# ── 回调类型 ────────────────────────────────────────────────

StatusCallback = Callable[[str, str, dict], Awaitable[None] | None]
"""状态变更回调: (agent_id, status, extra) → None。对齐 agents/agent.py。"""

StreamEventCallback = Callable[[str, dict], Awaitable[None] | None]
"""流事件回调: (agent_id, event) → None。对齐 agents/agent.py。"""


class StatusEventBus:
    """进程内事件总线 — agent 状态变更 + 流事件广播。

    契约 12: StatusEventBus
    - 进程内 pub/sub，不依赖 Redis
    - ``subscribe`` 返回 ``asyncio.Queue``，订阅者从 queue 消费事件
    - ``publish`` 将事件广播到所有匹配频道的订阅者（set 去重）
    - 维护 processing agents 集合（lobby 连接时发送快照）
    - 维护最近 100 条活动事件（lobby 重连 replay）

    用法::

        from hiveweave.realtime.event_bus import status_event_bus

        queue = await status_event_bus.subscribe("lobby")
        await status_event_bus.publish("lobby", {"type": "status", ...})
        event = await queue.get()
        await status_event_bus.unsubscribe(queue)
    """

    def __init__(self) -> None:
        # channel → set of subscriber queues
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        # queue → set of channels (for unsubscribe lookup)
        self._queue_channels: dict[asyncio.Queue[dict], set[str]] = {}
        # processing agent IDs
        self._processing: set[str] = set()
        # recent activity events (deque maxlen=100)
        self._recent_activity: deque[dict] = deque(maxlen=RECENT_ACTIVITY_BUFFER)
        # per-agent replay buffers (参考 DeepTutor StreamBus replay)
        self._agent_buffers: dict[str, deque[dict]] = {}
        # lock for subscriber management
        self._lock = asyncio.Lock()

    # ── 订阅 / 取消订阅 ──────────────────────────────────────

    async def subscribe(
        self,
        channel: str,
        agent_id: str | None = None,
    ) -> asyncio.Queue[dict]:
        """订阅频道，返回 ``Queue`` 接收事件。

        Args:
            channel: 频道名（``"lobby"``, ``"chat"``, ``"agent:{id}"``, ``"project:{id}"``）
            agent_id: 可选 — 如果提供，同时订阅 ``agent:{agent_id}`` 频道。
                用于 agent 频道需要同时接收 lobby 广播的场景。

        Returns:
            ``asyncio.Queue`` — 订阅者从此 queue ``get()`` 事件。
            事件为 dict，必须含 ``"type"`` 字段。

        Raises:
            RuntimeError: 全局订阅者数超过 ``MAX_SUBSCRIBERS`` (R3 fix)。
        """
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        channels = {channel}
        if agent_id:
            channels.add(f"agent:{agent_id}")

        async with self._lock:
            # R3 fix: 全局订阅者上限，防止恶意客户端耗尽内存
            if len(self._queue_channels) >= MAX_SUBSCRIBERS:
                log.warning(
                    "bus_subscribe_rejected",
                    channel=channel,
                    agent_id=agent_id,
                    current=len(self._queue_channels),
                    limit=MAX_SUBSCRIBERS,
                )
                raise RuntimeError(
                    f"Subscriber limit reached ({MAX_SUBSCRIBERS})"
                )
            for ch in channels:
                self._subscribers.setdefault(ch, set()).add(queue)
            self._queue_channels[queue] = channels

        log.debug(
            "bus_subscribe",
            channel=channel,
            agent_id=agent_id,
            channels=list(channels),
        )
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """取消订阅（从所有频道移除该 queue）。

        安全调用 — queue 不存在时 no-op。
        """
        async with self._lock:
            channels = self._queue_channels.pop(queue, set())
            for ch in channels:
                subs = self._subscribers.get(ch)
                if subs:
                    subs.discard(queue)
                    if not subs:
                        del self._subscribers[ch]

        if channels:
            log.debug("bus_unsubscribe", channels=list(channels))

    # ── 发布 ─────────────────────────────────────────────────

    async def publish(
        self,
        channel: str,
        event: dict,
        agent_id: str | None = None,
    ) -> None:
        """发布事件到频道。

        Args:
            channel: 目标频道名
            event: 事件 dict（自动补 ``timestamp`` 字段）
            agent_id: 可选 — 如果提供，同时发布到 ``agent:{agent_id}`` 频道。
                使用 set 去重，订阅了两个频道的 queue 只收到一份。
        """
        # 自动补 timestamp（如果未提供）
        if "timestamp" not in event:
            event = {**event, "timestamp": int(time.time() * 1000)}

        # 收集目标 queue（set 去重）
        async with self._lock:
            targets: set[asyncio.Queue[dict]] = set()
            subs = self._subscribers.get(channel)
            if subs:
                targets.update(subs)
            if agent_id:
                agent_channel = f"agent:{agent_id}"
                agent_subs = self._subscribers.get(agent_channel)
                if agent_subs:
                    targets.update(agent_subs)

        # 非阻塞投递（lock 外执行，避免阻塞其他 publish）
        for q in targets:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 队列满 — 丢弃最旧事件，推入最新（避免慢消费者阻塞总线）
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass
                log.warning("bus_queue_full_dropped_oldest", channel=channel)

    # ── 状态跟踪 ─────────────────────────────────────────────

    def set_processing(self, agent_id: str, value: bool) -> None:
        """设置 agent 处理状态。

        契约 12: ``set_processing(agent_id, value)`` — lobby 连接时发送快照。
        """
        if value:
            self._processing.add(agent_id)
        else:
            self._processing.discard(agent_id)

    def is_processing(self, agent_id: str) -> bool:
        """查询 agent 是否处理中。"""
        return agent_id in self._processing

    def get_all_processing(self) -> list[str]:
        """所有处理中的 agent ID（lobby init 快照用）。"""
        return list(self._processing)

    # ── 活动事件 ─────────────────────────────────────────────

    def emit_activity(self, event: dict) -> None:
        """发布活动事件并缓冲（最近 100 条）。

        契约 12: ``emit_activity(event)`` — ``get_recent_activity()`` 用于 lobby replay。
        """
        if "timestamp" not in event:
            event = {**event, "timestamp": int(time.time() * 1000)}
        self._recent_activity.append(event)

    def get_recent_activity(self) -> list[dict]:
        """最近 100 条活动事件（正序，最旧在前）。"""
        return list(self._recent_activity)

    # ── 高层发布辅助 ─────────────────────────────────────────

    async def publish_status_change(
        self,
        agent_id: str,
        status: str,
        project_id: str | None = None,
    ) -> None:
        """发布 agent 状态变更到 lobby + agent + project 频道。

        契约 12: 三频道均推送 status_change（不止 lobby）。
        同时更新 processing 集合。

        Args:
            agent_id: Agent ID
            status: ``"processing"`` 或 ``"idle"``
            project_id: 可选 — 如果提供，同时推送到 ``project:{project_id}``
        """
        self.set_processing(agent_id, status == "processing")
        event: dict[str, Any] = {
            "type": "status",
            "agentId": agent_id,
            "status": status,
            "project_id": project_id,
        }
        # 推送到 lobby
        await self.publish("lobby", event)
        # 推送到 agent 频道
        await self.publish(f"agent:{agent_id}", event)
        # 推送到 project 频道
        if project_id:
            await self.publish(f"project:{project_id}", event)

    async def publish_stream_event(self, agent_id: str, event: dict) -> None:
        """发布流事件，按契约 12 转发规则分发。

        转发规则：
        - text_delta / thinking_delta / start → 仅 ``agent:{agent_id}`` 频道
        - tool_call_start / tool_call_end / done / error → agent + lobby 频道

        同时缓冲到 recent_activity（lobby replay 用）和 per-agent replay
        buffer（参考 DeepTutor StreamBus replay 模式）。

        Args:
            agent_id: Agent ID
            event: 流事件 dict，必须含 ``"type"`` 字段
        """
        event_type = event.get("type", "")
        # 确保 agentId
        if "agentId" not in event:
            event = {**event, "agentId": agent_id}

        if event_type in _DELTA_ONLY_TYPES:
            # 仅 agent 频道
            await self.publish(f"agent:{agent_id}", event)
        else:
            # agent + lobby 频道（通过 agent_id 参数，set 去重）
            await self.publish("lobby", event, agent_id=agent_id)

        # 缓冲活动事件
        self.emit_activity(event)

        # Per-agent replay buffer（参考 DeepTutor StreamBus.subscribe() replay）
        buf = self._agent_buffers.get(agent_id)
        if buf is None:
            buf = deque(maxlen=AGENT_REPLAY_BUFFER)
            self._agent_buffers[agent_id] = buf
        buf.append(event)

    def get_agent_replay(self, agent_id: str) -> list[dict]:
        """获取 agent 的缓冲事件用于重放，并清空缓冲区。

        参考 DeepTutor StreamBus: subscribe() 立即重放已有事件给新订阅者。

        Args:
            agent_id: Agent ID

        Returns:
            缓冲的事件列表（按时间顺序）；如果 agent 无缓冲，返回空列表。
        """
        buf = self._agent_buffers.pop(agent_id, None)
        if buf is None:
            return []
        return list(buf)

    async def publish_system_paused(self) -> None:
        """发布系统暂停通知到 lobby。"""
        await self.publish("lobby", {"type": "system_paused"})

    async def publish_system_resumed(self) -> None:
        """发布系统恢复通知到 lobby。"""
        await self.publish("lobby", {"type": "system_resumed"})

    async def publish_goals_updated(self, project_id: str) -> None:
        """发布企业目标更新通知到 lobby + project 频道。

        前端 GoalsPanel 监听此事件重新拉取 goals，实现 agent 更新后实时刷新。
        """
        event = {"type": "goals_updated", "projectId": project_id}
        await self.publish("lobby", event)
        await self.publish(f"project:{project_id}", event)

    async def publish_agent_created(
        self, agent_id: str, role: str, name: str | None = None
    ) -> None:
        """发布新 agent 加入通知到 lobby。"""
        event: dict[str, Any] = {
            "type": "agent_created",
            "agentId": agent_id,
            "role": role,
        }
        if name:
            event["name"] = name
        await self.publish("lobby", event)

    async def publish_agent_dismissed(self, agent_id: str) -> None:
        """发布 agent 离开通知到 lobby。"""
        await self.publish("lobby", {"type": "agent_dismissed", "agentId": agent_id})

    async def publish_org_changed(self) -> None:
        """发布组织架构变更通知到 lobby — 前端 org tree 监听此事件刷新。"""
        await self.publish("lobby", {"type": "org_changed"})

    async def publish_question_asked(
        self,
        agent_id: str,
        project_id: str,
        question_id: str,
        question: str,
        options: list | None = None,
    ) -> None:
        """发布 question 事件到 lobby + project 频道。

        前端 QuestionDialog 监听此事件后立即拉取 pending questions，
        无需等待 5s 轮询。
        """
        event: dict[str, Any] = {
            "type": "question_asked",
            "agentId": agent_id,
            "projectId": project_id,
            "questionId": question_id,
            "question": question[:200],
        }
        if options:
            event["options"] = options
        await self.publish("lobby", event)
        await self.publish(f"project:{project_id}", event)

    async def publish_chat_message(
        self, agent_id: str, message: dict
    ) -> None:
        """发布聊天消息到 chat 频道。

        Args:
            agent_id: Agent ID
            message: 消息 dict（role, content 等）
        """
        event = {
            "type": "chat_message",
            "agentId": agent_id,
            **message,
        }
        await self.publish("chat", event)

    # ── 调试 ─────────────────────────────────────────────────

    def get_subscriber_count(self, channel: str) -> int:
        """获取频道订阅者数量（调试/监控用）。"""
        return len(self._subscribers.get(channel, set()))

    def get_all_channels(self) -> list[str]:
        """获取所有活跃频道名（调试用）。"""
        return list(self._subscribers.keys())


# ── 模块级单例 ──────────────────────────────────────────────

status_event_bus = StatusEventBus()
"""全局 StatusEventBus 实例。

所有 WebSocket channel 共享此实例进行 pub/sub。
对应 Elixir 的 ``Phoenix.PubSub`` 进程级注册名。
"""


# ── Agent 回调桥接 ──────────────────────────────────────────


def create_agent_callbacks(
    agent_id: str,
    project_id: str | None = None,
    bus: StatusEventBus | None = None,
) -> tuple[StatusCallback, StreamEventCallback]:
    """创建 Agent 回调，桥接到 ``StatusEventBus``。

    用于将 ``Agent`` 的 ``on_status_change`` / ``on_stream_event`` 回调
    连接到 WebSocket 广播。

    用法::

        from hiveweave.realtime.event_bus import create_agent_callbacks

        on_status, on_stream = create_agent_callbacks(agent_id, project_id)
        agent = await agent_manager.start_agent(
            agent_id, project_id, config,
            on_status_change=on_status,
            on_stream_event=on_stream,
        )

    Args:
        agent_id: Agent ID
        project_id: 可选 — 用于 status_change 推送到 project 频道
        bus: 可选 — 自定义 StatusEventBus 实例（默认用全局单例）

    Returns:
        ``(on_status_change, on_stream_event)`` 回调元组
    """
    bus = bus or status_event_bus

    async def on_status_change(aid: str, status: str, extra: dict) -> None:
        """状态变更回调 — 发布到 lobby + agent + project 频道。"""
        pid = extra.get("project_id") or project_id
        await bus.publish_status_change(aid, status, pid)

    async def on_stream_event(aid: str, event: dict) -> None:
        """流事件回调 — 按转发规则发布。"""
        if "agentId" not in event:
            event = {**event, "agentId": aid}
        await bus.publish_stream_event(aid, event)

    return on_status_change, on_stream_event
