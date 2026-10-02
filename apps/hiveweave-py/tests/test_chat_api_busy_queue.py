"""T14：chat API busy 409 语义对齐回归（busy 一律入队，409 只留 paused）.

S15 现场锚点（2026-09）: 给进行中 agent 连发消息收 HTTP 409（轮询三次
才成功）；而日志里另有 trigger_busy_steered_inbox / trigger_busy_enqueued
—— busy 时消息明明可以入队。两条路径并存且 409 那条对用户不友好。

现状结论（本文件钉死的新语义）:
  - 409 真实触发条件（修复前）:
      ① api/chat.py send_chat 步骤3 预检 ``status == "processing"`` →
         409 "Agent is busy"，消息**不落库不入队**（S15 病灶）；
      ② ``agent.chat() 返回 {"error": "busy"} → force_reset + 重试 →
         仍 busy → 409 "Agent is busy after reset"`` —— 自 P1 单飞改造
         （agent.chat busy 时入 _message_queue 返回 {ok, queued}）起该
         分支即为**死代码**，从未执行；
      ③ ``{"error": "paused"}`` → 409 "System is paused"（唯一合理 409）。
  - 对齐后: busy → 200 {ok, queued: true}，消息存 chat_messages 并入
    inbox 队列（from=用户, type=user_message, trusted_platform —— 与
    三端共用入口 deliver_user_message 的 busy 语义逐字一致）；409 仅
    保留 paused，带 Retry-After 轮询建议头。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from hiveweave.api.chat import ChatSendBody, send_chat
from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db

PROJECT_ID = "test-chatbusy-project"
AGENT_ID = "chatbusy-agent"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        project_db._agent_cache[AGENT_ID] = workspace_path
        await ensure_project_db(workspace_path)

        async def fake_get_agent_by_id(aid: str):
            if aid == AGENT_ID:
                return {"id": AGENT_ID, "project_id": PROJECT_ID,
                        "name": "甲", "role": "executor"}
            return None

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch("hiveweave.services.off_duty.is_agent_off_duty",
                  AsyncMock(return_value=False)),
        ):
            yield {"workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            project_db._agent_cache.pop(AGENT_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _processing_agent() -> SimpleNamespace:
    return SimpleNamespace(
        id=AGENT_ID,
        project_id=PROJECT_ID,
        config={"id": AGENT_ID, "role": "executor"},
        status=SimpleNamespace(value="processing"),
        _on_stream_event=lambda event: None,
        _on_status_change=lambda event: None,
    )


def _idle_agent(chat_result: dict | None = None) -> SimpleNamespace:
    agent = SimpleNamespace(
        id=AGENT_ID,
        project_id=PROJECT_ID,
        config={"id": AGENT_ID, "role": "executor"},
        status=SimpleNamespace(value="idle"),
        _on_stream_event=lambda event: None,
        _on_status_change=lambda event: None,
        chat=AsyncMock(return_value=chat_result or {"ok": True}),
    )
    return agent


async def _chat_rows(env) -> list[dict]:
    conn = await ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT id, role, content, metadata FROM chat_messages "
        "WHERE agent_id = ? ORDER BY created_at ASC",
        [AGENT_ID])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _inbox_rows(env) -> list[dict]:
    conn = await ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT from_agent_id, to_agent_id, message, message_type, wake "
        "FROM inbox WHERE to_agent_id = ?",
        [AGENT_ID])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── 状态一：busy → 一律入队（不再 409）──────────────────────


async def test_busy_message_enqueued_not_409(env):
    """agent processing 时发消息 → 200 {ok, queued}，消息落 chat_messages
    且入 inbox 队列（与 deliver_user_message busy 语义一致）。"""
    agent = _processing_agent()
    with patch("hiveweave.api.chat.agent_manager") as mgr:
        mgr.get_agent.return_value = agent
        result = await send_chat(
            ChatSendBody(agentId=AGENT_ID, message="请修复导出的编码问题"))

    assert result["ok"] is True
    assert result["queued"] is True
    assert result["userMessageId"]

    # chat_messages：用户消息已落库（不因 busy 丢失）
    rows = await _chat_rows(env)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "请修复导出的编码问题"

    # inbox：用户通道排队（trusted 口径 from=用户），watcher 会在 agent
    # 空闲后随 trigger digest 投递
    inbox = await _inbox_rows(env)
    assert len(inbox) == 1
    assert inbox[0]["from_agent_id"] == "用户"
    assert inbox[0]["message_type"] == "user_message"
    assert inbox[0]["message"] == "请修复导出的编码问题"


async def test_busy_no_agent_chat_call(env):
    """busy 入队路径不启动 LLM 回合（chat() 不被调用）。"""
    agent = _processing_agent()
    agent.chat = AsyncMock()
    with patch("hiveweave.api.chat.agent_manager") as mgr:
        mgr.get_agent.return_value = agent
        await send_chat(
            ChatSendBody(agentId=AGENT_ID, message="排查一下这个告警"))
    agent.chat.assert_not_awaited()


# ── 状态二：paused → 409 + Retry-After ──────────────────────


async def test_paused_409_with_retry_after(env):
    """系统暂停（不可自愈，需人工 resume）→ 409，detail 保留原因，
    Retry-After 头给调用方轮询节奏建议。"""
    agent = _idle_agent({"error": "paused"})
    with patch("hiveweave.api.chat.agent_manager") as mgr:
        mgr.get_agent.return_value = agent
        with pytest.raises(HTTPException) as exc_info:
            await send_chat(ChatSendBody(agentId=AGENT_ID, message="hello"))
    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.detail == "System is paused"
    assert exc.headers is not None
    assert exc.headers.get("Retry-After") == "5"


# ── 不可恢复：agent 不存在 → 404（语义不变）─────────────────


async def test_unknown_agent_still_404(env):
    with patch("hiveweave.api.chat.agent_manager") as mgr:
        mgr.get_agent.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            await send_chat(ChatSendBody(agentId="no-such-agent", message="hi"))
    assert exc_info.value.status_code == 404
