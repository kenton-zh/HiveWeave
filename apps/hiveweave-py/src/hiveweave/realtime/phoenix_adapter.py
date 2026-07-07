"""Phoenix Channel 协议适配器 — 兼容前端 phoenix.js WebSocket 客户端。

前端使用 ``phoenix`` npm 包的 ``Socket`` / ``Channel`` 连接后端：
    new Socket("ws://localhost:4000/socket", {params: {api_key}})
    socket.connect()
    channel = socket.channel("lobby:status")
    channel.join().receive("ok", ...)

Phoenix Channel 协议（vsn=2.0.0）消息格式为 5-element JSON array:
    [join_ref, ref, topic, event, payload]

本模块在 ``/socket/websocket`` 端点实现该协议，将 Phoenix 消息路由到
内部的 ``StatusEventBus`` pub/sub，并做事件名/payload 映射。

支持的 channel topic:
- ``lobby:status`` — 全局状态 + 活动流（对应 event_bus "lobby" 频道）
- ``agent:<id>`` — 单 agent 流式对话（对应 event_bus "agent:<id>" 频道）

事件名映射（后端 → 前端）:
- ``status`` → ``status_change``
- ``text_delta`` → ``stream_chunk`` (delta=true)
- ``thinking_delta`` → ``stream_chunk`` (delta=true, reasoning=true)
- ``tool_call_start`` → ``stream_tool`` (type="tool_use")
- ``tool_call_end`` → ``stream_tool`` (type="tool_result")
- ``agent_created`` / ``agent_dismissed`` → ``org_changed``
- ``done`` / ``error`` → 直接透传
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from hiveweave.config import settings
from hiveweave.realtime.event_bus import StatusEventBus, status_event_bus

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_TOO_MANY = 4429

MAX_PHOENIX_CONNECTIONS = 50

JOIN_HISTORY_LIMIT = 50

# ── 认证 ────────────────────────────────────────────────────


def _authenticate_ws(websocket: WebSocket) -> bool:
    """条件认证 — 与 channels.py 相同逻辑。"""
    expected = settings.api_key
    if not expected:
        return True
    provided = websocket.query_params.get("api_key") or websocket.query_params.get(
        "apiKey"
    )
    if not provided:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


# ── 事件名映射 ──────────────────────────────────────────────

# 后端 event type → 前端 channel event name
# 未在表中的 type 直接用原 type 作为 event name
_EVENT_NAME_MAP: dict[str, str] = {
    "status": "status_change",
    "text_delta": "stream_chunk",
    "thinking_delta": "stream_chunk",
    "tool_call_start": "stream_tool",
    "tool_call_end": "stream_tool",
    "agent_created": "org_changed",
    "agent_dismissed": "org_changed",
    "system_paused": "system_paused",
    "system_resumed": "system_resumed",
}


def _map_event(event: dict) -> tuple[str, dict]:
    """将后端事件映射为前端期望的 (event_name, payload)。

    Returns:
        (event_name, payload) — event_name 是 Phoenix channel event，
        payload 是发送给前端的数据。
    """
    raw_type = event.get("type", "")
    event_name = _EVENT_NAME_MAP.get(raw_type, raw_type)

    # stream_chunk 需要特殊处理 payload
    if raw_type in ("text_delta", "thinking_delta"):
        payload = {
            "text": event.get("content", ""),
            "delta": True,
            "deltaId": event.get("delta_id", event.get("deltaId", "")),
            "seq": event.get("seq"),
            "reasoning": raw_type == "thinking_delta",
        }
        # 移除 None 值
        payload = {k: v for k, v in payload.items() if v is not None}
        return event_name, payload

    # stream_tool 需要特殊处理
    if raw_type == "tool_call_start":
        payload = {
            "type": "tool_use",
            "toolName": event.get("tool_name", ""),
            "tool_name": event.get("tool_name", ""),
            "arguments": event.get("arguments", ""),
            "tool_call_id": event.get("tool_call_id", ""),
            "toolCallId": event.get("tool_call_id", ""),
        }
        return event_name, payload

    if raw_type == "tool_call_end":
        payload = {
            "type": "tool_result",
            "toolName": event.get("tool_name", ""),
            "tool_name": event.get("tool_name", ""),
            "tool_call_id": event.get("tool_call_id", ""),
            "toolCallId": event.get("tool_call_id", ""),
            "success": event.get("success", False),
        }
        return event_name, payload

    # status_change 需要转换 processing 字段
    if raw_type == "status":
        payload = {
            "agentId": event.get("agentId", ""),
            "processing": event.get("status") == "processing",
            "status": event.get("status", ""),
        }
        return event_name, payload

    # activity 事件直接透传
    if raw_type == "activity":
        return event_name, event

    # done / error / org_changed / system_paused / resumed 等 — 直接透传
    return event_name, event


# ── Channel Session ─────────────────────────────────────────


@dataclass
class ChannelSession:
    """单个 joined channel 的会话状态。"""

    topic: str
    join_ref: str
    queue: asyncio.Queue[dict] | None = None
    forward_task: asyncio.Task | None = None


# ── Phoenix Socket 端点 ─────────────────────────────────────

# 全局连接计数
_active_connections: int = 0
_conn_lock = asyncio.Lock()


async def phoenix_socket_ws(websocket: WebSocket) -> None:
    """Phoenix Channel 协议 WebSocket 端点。

    路由: ``/socket/websocket`` （前端 phoenix.js 自动追加 ``?vsn=2.0.0``）

    处理流程:
    1. 认证 + 连接数限制
    2. accept WebSocket
    3. 循环接收 Phoenix 消息: ``[join_ref, ref, topic, event, payload]``
    4. 路由到 phx_join / phx_leave / chat / cancel / heartbeat
    5. 并发 forward bus 事件到客户端（映射事件名 + Phoenix 格式封装）
    6. 断开时清理所有 channel 订阅
    """
    global _active_connections

    # 认证（accept 之前）
    if not _authenticate_ws(websocket):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
        return

    # 连接数限制
    async with _conn_lock:
        if _active_connections >= MAX_PHOENIX_CONNECTIONS:
            await websocket.close(code=WS_CLOSE_TOO_MANY, reason="Too many connections")
            return
        _active_connections += 1

    await websocket.accept()

    # 发送锁 — 避免 forward 和 receive 并发 send
    send_lock = asyncio.Lock()

    async def safe_send(data: Any) -> bool:
        try:
            async with send_lock:
                await websocket.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False

    # joined channels: topic → ChannelSession
    joined: dict[str, ChannelSession] = {}

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not isinstance(msg, list) or len(msg) != 5:
                continue

            join_ref, ref, topic, event, payload = msg

            # Heartbeat
            if topic == "phoenix" and event == "heartbeat":
                await safe_send(
                    [join_ref, ref, topic, "phx_reply",
                     {"status": "ok", "response": {}}]
                )
                continue

            # Join
            if event == "phx_join":
                await _handle_phoenix_join(
                    topic, join_ref, ref, payload or {},
                    joined, safe_send,
                )
                continue

            # Leave
            if event == "phx_leave":
                await _handle_phoenix_leave(topic, join_ref, ref, joined, safe_send)
                continue

            # Push events (chat, cancel, etc.)
            await _handle_phoenix_push(topic, event, payload or {}, joined, safe_send)

    except WebSocketDisconnect:
        log.debug("phoenix_ws_disconnected")
    except Exception as e:
        log.warning("phoenix_ws_error", error=str(e))
    finally:
        # 清理所有 joined channels
        for session in joined.values():
            if session.queue is not None:
                await status_event_bus.unsubscribe(session.queue)
            if session.forward_task is not None:
                session.forward_task.cancel()
                try:
                    await session.forward_task
                except (asyncio.CancelledError, Exception):
                    pass

        async with _conn_lock:
            _active_connections = max(0, _active_connections - 1)
        log.debug("phoenix_ws_closed", channels=len(joined))


# ── Join / Leave 处理 ───────────────────────────────────────


async def _handle_phoenix_join(
    topic: str,
    join_ref: str,
    ref: str,
    payload: dict,
    joined: dict[str, ChannelSession],
    send_fn: Any,
) -> None:
    """处理 phx_join 事件。"""
    # 已经 joined → 回复 ok（幂等）
    if topic in joined:
        await send_fn(
            [join_ref, ref, topic, "phx_reply", {"status": "ok", "response": {}}]
        )
        return

    session = ChannelSession(topic=topic, join_ref=join_ref)

    # 判断 channel 类型
    if topic.startswith("lobby:"):
        # lobby channel — 全局状态
        bus_channel = "lobby"
        session.queue = await status_event_bus.subscribe(bus_channel)

        # 发送 init 事件
        from hiveweave.services.system_state import system_state

        init_payload = {
            "agentIds": status_event_bus.get_all_processing(),
            "paused": system_state.paused(),
        }
        await send_fn(
            [join_ref, join_ref, topic, "init", init_payload]
        )

        # 发送 activity replay
        recent = status_event_bus.get_recent_activity()
        if recent:
            await send_fn(
                [join_ref, join_ref, topic, "activity_replay", {"events": recent}]
            )

    elif topic.startswith("agent:"):
        # agent channel — 单 agent 流式
        agent_id = topic.split(":", 1)[1]

        # 查找 agent 配置
        from hiveweave.db import meta as meta_db

        agent_config = await meta_db.get_agent_by_id(agent_id)
        if agent_config is None:
            # agent 不存在 → 回复 error
            await send_fn(
                [join_ref, ref, topic, "phx_reply",
                 {"status": "error", "response": {"reason": "agent_not_found"}}]
            )
            return

        # ensure agent running（崩溃恢复）
        await _ensure_agent_running(agent_id, agent_config)

        # 订阅 agent 频道
        session.queue = await status_event_bus.subscribe(f"agent:{agent_id}")

        # BUG-032: 事件重放（参考 DeepTutor StreamBus replay）。
        # 新订阅者加入后，立即将 agent 缓冲的最近事件（stream_chunk、
        # tool_call 等）推给客户端。这解决了 WebSocket join 延迟导致
        # 前端错过初始事件的问题。
        replay_events = status_event_bus.get_agent_replay(agent_id)
        for evt in replay_events:
            event_name, payload = _map_event(evt)
            phoenix_msg = [join_ref, None, topic, event_name, payload]
            await send_fn(phoenix_msg)

        # 获取历史消息
        from hiveweave.services.chat_message import ChatMessageService

        chat_service = ChatMessageService()
        try:
            history = await chat_service.get_messages(agent_id, limit=JOIN_HISTORY_LIMIT)
        except Exception:
            history = []

        init_payload = {
            "agentId": agent_id,
            "name": agent_config.get("name", ""),
            "role": agent_config.get("role", ""),
            "history": history,
            "inbox": [],
        }
        await send_fn(
            [join_ref, join_ref, topic, "init", init_payload]
        )

    else:
        # 未知 topic — 仍然回复 ok（避免前端卡在 join 状态）
        log.debug("phoenix_join_unknown_topic", topic=topic)
        await send_fn(
            [join_ref, ref, topic, "phx_reply", {"status": "ok", "response": {}}]
        )
        joined[topic] = session
        return

    # 启动 forward task（从 bus queue 读取事件并转发）
    if session.queue is not None:
        session.forward_task = asyncio.create_task(
            _forward_bus_events(topic, join_ref, session.queue, send_fn),
            name=f"phx-forward-{topic}",
        )

    joined[topic] = session

    # 回复 join ok
    await send_fn(
        [join_ref, ref, topic, "phx_reply", {"status": "ok", "response": {}}]
    )
    log.debug("phoenix_joined", topic=topic)


async def _handle_phoenix_leave(
    topic: str,
    join_ref: str,
    ref: str,
    joined: dict[str, ChannelSession],
    send_fn: Any,
) -> None:
    """处理 phx_leave 事件。"""
    session = joined.pop(topic, None)
    if session:
        if session.queue is not None:
            await status_event_bus.unsubscribe(session.queue)
        if session.forward_task is not None:
            session.forward_task.cancel()
            try:
                await session.forward_task
            except (asyncio.CancelledError, Exception):
                pass

    await send_fn(
        [join_ref, ref, topic, "phx_reply", {"status": "ok", "response": {}}]
    )
    log.debug("phoenix_left", topic=topic)


# ── Push 事件处理 ───────────────────────────────────────────


async def _handle_phoenix_push(
    topic: str,
    event: str,
    payload: dict,
    joined: dict[str, ChannelSession],
    send_fn: Any,
) -> None:
    """处理客户端 push 事件（chat / cancel）。"""
    if topic not in joined:
        return

    if event == "chat":
        await _handle_chat_push(topic, payload, send_fn)
    elif event == "cancel":
        await _handle_cancel_push(topic, send_fn)
    # 其他 push 事件忽略


async def _handle_chat_push(topic: str, payload: dict, send_fn: Any) -> None:
    """处理 chat push — 保存用户消息 + 发送 message_id + 调用 agent.chat。"""
    if not topic.startswith("agent:"):
        return

    agent_id = topic.split(":", 1)[1]
    message = payload.get("message", "")
    images = payload.get("images")

    from hiveweave.services.chat_message import ChatMessageService

    chat_service = ChatMessageService()

    # 保存用户消息
    try:
        saved = await chat_service.save_message(
            {
                "agent_id": agent_id,
                "role": "user",
                "content": message,
                "is_streaming": False,
            }
        )
        # 发送 message_id 事件
        await send_fn(
            [None, None, topic, "message_id",
             {"id": saved["id"], "agentId": agent_id, "role": "user"}]
        )
    except Exception as e:
        log.warning("phoenix_chat_save_failed", agent_id=agent_id, error=str(e))

    # 调用 agent.chat
    from hiveweave.agents.supervisor import agent_manager

    agent = agent_manager.get_agent(agent_id)
    if agent is None:
        await send_fn(
            [None, None, topic, "error",
             {"message": "Agent not running", "agentId": agent_id}]
        )
        return

    # 防御性修复：如果 agent 启动时没有设置流式回调（例如被
    # start_project_agents 启动但没有传入 callback），在此补充。
    # 没有这些回调，agent.chat() 不会广播 stream_chunk 事件，
    # 前端永远收不到响应。
    if getattr(agent, "_on_stream_event", None) is None:
        from hiveweave.realtime.event_bus import create_agent_callbacks
        project_id = getattr(agent, "project_id", "") or ""
        on_status, on_stream = create_agent_callbacks(agent_id, project_id)
        agent._on_status_change = on_status
        agent._on_stream_event = on_stream
        log.info("phoenix_patch_agent_callbacks", agent_id=agent_id)

    # BUG-036: JSON-structured user message — unambiguous sender identification.
    # Matches trigger.py's JSON message format: {"from": "...", "content": "..."}
    import json as _json
    user_msg = _json.dumps({"from": "用户", "content": message}, ensure_ascii=False)
    result = await agent.chat(user_msg)

    if result.get("error") == "busy":
        await send_fn(
            [None, None, topic, "error",
             {"message": "Agent is busy", "agentId": agent_id}]
        )
    elif result.get("error") == "paused":
        await send_fn(
            [None, None, topic, "error",
             {"message": "System is paused", "agentId": agent_id}]
        )
    # ok → 流式事件由 bus 自动推送


async def _handle_cancel_push(topic: str, send_fn: Any) -> None:
    """处理 cancel push — 取消当前 LLM 调用。"""
    if not topic.startswith("agent:"):
        return

    agent_id = topic.split(":", 1)[1]

    from hiveweave.agents.supervisor import agent_manager

    agent = agent_manager.get_agent(agent_id)
    if agent is not None:
        try:
            await agent.cancel()
        except Exception as e:
            log.warning("phoenix_cancel_failed", agent_id=agent_id, error=str(e))


# ── Forward 循环 ────────────────────────────────────────────


async def _forward_bus_events(
    topic: str,
    join_ref: str,
    queue: asyncio.Queue[dict],
    send_fn: Any,
) -> None:
    """从 bus queue 读取事件，映射后转发给 Phoenix 客户端。

    事件格式: [join_ref, null, topic, event_name, payload]
    """
    while True:
        event = await queue.get()
        try:
            event_name, payload = _map_event(event)
            log.debug("phoenix_forward", topic=topic, event_name=event_name, raw_type=event.get("type"))
            phoenix_msg = [join_ref, None, topic, event_name, payload]
            if not await send_fn(phoenix_msg):
                log.warning("phoenix_forward_send_failed", topic=topic)
                break
        except Exception as e:
            log.warning("phoenix_forward_error", topic=topic, error=str(e))


# ── Agent 启动辅助 ──────────────────────────────────────────


async def _ensure_agent_running(agent_id: str, config: dict) -> None:
    """确保 agent 正在运行（与 channels.py 相同逻辑）。"""
    from hiveweave.agents.supervisor import agent_manager
    from hiveweave.realtime.event_bus import create_agent_callbacks

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
        log.info("phoenix_ensure_agent", agent_id=agent_id, project_id=project_id)
    except Exception as e:
        log.warning("phoenix_ensure_agent_failed", agent_id=agent_id, error=str(e))


# ── 路由注册 ────────────────────────────────────────────────


def register_phoenix_route(app: FastAPI) -> None:
    """注册 Phoenix Channel WebSocket 路由。

    路由: ``/socket/websocket`` — 前端 phoenix.js 默认连接路径。
    """
    app.websocket("/socket/websocket")(phoenix_socket_ws)
    log.info("phoenix_route_registered", route="/socket/websocket")
