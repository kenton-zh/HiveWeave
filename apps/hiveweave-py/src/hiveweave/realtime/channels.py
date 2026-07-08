"""WebSocket channels — 3 channel 实现（契约 12）。

三个 WebSocket 端点：
1. ``/ws/lobby`` — 全局状态 + 活动流（agent 状态变更、系统暂停/恢复、新 agent 加入/离开）
2. ``/ws/agent/{agent_id}`` — 单 agent 流式对话（text_delta / tool_call / done / error）
3. ``/ws/chat`` — 聊天消息推送（用户消息、agent 回复、团队聊天）

设计要点：
- **认证条件强制**: ``HIVEWEAVE_API_KEY`` 未设→开放，已设→用 ``secrets.compare_digest`` 校验
- **连接管理**: WebSocket 断开时清理订阅（finally 块确保 unsubscribe）
- **错误处理**: WebSocket 异常不崩溃服务器（catch + log + close）
- **并发模型**: 每个连接启动两个 task（forward + receive），``FIRST_COMPLETED`` 退出
- **ping/pong**: lobby/agent 频道响应 ping（chat 频道也支持，便于前端统一）
- **init 快照**: lobby 连接发送 ``{agentIds, paused}``；agent 连接发送 agent 信息 + 历史 50 条

对应 Elixir: ``lobby_channel.ex`` + ``agent_channel.ex`` + ``project_channel.ex``
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Awaitable, Callable

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from hiveweave.config import settings
from hiveweave.realtime.event_bus import (
    StatusEventBus,
    create_agent_callbacks,
    status_event_bus,
)

log = structlog.get_logger(__name__)

# ── 常量（契约 12）──────────────────────────────────────────

JOIN_HISTORY_LIMIT = 50
"""join agent 频道时返回的历史消息数。契约 12: 50 条。"""

WS_CLOSE_UNAUTHORIZED = 4401
"""WebSocket 关闭码 — 未认证。"""

WS_CLOSE_NOT_FOUND = 4404
"""WebSocket 关闭码 — agent 未找到。"""

WS_CLOSE_TOO_MANY_CONNECTIONS = 4429
"""WebSocket 关闭码 — 连接数超限。"""

MAX_WS_CONNECTIONS = 50
"""最大并发 WebSocket 连接数。R10 fix: 防止大量连接耗尽内存（每连接双 task）。"""

# 全局活跃连接计数器（R10 fix）
_active_ws_connections: int = 0
_ws_conn_lock = asyncio.Lock()


# ── 认证 ────────────────────────────────────────────────────


def _authenticate_ws(websocket: WebSocket) -> bool:
    """条件认证 — ``HIVEWEAVE_API_KEY`` 未设则开放，已设则强制。

    契约 12: 认证条件强制
    - env 未设 → 跳过校验（开放模式）
    - env 已设 → 用 ``secrets.compare_digest`` 校验（防时序攻击）
      检查顺序: query param ``api_key`` → ``apiKey`` → ``Authorization: Bearer`` header

    Args:
        websocket: FastAPI WebSocket 实例

    Returns:
        True = 认证通过（或开放模式），False = 认证失败
    """
    expected = settings.api_key
    if not expected:
        # 开放模式 — 未设置 API key
        return True

    # 检查 query param
    provided = websocket.query_params.get("api_key") or websocket.query_params.get(
        "apiKey"
    )

    # 检查 Authorization header
    if not provided:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]

    if not provided:
        return False

    return secrets.compare_digest(provided, expected)


# ── WebSocket 会话辅助 ──────────────────────────────────────


async def _forward_loop(
    queue: asyncio.Queue[dict],
    send_fn: Callable[[dict], Awaitable[bool]],
) -> None:
    """从 bus queue 读取事件并转发给客户端。

    Args:
        queue: ``StatusEventBus.subscribe`` 返回的 Queue
        send_fn: 发送函数，返回 True=成功，False=连接已断开
    """
    while True:
        event = await queue.get()
        if not await send_fn(event):
            # 发送失败 — 连接已断开，退出循环
            break


async def _receive_loop(
    websocket: WebSocket,
    on_message: Callable[[dict], Awaitable[None]] | None,
    send_fn: Callable[[dict], Awaitable[bool]],
) -> None:
    """从客户端读取消息并处理。

    处理:
    - ``ping`` → 回复 ``{type: "pong", timestamp}``
    - 其他消息 → 调用 ``on_message`` 回调

    Args:
        websocket: FastAPI WebSocket 实例
        on_message: 自定义消息处理回调
        send_fn: 发送函数
    """
    while True:
        # 接收文本消息
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            break
        except Exception as e:
            log.debug("ws_receive_error", error=str(e))
            break

        # 解析 JSON
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await send_fn({"type": "error", "error": "Invalid JSON"})
            continue

        if not isinstance(msg, dict):
            continue

        # ping/pong（所有频道支持）
        msg_type = msg.get("type", "")
        if msg_type == "ping":
            await send_fn(
                {"type": "pong", "timestamp": int(time.time() * 1000)}
            )
            continue

        # 自定义消息处理
        if on_message is not None:
            try:
                await on_message(msg)
            except Exception as e:
                log.warning("ws_message_handler_error", error=str(e))


async def _run_ws_session(
    websocket: WebSocket,
    channel: str,
    *,
    on_message: Callable[[dict], Awaitable[None]] | None = None,
    init_events: list[dict] | None = None,
    bus: StatusEventBus | None = None,
    agent_id: str | None = None,
) -> None:
    """运行 WebSocket 会话 — 订阅 bus + 并发 forward/receive。

    流程:
    1. accept WebSocket
    2. 认证（失败 → send error + close）
    3. subscribe bus channel
    4. 发送 init 事件（快照）
    5. 并发运行 forward_loop + receive_loop
    6. 断开时 unsubscribe（finally）

    Args:
        websocket: FastAPI WebSocket
        channel: bus 频道名
        on_message: 客户端消息处理回调
        init_events: 连接后立即发送的初始事件列表
        bus: StatusEventBus 实例（默认全局单例）
        agent_id: 可选 — 同时订阅 agent 频道
    """
    bus = bus or status_event_bus

    # 1. Auth (在 accept 之前 — 拒绝未认证连接，不进入 accept 状态)
    if not _authenticate_ws(websocket):
        await websocket.close(
            code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized"
        )
        return

    # 1b. R10 fix: 连接数上限检查（accept 之前拒绝）
    global _active_ws_connections
    async with _ws_conn_lock:
        if _active_ws_connections >= MAX_WS_CONNECTIONS:
            await websocket.close(
                code=WS_CLOSE_TOO_MANY_CONNECTIONS,
                reason=f"Too many connections (max {MAX_WS_CONNECTIONS})",
            )
            log.warning(
                "ws_rejected_too_many",
                active=_active_ws_connections,
                limit=MAX_WS_CONNECTIONS,
            )
            return
        _active_ws_connections += 1
    log.debug("ws_conn_acquired", active=_active_ws_connections)

    # 2. Accept
    await websocket.accept()

    # 发送锁 — 避免 forward 和 receive 并发 send 导致交错
    send_lock = asyncio.Lock()

    async def safe_send(data: dict) -> bool:
        """线程安全发送 JSON。返回 True=成功，False=失败。"""
        try:
            async with send_lock:
                await websocket.send_json(data)
            return True
        except Exception as e:
            log.debug("ws_send_failed", error=str(e))
            return False

    # R3+R10 fix: subscribe 可能 raise RuntimeError（订阅者上限），
    # 放入 try 块以确保 finally 释放连接计数。
    queue: asyncio.Queue[dict] | None = None
    try:
        # 3. Subscribe
        queue = await bus.subscribe(channel, agent_id=agent_id)

        # 4. 发送 init 事件
        if init_events:
            for event in init_events:
                if not await safe_send(event):
                    # 发送失败 — 连接已断开
                    return

        # 5. 并发运行 forward + receive
        forward_task = asyncio.create_task(
            _forward_loop(queue, safe_send),
            name=f"ws-forward-{channel}",
        )
        receive_task = asyncio.create_task(
            _receive_loop(websocket, on_message, safe_send),
            name=f"ws-receive-{channel}",
        )

        done, pending = await asyncio.wait(
            {forward_task, receive_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 取消未完成的 task
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # 检查是否有异常
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                log.warning(
                    "ws_task_exception",
                    channel=channel,
                    error=str(exc),
                )

    except WebSocketDisconnect:
        log.info("ws_disconnected", channel=channel)
    except Exception as e:
        log.warning("ws_session_error", channel=channel, error=str(e))
    finally:
        # 6. 清理订阅
        if queue is not None:
            await bus.unsubscribe(queue)
        # R10 fix: 释放连接计数
        async with _ws_conn_lock:
            _active_ws_connections = max(0, _active_ws_connections - 1)
        log.debug("ws_conn_released", active=_active_ws_connections)
        log.debug("ws_session_closed", channel=channel)


# ── Agent 启动辅助 ──────────────────────────────────────────


async def _ensure_agent_running(agent_id: str, config: dict) -> None:
    """确保 agent 正在运行（契约 12: ensure_project_booted）。

    契约 12: join agent:<id> 时按需 ensure_project_booted
    - 如果 agent 未在 agent_manager 中注册，启动它
    - 这是崩溃恢复机制 — 后端重启后前端打开 agent 面板即拉起项目
    - 安全：agent 不会自动开始干活，仅 GenServer 就绪等待 chat

    Args:
        agent_id: Agent ID
        config: agent 配置 dict（来自 Meta DB）
    """
    from hiveweave.agents.supervisor import agent_manager

    # 已运行 → no-op
    if agent_manager.get_agent(agent_id) is not None:
        return

    project_id = config.get("project_id", "")
    on_status, on_stream = create_agent_callbacks(agent_id, project_id)

    try:
        await agent_manager.start_agent(
            agent_id,
            project_id,
            config,
            on_status_change=on_status,
            on_stream_event=on_stream,
        )
        log.info("ws_ensure_agent_started", agent_id=agent_id, project_id=project_id)
    except Exception as e:
        log.warning(
            "ws_ensure_agent_failed",
            agent_id=agent_id,
            error=str(e),
        )


# ── WebSocket 端点 ──────────────────────────────────────────


async def lobby_ws(websocket: WebSocket) -> None:
    """lobby channel — 全局状态 + 活动流。

    契约 12: ``/ws/lobby``
    - 广播 agent 状态变更（processing/idle）
    - 新 agent 加入/离开
    - 系统暂停/恢复通知
    - 客户端连接时发送当前所有 processing agents 快照
    - 支持 ping/pong 心跳

    事件流向:
    - server → client: status / system_paused / system_resumed / agent_created /
      agent_dismissed / activity / org_changed
    - client → server: ping
    """
    from hiveweave.services.system_state import system_state

    # 构建初始快照
    init_events: list[dict] = [
        {
            "type": "init",
            "agentIds": status_event_bus.get_all_processing(),
            "paused": system_state.paused(),
        }
    ]

    # 添加最近活动 replay（如果有）
    recent = status_event_bus.get_recent_activity()
    if recent:
        init_events.append(
            {
                "type": "activity_replay",
                "events": recent,
            }
        )

    await _run_ws_session(
        websocket,
        channel="lobby",
        init_events=init_events,
    )


async def agent_ws(websocket: WebSocket, agent_id: str) -> None:
    """agent channel — 单 agent 流式对话。

    契约 12: ``/ws/agent/{agent_id}``
    - 流式对话事件: start / text_delta / thinking_delta /
      tool_call_start / tool_call_end / done / error
    - agent 状态变更
    - 只订阅单个 agent 的事件
    - join 时返回 agent 信息 + 最近 50 条历史
    - join 时按需 ensure_project_booted（崩溃恢复）
    - 支持 chat / cancel / ping

    事件流向:
    - server → client: stream_chunk / stream_tool / done / error /
      status_change / message_id
    - client → server: chat / cancel / ping

    Args:
        websocket: FastAPI WebSocket
        agent_id: Agent UUID（路径参数）
    """
    # 认证检查（在任意 DB/agent 操作之前 — 防止未认证请求触发 ensure_project_booted）
    if not _authenticate_ws(websocket):
        await websocket.close(
            code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized"
        )
        return

    from hiveweave.db import meta as meta_db
    from hiveweave.services.chat_message import ChatMessageService

    # 查找 agent
    agent_config = await meta_db.get_agent_by_id(agent_id)
    if agent_config is None:
        await websocket.accept()
        try:
            await websocket.send_json(
                {"type": "error", "agentId": agent_id, "error": "agent_not_found"}
            )
        except Exception:
            pass
        await websocket.close(code=WS_CLOSE_NOT_FOUND)
        return

    # ensure_project_booted — 崩溃恢复机制
    await _ensure_agent_running(agent_id, agent_config)

    # 获取历史消息（50 条）
    chat_service = ChatMessageService()
    try:
        history = await chat_service.get_messages(agent_id, limit=JOIN_HISTORY_LIMIT)
    except Exception as e:
        log.warning("ws_agent_history_failed", agent_id=agent_id, error=str(e))
        history = []

    # 获取待处理 inbox 消息
    from hiveweave.services.inbox import InboxService
    inbox_service = InboxService()
    try:
        pending_inbox = await inbox_service.get_pending_messages(agent_id)
    except Exception as e:
        log.warning("ws_agent_inbox_failed", agent_id=agent_id, error=str(e))
        pending_inbox = []

    # 构建初始快照
    init_events: list[dict] = [
        {
            "type": "init",
            "agentId": agent_id,
            "name": agent_config.get("name", ""),
            "role": agent_config.get("role", ""),
            "history": history,
            "inbox": pending_inbox,
        }
    ]

    # 消息处理回调
    async def on_message(msg: dict) -> None:
        """处理客户端消息: chat / cancel。"""
        msg_type = msg.get("type", "")

        if msg_type == "chat":
            message = msg.get("message", "")
            images = msg.get("images")

            from hiveweave.agents.supervisor import agent_manager

            agent = agent_manager.get_agent(agent_id)
            if agent is None:
                await _safe_send_agent_error(
                    websocket, agent_id, "Agent not running"
                )
                return

            # 调用 agent.chat
            result = await agent.chat(message)

            if result.get("error") == "busy":
                await _safe_send_agent_error(
                    websocket, agent_id, "Agent is busy"
                )
            elif result.get("error") == "paused":
                await _safe_send_agent_error(
                    websocket, agent_id, "System is paused"
                )
            # ok → 流式事件由 bus 自动推送

        elif msg_type == "cancel":
            from hiveweave.agents.supervisor import agent_manager

            agent = agent_manager.get_agent(agent_id)
            if agent is not None:
                try:
                    await agent.cancel()
                except Exception as e:
                    log.warning(
                        "ws_agent_cancel_failed",
                        agent_id=agent_id,
                        error=str(e),
                    )

    await _run_ws_session(
        websocket,
        channel=f"agent:{agent_id}",
        on_message=on_message,
        init_events=init_events,
    )


async def chat_ws(websocket: WebSocket) -> None:
    """chat channel — 聊天消息推送。

    契约 12: ``/ws/chat``
    - 用户聊天消息推送
    - agent 回复消息推送
    - 团队聊天消息
    - 支持 ping/pong 心跳

    事件流向:
    - server → client: chat_message（用户消息/agent 回复/团队聊天）
    - client → server: chat（发送消息给 agent）/ team_chat / ping

    客户端消息格式::

        {"type": "chat", "agent_id": "...", "message": "..."}
        {"type": "team_chat", "team_id": "...", "message": "..."}
    """
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.realtime.event_bus import status_event_bus as _bus

    async def on_message(msg: dict) -> None:
        """处理客户端消息: chat / team_chat。"""
        msg_type = msg.get("type", "")

        if msg_type == "chat":
            target_agent_id = msg.get("agent_id", "")
            message = msg.get("message", "")

            if not target_agent_id:
                await _safe_send_chat_error(websocket, "agent_id required")
                return

            # 保存用户消息到 chat_messages
            from hiveweave.services.chat_message import ChatMessageService

            chat_service = ChatMessageService()
            try:
                saved = await chat_service.save_message(
                    {
                        "agent_id": target_agent_id,
                        "role": "user",
                        "content": message,
                        "is_streaming": False,
                    }
                )
                # 推送 message_id 给客户端
                await _safe_send_json(websocket, {
                    "type": "message_id",
                    "agentId": target_agent_id,
                    "role": "user",
                    "id": saved["id"],
                })
            except Exception as e:
                log.warning(
                    "ws_chat_save_failed",
                    agent_id=target_agent_id,
                    error=str(e),
                )

            # 推送到 chat 频道
            await _bus.publish_chat_message(
                target_agent_id,
                {"role": "user", "content": message},
            )

            # 调用 agent.chat
            agent = agent_manager.get_agent(target_agent_id)
            if agent is None:
                await _safe_send_agent_error(
                    websocket, target_agent_id, "Agent not running"
                )
                return

            result = await agent.chat(message)

            if result.get("error") == "busy":
                await _safe_send_agent_error(
                    websocket, target_agent_id, "Agent is busy"
                )
            elif result.get("error") == "paused":
                await _safe_send_agent_error(
                    websocket, target_agent_id, "System is paused"
                )
            # ok → 流式事件由 bus 自动推送到 agent 频道

        elif msg_type == "team_chat":
            # TODO: 契约 12 — 团队聊天消息推送
            # 当前仅广播到 chat 频道
            team_id = msg.get("team_id", "")
            message = msg.get("message", "")
            await _bus.publish(
                "chat",
                {
                    "type": "team_chat",
                    "team_id": team_id,
                    "message": message,
                },
            )

    await _run_ws_session(
        websocket,
        channel="chat",
        on_message=on_message,
    )


# ── 安全发送辅助 ────────────────────────────────────────────


async def _safe_send_json(websocket: WebSocket, data: dict) -> bool:
    """安全发送 JSON — 异常不抛出。"""
    try:
        await websocket.send_json(data)
        return True
    except Exception:
        return False


async def _safe_send_agent_error(
    websocket: WebSocket, agent_id: str, error: str
) -> None:
    """发送 agent 错误事件。"""
    await _safe_send_json(
        websocket,
        {"type": "error", "agentId": agent_id, "error": error},
    )


async def _safe_send_chat_error(websocket: WebSocket, error: str) -> None:
    """发送 chat 错误事件。"""
    await _safe_send_json(
        websocket,
        {"type": "error", "error": error},
    )


# ── 路由注册 ────────────────────────────────────────────────


def register_ws_routes(app: FastAPI) -> None:
    """注册 WebSocket 路由到 FastAPI app。

    契约 12: 3 个 WebSocket channel
    - ``/ws/lobby`` — 全局状态 + 活动流
    - ``/ws/agent/{agent_id}`` — 单 agent 流式对话
    - ``/ws/chat`` — 聊天消息推送

    用法::

        from hiveweave.realtime.channels import register_ws_routes
        from hiveweave.main import app

        register_ws_routes(app)

    Args:
        app: FastAPI 应用实例
    """
    app.websocket("/ws/lobby")(lobby_ws)
    app.websocket("/ws/agent/{agent_id}")(agent_ws)
    app.websocket("/ws/chat")(chat_ws)

    log.info(
        "ws_routes_registered",
        routes=["/ws/lobby", "/ws/agent/{agent_id}", "/ws/chat"],
    )
