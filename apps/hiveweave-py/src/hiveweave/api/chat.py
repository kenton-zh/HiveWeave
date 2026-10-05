"""Chat endpoints (contract 19, group 6 + 7).

契约 19: Chat — 发送 + 历史 + 未读 + 收件箱 + 暂停/恢复 + 重置 + 解析模型 + SSE 流式
- POST   /api/chat                          触发 agent 聊天（含专家命令路由；busy 入队不拒收，T14）
- GET    /api/chat/history/{agentId}        历史消息（UI：分栏窗口，非混合 100）
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

from hiveweave.api.auth import validate_id
from hiveweave.agents.agent import Agent
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


async def _ensure_agent_started(agent_id: str) -> tuple[Agent, dict] | None:
    """确保 agent 已启动（带流事件回调）。返回 (agent, config) 或 None。"""
    agent = agent_manager.get_agent(agent_id)
    if agent is not None:
        # 防御性修复：如果 agent 启动时没有设置流式回调，在此补充
        if getattr(agent, "_on_stream_event", None) is None:
            from hiveweave.realtime.event_bus import create_agent_callbacks
            project_id = getattr(agent, "project_id", "") or ""
            on_status, on_stream = create_agent_callbacks(agent_id, project_id)
            agent._on_status_change = on_status
            agent._on_stream_event = on_stream
            log.info("rest_patch_agent_callbacks", agent_id=agent_id)
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
    T14 语义: busy 一律入队（返回 {ok, queued: true}，消息经 inbox 队列
    在 agent 空闲后投递）；409 仅保留 paused（带 Retry-After 轮询建议）。
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

    # 1b. 下班：不启动 agent，保存用户消息 + 自动回复
    from hiveweave.services.off_duty import (
        is_agent_off_duty,
        send_off_duty_auto_reply,
    )

    if await is_agent_off_duty(agent_id):
        # 确认 agent 存在（否则 404）
        config = await meta_db.get_agent_by_id(agent_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        user_msg = await _chat_msg.save_message(
            {
                "agent_id": agent_id,
                "role": "user",
                "content": message,
                "is_streaming": False,
                "is_read": True,
                "images": body.images,
                "metadata": {"source": "web"},  # REST 面统一 web 口径（原 user，381dbe5 P1 备忘）
            }
        )
        asst = await send_off_duty_auto_reply(agent_id)
        return {
            "ok": True,
            "userMessageId": user_msg["id"],
            "offDuty": True,
            "reply": asst["content"],  # 与落库/流式同源（T13 platform-notice 前缀）
            "assistantMessageId": asst["id"],
        }

    # 2. 解析 agent
    started = await _ensure_agent_started(agent_id)
    if started is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent, config = started

    # 3. 保存用户消息（busy 入队分支共用；先落库再触发，BUG-036 口径）
    async def _save_web_user_msg() -> dict:
        return await _chat_msg.save_message(
            {
                "agent_id": agent_id,
                "role": "user",
                "content": message,
                "is_streaming": False,
                "is_read": True,
                "images": body.images,
                "metadata": {"source": "web"},  # REST 面统一 web 口径（原 user，381dbe5 P1 备忘）
            }
        )

    # 3a. T14 对齐：busy 一律入队，不再 409 拒收。与三端共用入口
    #     deliver_user_message 的 busy 语义逐字一致：存 chat_messages +
    #     InboxService 排队（from=用户, type=user_message, trusted_platform），
    #     inbox watcher 在 agent 空闲后随 trigger digest 投递。
    #     （S15 现场病灶：原实现此处 409，调用方轮询三次才成功。）
    #     409 只保留不可恢复/需人工介入的 paused（见下）。
    agent_check = agent_manager.get_agent(agent_id)
    if agent_check is not None and agent_check.status.value == "processing":
        user_msg = await _save_web_user_msg()
        await _inbox.send_message(
            from_agent_id="用户",
            to_agent_id=agent_id,
            message=message,
            message_type="user_message",
            priority="normal",
            trusted_platform=True,
        )
        log.info("chat_busy_enqueued", agent_id=agent_id,
                 user_message_id=user_msg["id"])
        return {"ok": True, "queued": True, "userMessageId": user_msg["id"]}

    # 4. 保存用户消息
    user_msg = await _save_web_user_msg()

    # 5. 触发 chat
    # BUG-036: JSON-structured user message — unambiguous sender identification
    import json as _json
    user_msg_str = _json.dumps({"from": "用户", "content": message}, ensure_ascii=False)
    # 用户发图喂 LLM（与 WS idle 路径同口径）：data URL → 内部格式，经 opts
    # 穿到本轮 user 消息与 conversation 用户 turn。chat_messages 已存原图供
    # UI；无图时保持原单参调用，行为逐字不变。
    from hiveweave.services.vision import parse_user_images

    user_images = parse_user_images(body.images) if body.images else []
    # I15(P2-5)：REST 用户直聊显式报 source="chat" —— "chat" 只许在用户
    # 入口显式传；缺省会落 "unknown"+告警（见 agent._activation_trigger_fields）。
    chat_extra: dict = {"opts": {"source": "chat"}}
    if user_images:
        chat_extra["opts"]["images"] = user_images
    result = await agent.chat(user_msg_str, **chat_extra)
    # T14：chat() 在 PROCESSING 下入内存队并返回 {ok, queued}，不再返回
    # busy（原 force_reset + 重试分支自单飞改造起即为死代码，已删——
    # 它若被执行会强杀运行中回合）。唯一残余 409 = paused。
    if result.get("error") == "paused":
        # 可恢复但需人工（resume）；Retry-After 是轮询节奏建议
        # （调用方可轮询 GET /api/chat/paused），非恢复时间承诺。
        raise HTTPException(
            status_code=409,
            detail="System is paused",
            headers={"Retry-After": "5"},
        )
    if result.get("held"):
        # 会务 hold（docs/spec/team-meeting.md §前端）：chat() 卡口把消息
        # 原样入队保留（散会回岗随普通 trigger 消化），REST 返回结构化
        # error，前端/调用方据此知道本轮未启动 LLM。
        return {
            "error": "meeting_hold",
            "queued": True,
            "userMessageId": user_msg["id"],
        }
    if result.get("error") == "project_not_started":
        asst = await send_off_duty_auto_reply(agent_id)
        return {
            "ok": True,
            "userMessageId": user_msg["id"],
            "offDuty": True,
            "reply": asst["content"],  # 与落库/流式同源（T13 platform-notice 前缀）
            "assistantMessageId": asst["id"],
        }
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
            "metadata": {"source": "system", "kind": "routed"},
        }
    )
    return {"ok": True, "routed": True, "expert": expert_role}


async def _ui_chat_messages(agent_id: str, limit: int, offset: int) -> list:
    """Chat UI load.

    offset=0: union of two recency windows (up to ``limit`` main-pane
    user/assistant + ``limit`` team/background-user). ``len(rows)`` may be
    up to 2×limit; this is not mixed ``LIMIT limit``.

    offset>0: 主栏同谓词分页（user/assistant 窗口向后翻页）——前端
    滚动到顶「加载更早」用。与 offset=0 的 direct 窗谓词一致，翻页
    不会产生缺口；team 消息不混入（团队沟通栏不走此分页）。
    """
    validate_id(agent_id, "agent_id")
    if offset:
        return await _chat_msg.get_direct_window_messages(
            agent_id, limit=limit, offset=offset
        )
    return await _chat_msg.get_panel_messages(
        agent_id, direct_limit=limit, other_limit=limit
    )


@router.get("/history/{agent_id}")
async def chat_history(
    agent_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """历史消息。offset=0 分栏窗口（最多 2×limit）；offset>0 为混合调试分页。"""
    return {"messages": await _ui_chat_messages(agent_id, limit, offset)}


@router.get("/unread/{agent_id}")
async def chat_unread(agent_id: str) -> dict:
    """未读背景消息。"""
    validate_id(agent_id, "agent_id")
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
    validate_id(agent_id, "agent_id")
    messages = await _inbox.get_pending_messages(agent_id)
    unread = await _inbox.get_unread_count(agent_id)
    return {"messages": messages, "unreadCount": unread}


@router.post("/inbox")
async def chat_send_inbox(body: InboxSendBody) -> dict:
    """发送 agent 间消息。

    BUG-010 修复：写入 inbox 后显式触发目标 agent（比后台 watcher
    5s 延迟更及时）。watcher 仍然兜底——如果 agent 实例不存在，
    等后续 start_agent 时 inbox 仍在那里，下次轮询触发。

    Security: this REST endpoint is NOT trusted for human identity —
    send_message raises ValueError for from="用户"/"user"/type=user_message
    without trusted_platform=True. Real human speech must go through the
    phoenix WebSocket (session-authenticated, trusted_platform=True).
    The ValueError → 400 here is the anti-spoofing gate, not a gap.
    """
    try:
        msg = await _inbox.send_message(
            from_agent_id=body.fromAgentId,
            to_agent_id=body.toAgentId,
            message=body.content,
            message_type=body.type or "normal",
            priority=body.priority or "normal",
        )
    except ValueError as e:
        log.warning(
            "send_inbox_rejected",
            error=str(e),
            from_agent_id=body.fromAgentId,
            message_type=body.type,
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("send_inbox_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create communication")

    # BUG-022 fix: remove explicit trigger — the target agent's inbox watcher
    # handles this autonomously. Double-triggering (API + watcher + tool executor)
    # caused duplicate task delivery.

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
    validate_id(agent_id, "agent_id")
    agent = agent_manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await agent.cancel(reason="reset_processing")
    return {"ok": True, "agentId": agent_id, "processing": False}


@router.get("/resolved-model/{agent_id}")
async def resolved_model(agent_id: str) -> dict:
    """查 agent 解析后的实际模型（与运行时 _get_model_config 同逻辑）。"""
    validate_id(agent_id, "agent_id")
    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    model_id = config.get("model_id")

    # 显式配置了模型 → 直接查
    if model_id:
        model = await _model.get(model_id)
        if model is None:
            return {"agentId": agent_id, "modelName": None, "modelId": model_id, "source": "explicit_missing"}
        return {
            "agentId": agent_id,
            "modelName": model.get("name"),
            "modelId": model.get("model_id"),
            "source": "explicit",
        }

    # 无显式配置 → 走 tier 解析（与 agent._get_model_config 一致）
    from hiveweave.services.policy import model_tier_for_agent

    tier = model_tier_for_agent(config)
    resolved = await _model.resolve_model(tier=tier)
    if resolved:
        return {
            "agentId": agent_id,
            "modelName": resolved.get("name"),
            "modelId": resolved.get("model_id"),
            "source": f"tier_{tier}",
        }

    return {"agentId": agent_id, "modelName": None, "modelId": None, "source": "none"}


@router.get("/messages/{agent_id}")
async def chat_messages(
    agent_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list:
    """查 agent 消息（数组直返）。offset=0 时分栏取最近，避免主栏被工位流水挤空。"""
    return await _ui_chat_messages(agent_id, limit, offset)


@router.get("/todos/{agent_id}")
async def get_todos(agent_id: str) -> dict:
    """查 agent 待办。"""
    validate_id(agent_id, "agent_id")
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
    validate_id(agent_id, "agent_id")
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
    status: str | None = Query(default=None),
) -> dict:
    """查待答问题（query: agentId 或 projectId，可选 status 过滤）。"""
    try:
        # status 过滤条件（仅在 status 非空时追加）
        status_clause = " AND status = ?" if status else ""

        if agentId:
            params: list = [agentId] + ([status] if status else [])
            rows = await project_db.query(
                agentId,
                "SELECT id, agent_id, question, options, answer, status, created_at, "
                "answered_at FROM questions WHERE agent_id = ?"
                + status_clause +
                " ORDER BY created_at DESC",
                params,
            )
        elif projectId:
            workspace = await meta_db.get_project_workspace(projectId)
            if not workspace:
                return {"questions": []}
            conn = await project_db.ensure_project_db(workspace)
            params2: list = [projectId] + ([status] if status else [])
            cursor = await conn.execute(
                "SELECT id, agent_id, question, options, answer, status, created_at, "
                "answered_at FROM questions WHERE project_id = ?"
                + status_clause +
                " ORDER BY created_at DESC",
                params2,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        else:
            return {"questions": []}
        # Parse options JSON string → array for frontend
        # Also convert snake_case keys to camelCase for frontend compatibility
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if d.get("options") and isinstance(d["options"], str):
                try:
                    d["options"] = json.loads(d["options"])
                except (json.JSONDecodeError, TypeError):
                    d["options"] = None
            # snake_case → camelCase for frontend (PendingQuestion interface)
            d["agentId"] = d.pop("agent_id", None)
            d["agentName"] = d.pop("agent_name", None)
            if "created_at" in d:
                d["createdAt"] = d.pop("created_at")
            if "answered_at" in d:
                d["answeredAt"] = d.pop("answered_at")
            result.append(d)
        return {"questions": result}
    except Exception as e:
        log.warning("get_questions_failed", error=str(e))
        return {"questions": []}


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, body: QuestionAnswerBody) -> dict:
    """回答问题。

    P1-12（批22）语义：``pending``/``cancelled`` = 正常作答（原行为）；
    ``timed_out`` = **迟到人类裁决** —— 应用用户选择、给 agent 投递
    ``[QUESTION RE-ADJUDICATED]`` 平台通知（wake=1，原自动裁决被推翻）、
    user_pings 置 ``readjudicated``；``answered`` = 已收口 —— 同答案重复
    提交幂等放行（不重投），不同答案拒绝（重判语义只开 timed_out）。
    """
    validate_id(question_id, "question_id")
    now_ms = int(time.time() * 1000)
    try:
        rows = await project_db.query(
            body.agentId,
            "SELECT id, agent_id, project_id, question, options, answer, "
            "status, resolved_by FROM questions WHERE id = ?",
            [question_id],
        )
    except Exception as e:
        log.error("answer_question_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to answer question")
    row = dict(rows[0]) if rows else None
    status = str((row or {}).get("status") or "")
    prev_answer = str((row or {}).get("answer") or "")

    # 已收口（answered）：同答案 = 幂等 no-op；不同答案 = 拒绝（409）。
    # 在写事务 try 之外 raise —— 外层 except Exception 会把 HTTPException
    # 重包成 500，冲突语义必须原样透出。
    if row is not None and status == "answered":
        if (body.answer or "").strip() == prev_answer.strip():
            return {
                "ok": True,
                "answer": body.answer,
                "readjudicated": False,
                "idempotent": True,
            }
        raise HTTPException(
            status_code=409,
            detail=(
                "question already answered; re-adjudication is only available "
                "for timed_out questions"
            ),
        )

    readjudicated = row is not None and status == "timed_out"
    try:
        if readjudicated:
            # 迟到裁决：应用用户选择（行留痕 resolved_by='user_readjudicated'）。
            await project_db.execute(
                body.agentId,
                "UPDATE questions SET answer = ?, status = 'answered', "
                "answered_at = ?, resolved_by = 'user_readjudicated' "
                "WHERE id = ?",
                [body.answer, now_ms, question_id],
            )
            # 批24 修复5：迟到裁决同样是用户作答动作 ⇒ 回填 read_at（幂等）。
            if row is not None:
                from hiveweave.tools.question import backfill_user_ping_read_at

                await backfill_user_ping_read_at(
                    str(row.get("project_id") or ""), question_id, now_ms=now_ms
                )
        else:
            await project_db.execute(
                body.agentId,
                "UPDATE questions SET answer = ?, status = 'answered', "
                "answered_at = ? WHERE id = ?",
                [body.answer, now_ms, question_id],
            )
            # 正常作答也同步 ping 台账（晚到答案不走 execute_question 的
            # future 分支，无这条会永远停在 pending）。
            if row is not None:
                from hiveweave.tools.question import (
                    _update_user_ping_status,
                    backfill_user_ping_read_at,
                )

                await _update_user_ping_status(
                    str(row.get("project_id") or ""),
                    question_id,
                    "answered",
                )
                # 批24 修复5：作答 ⇒ 回填关联 user_pings.read_at
                # （幂等：read_at 已有值不覆盖；历史 NULL 不追）。
                await backfill_user_ping_read_at(
                    str(row.get("project_id") or ""), question_id, now_ms=now_ms
                )
        # BUG-037: resolve 内存中的 Future，让阻塞中的 execute_question 继续
        from hiveweave.tools.question import resolve_question
        resolved = resolve_question(question_id, body.answer)
        if not resolved:
            log.info("answer_question_no_pending_future",
                     question_id=question_id,
                     msg="Question answered in DB but no in-memory Future found "
                         "(may have timed out or been cancelled)")
        if readjudicated and row is not None:
            # P1-12：迟到裁决投递 —— 同答案不重投（幂等由 changed 判定）。
            from hiveweave.tools.question import deliver_question_readjudication

            await deliver_question_readjudication(
                str(row.get("project_id") or ""),
                str(row.get("agent_id") or body.agentId),
                question_id,
                question_text=str(row.get("question") or ""),
                previous_choice=prev_answer,
                new_answer=body.answer,
                changed=(body.answer or "").strip() != prev_answer.strip(),
            )
    except Exception as e:
        log.error("answer_question_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to answer question")
    return {"ok": True, "answer": body.answer, "readjudicated": readjudicated}


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
    validate_id(agent_id, "agent_id")
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


# ── 前端 RESTful 路径参数兼容路由 ─────────────────────────────
# 前端期望 /api/chat/{agentId}/messages 风格；保留现有 /messages/{agentId} 与
# POST /api/chat（agentId in body）路由，额外提供 path 参数变体。
# COMPAT: 前端 api.ts 期望的 RESTful 路径


@router.get("/{agent_id}/messages")
async def chat_messages_path(
    agent_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list:
    """查 agent 消息（path: agentId）— 前端 RESTful 兼容路由。

    R7 fix: 分页参数透传。R11: COMPAT 兼容路由。
    """
    return await _ui_chat_messages(agent_id, limit, offset)


# COMPAT: 前端 api.ts 期望的 RESTful 路径
@router.post("/{agent_id}/messages")
async def send_chat_path(agent_id: str, body: ChatSendBody) -> dict:
    """触发 agent 聊天（path: agentId 覆盖 body agentId）— 前端 RESTful 兼容路由。

    R11: COMPAT 兼容路由。
    """
    validate_id(agent_id, "agent_id")
    overridden = body.model_copy(update={"agentId": agent_id})
    return await send_chat(overridden)


# ── COMPAT: /api/chat/comms (BUG-029 fix) ────────────────────

@router.get("/comms")
async def chat_comms_compat(
    projectId: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> dict:
    """列出团队通信（兼容别名，委托到 /api/communications）。

    BUG-029: 某些客户端/测试脚本使用 /api/chat/comms 而非 /api/communications。
    此路由作为兼容别名，内部直接委托到 list_communications。
    """
    from hiveweave.api.communications import list_communications
    return await list_communications(projectId=projectId, limit=limit)
