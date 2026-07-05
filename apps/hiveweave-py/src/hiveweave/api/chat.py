"""Chat endpoints (contract 19, group 6 + 7).

契约 19: Chat — 发送 + 历史 + 未读 + 收件箱 + 暂停/恢复 + 重置 + 解析模型 + SSE 流式
- POST   /api/chat                          触发 agent 聊天（含专家命令路由 + busy 重试）
- GET    /api/chat/history/{agentId}        历史消息（限 200 条）
- GET    /api/chat/unread/{agentId}         未读背景消息
- POST   /api/chat/mark-read                批量标记已读
- GET    /api/chat/inbox/{agentId}          收件箱
- POST   /api/chat/inbox                    发送 agent 间消息
- POST   /api/chat/pause | /resume          暂停/恢复系统
- GET    /api/chat/paused                   查暂停状态
- POST   /api/chat/reset-processing/{agentId}  强制重置 agent 处理状态
- GET    /api/chat/resolved-model/{agentId} 查 agent 解析后的实际模型
- GET    /api/chat/messages/{agentId}       查 agent 消息（数组直返）
- GET    /api/chat/todos/{agentId} + POST   待办
- GET    /api/chat/questions                待答问题
- POST   /api/chat/questions/{id}/answer    回答问题
- GET    /api/chat/{agentId}/stream         SSE 流式（text/event-stream）
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import structlog

from hiveweave.agents.supervisor import agent_manager
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services.chat_message import ChatMessageService
from hiveweave.services.inbox import InboxService
from hiveweave.services.model import ModelService
from hiveweave.services.system_state import system_state

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

_chat_msg = ChatMessageService()
_inbox = InboxService()
_model = ModelService()

#: 专家命令正则（契约 19 特别流程 1）
_EXPERT_CMD_RE = re.compile(r"^/(review|test|audit|perf)\s+(.+)$", re.IGNORECASE)
_EXPERT_ROLE_MAP = {
    "review": "code_reviewer",
    "test": "test_engineer",
    "audit": "security_auditor",
    "perf": "web_perf_auditor",
}

#: busy 重试 sleep（契约 19 特别流程 2）
_BUSY_RESET_SLEEP = 0.5


# ── SSE 流式事件总线 ─────────────────────────────────────────
# 每个 agent_id → 一组订阅者队列。agent 的 on_stream_event 回调向所有队列推事件。

_stream_queues: dict[str, set[asyncio.Queue]] = {}


def _subscribe(agent_id: str) -> asyncio.Queue:
    """订阅 agent 的流事件，返回一个新队列。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _stream_queues.setdefault(agent_id, set()).add(q)
    return q


def _unsubscribe(agent_id: str, q: asyncio.Queue) -> None:
    """取消订阅。"""
    subs = _stream_queues.get(agent_id)
    if subs:
        subs.discard(q)
        if not subs:
            _stream_queues.pop(agent_id, None)


def _emit_stream(agent_id: str, event: dict) -> None:
    """向 agent 的所有订阅者推流事件（best-effort，队列满则丢弃）。"""
    subs = _stream_queues.get(agent_id)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 丢弃背压事件


async def _stream_callback(agent_id: str, event: dict) -> None:
    """agent 的 on_stream_event 回调 → 推入事件总线。"""
    _emit_stream(agent_id, event)


# ── Agent 启动辅助 ───────────────────────────────────────────


async def _ensure_agent_started(agent_id: str) -> tuple[object, dict] | None:
    """确保 agent 已启动（带流事件回调）。返回 (agent, config) 或 None。"""
    agent = agent_manager.get_agent(agent_id)
    if agent is not None:
        return agent, agent.config
    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        return None
    project_id = config.get("project_id") or await meta_db.get_agent_project_id(
        agent_id
    )
    if not project_id:
        return None
    agent = await agent_manager.start_agent(
        agent_id,
        project_id,
        config,
        on_stream_event=_stream_callback,
    )
    return agent, config


# ── 请求/响应模型 ────────────────────────────────────────────


class ChatSendBody(BaseModel):
    agentId: str
    message: str
    images: list | None = None


class MarkReadBody(BaseModel):
    ids: list[str]
    agentId: str


class InboxSendBody(BaseModel):
    fromAgentId: str
    toAgentId: str
    content: str
    type: str | None = "normal"
    subject: str | None = None
    priority: str | None = "normal"
    metadata: dict | None = None


class TodoItem(BaseModel):
    content: str | None = None
    task: str | None = None
    status: str | None = "pending"
    priority: str | None = "medium"


class TodosBody(BaseModel):
    todos: list[TodoItem]


class QuestionAnswerBody(BaseModel):
    answer: str
    agentId: str


# ── 端点 ─────────────────────────────────────────────────────


