"""Streaming finalize / broadcast / streamer callbacks.

Extracted from agent.py — behavior-preserving mechanical split (P1).
Module-level functions take ``agent`` as first arg; Agent methods are thin wrappers.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.agents.constants import TOOL_RESULT_STREAM_EXCERPT
from hiveweave.agents.helpers.tools_def import _short_hash

log = structlog.get_logger(__name__)


async def finalize_streaming_turn(
    agent: Any,
    *,
    msg_id: str | None = None,
    content: str | None = None,
    thinking: object | None = None,
    tool_calls_json: str | None = None,
    metadata: dict | None = None,
    allow_agent_wide_fallback: bool = True,
) -> bool:
    """Close this turn's streaming placeholder — never leave a DB orphan.

    ``update_message`` can return False without raising (no DB / no row).
    Callers used to clear ``_streaming_msg_id`` anyway → true orphans.
    This helper only drops the in-memory pointer after a confirmed clear.
    """
    target_id = agent._streaming_msg_id if msg_id is None else msg_id
    attrs: dict = {}
    if content is not None:
        attrs["content"] = content
    if thinking is not None:
        attrs["thinking"] = thinking
    if tool_calls_json is not None:
        attrs["tool_calls"] = tool_calls_json
    if metadata is not None:
        attrs["metadata"] = metadata

    # Never agent-wide-clear if a newer turn already owns another placeholder
    fallback = allow_agent_wide_fallback
    if (
        fallback
        and target_id is not None
        and agent._streaming_msg_id is not None
        and agent._streaming_msg_id != target_id
    ):
        fallback = False

    cleared = await agent._chat_msg.finalize_streaming_message(
        agent.id,
        target_id,
        attrs or None,
        allow_agent_wide_fallback=fallback,
    )
    if cleared and agent._streaming_msg_id == target_id:
        agent._streaming_msg_id = None
    return cleared


def broadcast_status(agent: Any, status: str, extra: dict | None = None) -> None:
    """广播状态变更（通过回调，不直接依赖 WebSocket）。"""
    if agent._on_status_change is not None:
        try:
            result = agent._on_status_change(
                agent.id, status, extra or {}
            )
            # 如果回调返回协程，不需要 await（fire-and-forget）
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            pass


def broadcast_stream_event(agent: Any, event: dict) -> None:
    """广播流事件（通过回调，不直接依赖 WebSocket）。"""
    etype = event.get("type", "?")
    has_cb = agent._on_stream_event is not None
    log.debug(
        "agent_broadcast_stream",
        agent_id=agent.id,
        event_type=etype,
        has_callback=has_cb,
    )
    if agent._on_stream_event is not None:
        try:
            result = agent._on_stream_event(agent.id, event)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as e:
            log.warning(
                "agent_broadcast_failed", agent_id=agent.id, error=str(e)
            )


def broadcast_agent_health(agent: Any, health: str, message: str = "") -> None:
    """广播 agent 健康事件（LLM 调用出错 / 恢复）。

    前端契约（经 publish_stream_event 发到 lobby + agent:{id} 频道）::

        {"type": "agent_health", "agentId": ..., "projectId": ...,
         "health": "error" | "ok", "message": "<错误摘要，截断 200 字符；ok 时为空串>",
         "at": <毫秒时间戳>}

    "agent_health" 不属于 _DELTA_ONLY_TYPES，因此会分发到 lobby。
    广播失败静默吞掉 —— 绝不能因广播异常搞挂 agent。
    """
    try:
        broadcast_stream_event(agent, {
            "type": "agent_health",
            "agentId": agent.id,
            "projectId": agent.project_id,
            "health": health,
            "message": message[:200],
            "at": int(time.time() * 1000),
        })
    except Exception as e:
        log.warning(
            "agent_health_broadcast_failed",
            agent_id=agent.id,
            error=str(e),
        )


async def on_delta(agent: Any, event: dict) -> None:
    """SSE delta 回调 — 转发给流事件回调 + 实时落库。

    每次 text_delta 都立即写入 DB streaming placeholder，确保：
    1. 前端长时间看不到新消息时（agent 多轮工具调用，
       placeholder 一直空），不会误判为 [对话被中断]
    2. 后端崩溃/重启时，部分输出已持久化

    FIX(text-acc): 收到 round_start 时重置 DB 文本累积器。前端 stream
    draft 必须同步丢掉上一轮 text/thinking 段（保留 tool_call），否则
    工具循环会把每轮复述叠在同一气泡里。
    """
    # P0-3: 任何 streamer 事件都算流式活动（僵尸判定信号）。
    # 本地 thinking 心跳不经此回调（agent._heartbeat 直接广播），
    # 不会掩盖「HTTP 挂死零事件」的真卡死。
    agent._last_stream_activity_at = time.time() * 1000
    if event.get("type") == "llm_queue":
        # Semaphore wait ping: keep zombie sweep alive, do not feed or
        # stop the thinking heartbeat (tokens have not started).
        return
    # 第一个 delta 到达 → 停止心跳（LLM 开始产出内容了）
    agent._stop_heartbeat()
    broadcast_stream_event(agent, event)

    # 工具循环新一轮 → 重置文本累积器 + BUG-7 按轮次累加 LLM 调用
    if event.get("type") == "round_start":
        agent._streaming_text_acc = ""
        if agent._streaming_msg_id:
            try:
                await agent._chat_msg.update_message(
                    agent.id, agent._streaming_msg_id,
                    {"content": ""},
                )
            except Exception:
                pass
        _run_id = getattr(agent, "_current_run_id", None)
        if _run_id:
            try:
                await agent._run_ledger.increment_llm_calls(agent.id, _run_id)
            except Exception:
                pass
        return

    if event.get("type") == "text_delta" and agent._streaming_msg_id:
        acc = getattr(agent, "_streaming_text_acc", "")
        acc += event.get("content", "")
        agent._streaming_text_acc = acc
        # Save every chunk (not batch) — long tool-call sequences
        # produce few text deltas, and DB writes are cheap.
        try:
            await agent._chat_msg.update_message(
                agent.id, agent._streaming_msg_id,
                {"content": acc},
            )
        except Exception:
            pass  # Best-effort


async def on_tool_call(
    agent: Any, tool_name: str, arguments: str, tool_call_id: str
) -> dict:
    """工具执行回调 — 桥接 Streamer 和 ToolExecutor。

    Streamer 期望返回: {"role": "tool", "content": "...", "tool_call_id": "..."}
    ToolExecutor 返回: {"success": bool, "output": str, "error": str | None}
    """
    # 解析参数
    try:
        tool_args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        tool_args = {}

    workspace = await agent._get_workspace_path()
    project_ws = await meta_db.get_project_workspace(agent.project_id) or workspace

    # 工具调用开始 → 停止心跳（agent 已进入工具执行阶段）
    agent._stop_heartbeat()

    # 广播工具调用开始
    broadcast_stream_event(
        agent,
        {
            "type": "tool_call_start",
            "tool_name": tool_name,
            "arguments": arguments,
            "tool_call_id": tool_call_id,
        },
    )

    # ── Durable Run Ledger: record step start ──
    step_id = None
    _run_id = getattr(agent, "_current_run_id", None)
    if _run_id:
        args_hash = _short_hash(arguments) if arguments else None
        # P2 fix(TEST10): 预分配 index 再 await，避免并行工具调用竞态
        # （多个并行 call 同时读 counter → 同 index）。先自增再落库。
        current_index = agent._run_step_counter
        agent._run_step_counter += 1
        try:
            step_id = await agent._run_ledger.record_step_start(
                agent_id=agent.id,
                run_id=_run_id,
                step_index=current_index,
                step_type="tool_call",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_args_hash=args_hash,
            )
            # BUG-7: increment tool counter at step start (covers interrupt path)
            await agent._run_ledger.increment_tool_calls(agent.id, _run_id)
        except Exception as e:
            log.debug("run_ledger.step_start_failed", error=str(e))

    # 执行工具
    result = await agent._tool_executor.execute(
        agent_id=agent.id,
        tool_name=tool_name,
        tool_args=tool_args,
        workspace_path=workspace,
        project_root=project_ws,
    )

    # ── Durable Run Ledger: record step end ──
    if step_id:
        try:
            result_content = result.get("output") or ""
            await agent._run_ledger.record_step_end(
                agent_id=agent.id,
                step_id=step_id,
                status="completed" if result.get("success") else "failed",
                result_hash=_short_hash(result_content[:1000]) if result_content else None,
                result_size=len(result_content),
                error=result.get("error"),
                result_excerpt=result_content or result.get("error"),
            )
        except Exception as e:
            log.debug("run_ledger.step_end_failed", error=str(e))

    # 转换格式
    content = result.get("output") or ""
    if result.get("error") and not content:
        content = f"Error: {result['error']}"

    # 广播工具调用结束（result 摘要随事件推送，前端折叠行展开用）
    broadcast_stream_event(
        agent,
        {
            "type": "tool_call_end",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "success": result.get("success", False),
            "result": (content or "")[:TOOL_RESULT_STREAM_EXCERPT],
        },
    )

    return {
        "role": "tool",
        "content": content,
        "tool_call_id": tool_call_id,
        # success / duplicate / end_turn 透传给 streamer：
        # - success: 失败调用
        # - duplicate: 同参已执行过无新效果（doom 加速）
        # - end_turn: commit_turn 已接受 → 硬断本轮工具循环（BUG-3）
        # - blocked: 平台护栏/沙箱/权限拒绝（H3 stall 分流）——必须透传，
        #   否则 tool_exec 的 blocked_ids 永远为空（2026-08-13 审计 P0）。
        # 这些键不会进入发给 LLM 的消息体 —— _execute_tools 重新组包时剥离。
        "success": result.get("success", False),
        "duplicate": result.get("duplicate", False),
        "end_turn": bool(result.get("end_turn")),
        "blocked": bool(result.get("blocked")),
        # Multimodal screenshot pixels (browse / assert_visual)
        "images": result.get("images"),
    }
