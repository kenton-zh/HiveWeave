"""Debug endpoints (contract 19, group 15 debug).

契约 19: Debug — agent state / conversation dump / memory dump / system state
- GET /api/debug/agents/{agentId}/state         agent 运行态（config + busy + status）
- GET /api/debug/agents/{agentId}/conversation  对话历史 dump
- GET /api/debug/agents/{agentId}/memory        三层记忆 dump
- GET /api/debug/system                         系统态（paused + 活跃 agent 数）
- GET /api/debug/traces?agentId=&hours=         事件追踪（同 /api/events/audit）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import structlog

from hiveweave.agents.supervisor import agent_manager
from hiveweave.db import meta as meta_db
from hiveweave.services.memory import MemoryService
from hiveweave.services.system_state import system_state
from hiveweave.services.event_audit import event_audit
from hiveweave.conversation.store import conversation_store

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])

_memory = MemoryService()


@router.get("/agents/{agent_id}/state")
async def agent_state(agent_id: str) -> dict:
    """agent 运行态（config + busy + status）。"""
    config = await meta_db.get_agent_by_id(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    busy = agent_manager.is_busy(agent_id)
    in_memory = agent_manager.get_agent(agent_id) is not None
    return {
        "agentId": agent_id,
        "config": config,
        "inMemory": in_memory,
        "busy": busy,
        "status": config.get("status", "active"),
    }


@router.get("/agents/{agent_id}/conversation")
async def conversation_dump(agent_id: str) -> dict:
    """对话历史 dump（conversation_turns 表）。"""
    project_id = await meta_db.get_agent_project_id(agent_id)
    if not project_id:
        raise HTTPException(status_code=404, detail="Agent project not found")
    try:
        history = await conversation_store.get_history(agent_id, project_id)
    except Exception as e:
        log.warning("conversation_dump_failed", agent_id=agent_id, error=str(e))
        history = []
    prefix = conversation_store.get_compacted_prefix(project_id, agent_id)
    return {
        "agentId": agent_id,
        "projectId": project_id,
        "messages": history,
        "messageCount": len(history),
        "compactedPrefix": prefix,
    }


@router.get("/agents/{agent_id}/memory")
async def memory_dump(agent_id: str) -> dict:
    """三层记忆 dump（project + agent + archive）。"""
    project_id = await meta_db.get_agent_project_id(agent_id)
    if not project_id:
        raise HTTPException(status_code=404, detail="Agent project not found")
    try:
        project_mems = await _memory.get_project_memories(project_id)
    except Exception as e:
        log.warning("project_memory_dump_failed", error=str(e))
        project_mems = []
    try:
        agent_mems = await _memory.get_agent_memories(agent_id, project_id)
    except Exception as e:
        log.warning("agent_memory_dump_failed", error=str(e))
        agent_mems = []
    return {
        "agentId": agent_id,
        "projectId": project_id,
        "project": project_mems,
        "agent": agent_mems,
        "projectCount": len(project_mems),
        "agentCount": len(agent_mems),
    }


@router.get("/system")
async def system_dump() -> dict:
    """系统态（paused + 活跃 agent 数 + 缓存连接数）。"""
    from hiveweave.db import project as project_db

    active_agents: list[str] = []
    try:
        from hiveweave.services.agent_router import agent_router
        active_agents = [r.agent_id for r in agent_router.list_active_routes()]
    except Exception as e:
        log.warning("system_dump_agents_failed", error=str(e))

    return {
        "paused": system_state.paused(),
        "activeAgentCount": len(active_agents),
        "activeAgents": active_agents[:50],
        "projectDbCacheSize": len(project_db._cache) if hasattr(project_db, "_cache") else None,
        "conversationCacheSize": len(conversation_store._cache),
    }


@router.get("/traces")
async def traces(
    agentId: str = Query(...),
    hours: int = Query(default=1, le=168),
    limit: int = Query(default=100, le=500),
) -> dict:
    """事件追踪（agent_events 时间线）。"""
    events = await event_audit.timeline(agentId, hours=hours, limit=limit)
    return {"traces": events, "agentId": agentId, "count": len(events)}


@router.get("/agents/{agent_id}/traces")
async def agent_traces(
    agent_id: str,
    hours: int = Query(default=24, le=168),
    limit: int = Query(default=200, le=500),
) -> dict:
    """返回 Agent 的对话轮次和 LLM 调用事件。

    对话消息按 user 消息分组为 turn，每组包含后续
    assistant + tool 消息，直到下一个 user 消息。

    返回: { turns, events } — turns 含 raw_messages 数组，
    events 含 token 信息。
    """
    import asyncio
    import time as time_module

    # ── 事件（LLM round token 信息）──────────────────────────
    try:
        events = await asyncio.wait_for(
            event_audit.timeline(agent_id, hours=hours, limit=limit),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        log.warning("agent_traces_events_timeout", agent_id=agent_id)
        events = []
    except Exception as e:
        log.warning("agent_traces_events_failed", agent_id=agent_id, error=str(e))
        events = []

    # ── 对话轮次（按 user 消息分组）─────────────────────────
    turns: list[dict] = []
    try:
        project_id = await meta_db.get_agent_project_id(agent_id)
        if project_id:
            history = await asyncio.wait_for(
                conversation_store.get_history(agent_id, project_id),
                timeout=5.0,
            )
            # 按 user 消息边界分组
            current_turn_msgs: list[dict] = []
            turn_index = 0
            for m in history:
                role = m.get("role", "")
                if role == "user" and current_turn_msgs:
                    # 遇到新 user 消息 → 保存前一轮
                    turns.append(_build_turn(turn_index, current_turn_msgs))
                    turn_index += 1
                    current_turn_msgs = []
                current_turn_msgs.append(m)
            # 最后一批
            if current_turn_msgs:
                turns.append(_build_turn(turn_index, current_turn_msgs))
    except asyncio.TimeoutError:
        log.warning("agent_traces_turns_timeout", agent_id=agent_id)
    except Exception as e:
        log.warning("agent_traces_turns_failed", agent_id=agent_id, error=str(e))

    return {
        "turns": turns,
        "events": events,
        "agentId": agent_id,
        "turnCount": len(turns),
        "eventCount": len(events),
    }


def _build_turn(turn_index: int, messages: list[dict]) -> dict:
    """将一组消息构建为一个 trace turn。

    提取 user 消息摘要、计算 tool call 数量、
    估算 token 用量（heuristic: 4 chars ≈ 1 token）。
    """
    import uuid as _uuid

    raw_messages = []
    approx_tokens = 0
    created_at = None
    user_content = ""
    tool_call_count = 0

    for m in messages:
        raw = dict(m)
        content = m.get("content") or ""
        role = m.get("role", "")

        # 估算 token
        approx_tokens += max(1, len(content) // 4)
        if role == "assistant" and m.get("tool_calls"):
            try:
                import json as _json
                tcs = _json.loads(m["tool_calls"]) if isinstance(
                    m["tool_calls"], str
                ) else m["tool_calls"]
                tool_call_count += len(tcs)
            except Exception:
                pass

        # 追踪 user 消息（摘要用）
        if role == "user" and not m.get("is_background"):
            user_content = content

        # 时间戳
        if created_at is None:
            created_at = m.get("created_at")

        # 确保序列化友好
        raw.setdefault("content", "")
        raw_messages.append(raw)

    # 摘要
    if user_content:
        summary = user_content.replace("\n", " ")[:120]
    elif tool_call_count > 0:
        summary = f"{tool_call_count} 次工具调用"
    else:
        summary = "(系统消息)"

    return {
        "id": str(_uuid.uuid4()),
        "turn_index": turn_index,
        "created_at": created_at or 0,
        "approx_tokens": approx_tokens,
        "tool_call_count": tool_call_count,
        "summary": summary,
        "message_count": len(raw_messages),
        "raw_messages": raw_messages,
    }