@router.post("")
async def send_chat(body: ChatSendBody) -> dict:
    """触发 agent 聊天。

    契约 19 特别流程 1: 专家命令路由（/review /test /audit /perf）。
    契约 19 特别流程 2: busy 重试（force_reset + sleep 500ms + 重试 1 次）。
    """
    agent_id = body.agentId
    message = body.message

    # 1. 专家命令路由
    m = _EXPERT_CMD_RE.match(message.strip())
    if m:
        expert_role = _EXPERT_ROLE_MAP[m.group(1).lower()]
        routed = await _route_to_expert(agent_id, expert_role, message)
        if routed is not None:
            return routed

    # 2. 解析 agent
    started = await _ensure_agent_started(agent_id)
    if started is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent, config = started

    # 3. 保存用户消息
    user_msg = await _chat_msg.save_message(
        {
            "agent_id": agent_id,
            "role": "user",
            "content": message,
            "is_streaming": False,
            "is_read": True,
            "images": body.images,
        }
    )

    # 4. 触发 chat（含 busy 重试）
    result = await agent.chat(message)
    if result.get("error") == "busy":
        # force_reset + sleep + 重试
        await agent.cancel()
        await asyncio.sleep(_BUSY_RESET_SLEEP)
        result = await agent.chat(message)
        if result.get("error") == "busy":
            raise HTTPException(status_code=409, detail="Agent is busy after reset")
        return {"ok": True, "userMessageId": user_msg["id"], "reset": True}
    if result.get("error") == "paused":
        raise HTTPException(status_code=409, detail="System is paused")
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail="Failed to trigger chat")

    return {"ok": True, "userMessageId": user_msg["id"]}


async def _route_to_expert(
    agent_id: str, expert_role: str, message: str
) -> dict | None:
    """专家命令路由：在同项目内按 role 查找专家 agent。

    Returns: 路由结果 dict；未找到专家返回 None（退回普通处理）。
    """
    project_id = await meta_db.get_agent_project_id(agent_id)
    if not project_id:
        return None
    from hiveweave.services.org import OrgService

    org = OrgService()
    agents = await org.list_agents(project_id)
    expert = next(
        (a for a in agents if a.get("role") == expert_role), None
    )
    if expert is None:
        return None  # 退回普通处理

    expert_id = expert["id"]
    started = await _ensure_agent_started(expert_id)
    if started is None:
        return None

    # 投递到专家 inbox
    await _inbox.send_message(
        from_agent_id=agent_id,
        to_agent_id=expert_id,
        message=message,
        message_type="expert_dispatch",
        priority="normal",
    )
    # 保存一条"已路由"assistant 消息给原 agent
    await _chat_msg.save_message(
        {
            "agent_id": agent_id,
            "role": "assistant",
            "content": f"[ROUTED] Message routed to {expert_role}.",
            "is_background": True,
        }
    )
    return {"ok": True, "routed": True, "expert": expert_role}


@router.get("/history/{agent_id}")
async def chat_history(agent_id: str) -> dict:
    """历史消息（限 200 条）。"""
    messages = await _chat_msg.get_messages(agent_id, limit=200)
    return {"messages": messages}


@router.get("/unread/{agent_id}")
async def chat_unread(agent_id: str) -> dict:
    """未读背景消息。"""
    messages = await _chat_msg.get_unread_background(agent_id)
    return {"messages": messages, "count": len(messages)}


@router.post("/mark-read")
async def chat_mark_read(body: MarkReadBody) -> dict:
    """批量标记已读。"""
    count = await _chat_msg.mark_as_read(body.agentId, body.ids)
    return {"ok": True, "count": count}


@router.get("/inbox/{agent_id}")
async def chat_inbox(agent_id: str) -> dict:
    """收件箱。"""
    messages = await _inbox.get_pending_messages(agent_id)
    unread = await _inbox.get_unread_count(agent_id)
    return {"messages": messages, "unreadCount": unread}


@router.post("/inbox")
async def chat_send_inbox(body: InboxSendBody) -> dict:
    """发送 agent 间消息。"""
    try:
        msg = await _inbox.send_message(
            from_agent_id=body.fromAgentId,
            to_agent_id=body.toAgentId,
            message=body.content,
            message_type=body.type or "normal",
            priority=body.priority or "normal",
        )
    except Exception as e:
        log.error("send_inbox_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create communication")
    return {"ok": True, "message": msg}


@router.post("/pause")
async def pause_system() -> dict:
    """暂停系统。"""
    system_state.pause()
    return {"paused": True}


@router.post("/resume")
async def resume_system() -> dict:
    """恢复系统。"""
    system_state.resume()
    return {"paused": False}


@router.get("/paused")
async def is_paused() -> dict:
    """查暂停状态。"""
    return {"paused": system_state.paused()}


@router.post("/reset-processing/{agent_id}")
async def reset_processing(agent_id: str) -> dict:
    """强制重置 agent 处理状态（force_reset 信号 + 重置 idle）。"""
    agent = agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await agent.cancel()
    return {"ok": True, "agentId": agent_id, "processing": False}


@router.get("/resolved-model/{agent_id}")
async def resolved_model(agent_id: str) -> dict:
    """查 agent 解析后的实际模型。"""
    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    model_id = config.get("model_id")
    if not model_id:
        return {"agentId": agent_id, "modelName": None, "modelId": None, "source": "none"}
    model = await _model.get(model_id)
    if model is None:
        return {"agentId": agent_id, "modelName": None, "modelId": model_id, "source": "none"}
    return {
        "agentId": agent_id,
        "modelName": model.get("name"),
        "modelId": model.get("model_id"),
        "source": "auto",
    }


@router.get("/messages/{agent_id}")
async def chat_messages(agent_id: str) -> list:
    """查 agent 消息（数组直返，限 200 条）。"""
    return await _chat_msg.get_messages(agent_id, limit=200)


@router.get("/todos/{agent_id}")
async def get_todos(agent_id: str) -> dict:
    """查 agent 待办。"""
    try:
        rows = await project_db.query(
            agent_id,
            "SELECT id, content, status, priority FROM todos "
            "WHERE agent_id = ? ORDER BY created_at ASC",
            [agent_id],
        )
        return {"todos": [dict(r) for r in rows]}
    except Exception:
        return {"todos": []}


@router.post("/todos/{agent_id}")
async def set_todos(agent_id: str, body: TodosBody) -> dict:
    """覆盖写 agent 待办（先 DELETE 再 INSERT）。"""
    try:
        await project_db.execute(agent_id, "DELETE FROM todos WHERE agent_id = ?", [agent_id])
        now_ms = int(time.time() * 1000)
        todos_out = []
        for item in body.todos:
            tid = str(uuid.uuid4())
            content = item.content or item.task or ""
            await project_db.execute(
                agent_id,
                "INSERT INTO todos (id, agent_id, content, status, priority, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [tid, agent_id, content, item.status or "pending",
                 item.priority or "medium", now_ms, now_ms],
            )
            todos_out.append({"id": tid, "content": content,
                              "status": item.status or "pending",
                              "priority": item.priority or "medium"})
        return {"ok": True, "todos": todos_out}
    except Exception as e:
        log.error("set_todos_failed", agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to set todos")


@router.get("/questions")
async def get_questions(
    agentId: str | None = Query(default=None),
    projectId: str | None = Query(default=None),
) -> dict:
    """查待答问题（query: agentId 或 projectId）。"""
    try:
        if agentId:
            rows = await project_db.query(
                agentId,
                "SELECT id, agent_id, question, answer, status, created_at, "
                "answered_at FROM questions WHERE agent_id = ? "
                "ORDER BY created_at DESC",
                [agentId],
            )
        elif projectId:
            workspace = await meta_db.get_project_workspace(projectId)
            if not workspace:
                return {"questions": []}
            conn = await project_db.ensure_project_db(workspace)
            cursor = await conn.execute(
                "SELECT id, agent_id, question, answer, status, created_at, "
                "answered_at FROM questions WHERE project_id = ? "
                "ORDER BY created_at DESC",
                [projectId],
            )
            rows = await cursor.fetchall()
            await cursor.close()
        else:
            return {"questions": []}
        return {"questions": [dict(r) for r in rows]}
    except Exception as e:
        log.warning("get_questions_failed", error=str(e))
        return {"questions": []}


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, body: QuestionAnswerBody) -> dict:
    """回答问题。"""
    try:
        now_ms = int(time.time() * 1000)
        await project_db.execute(
            body.agentId,
            "UPDATE questions SET answer = ?, status = 'answered', "
            "answered_at = ? WHERE id = ?",
            [body.answer, now_ms, question_id],
        )
    except Exception as e:
        log.error("answer_question_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to answer question")
    return {"ok": True, "answer": body.answer}


# ── SSE 流式端点 ─────────────────────────────────────────────


@router.get("/{agent_id}/stream")
async def chat_stream(agent_id: str):
    """SSE 流式端点 — 订阅 agent 的流事件。

    契约 19: Accept: text/event-stream
    事件格式:
        data: {"type":"start","agentId":"..."}
        data: {"type":"text_delta","content":"..."}
        data: {"type":"done"}
    """
    # 确保 agent 存在
    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    queue = _subscribe(agent_id)

    async def event_generator():
        try:
            yield _sse({"type": "start", "agentId": agent_id})
            deadline = time.time() + 300  # 5 分钟最大连接时长
            while time.time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # keepalive 心跳
                    yield ": keepalive\n\n"
                    continue
                ev_type = event.get("type", "event")
                payload = {"type": ev_type, "agentId": agent_id}
                payload.update(event)
                yield _sse(payload)
                if ev_type in ("done", "error", "chat_done"):
                    break
            yield _sse({"type": "done", "agentId": agent_id})
        finally:
            _unsubscribe(agent_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict) -> str:
    """构造一条 SSE data 行。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
