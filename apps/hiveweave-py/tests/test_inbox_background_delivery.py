"""Inbox background delivery — BUGFIX: progress/ACK 消息静默丢失。

回归场景（井字棋实测事故）：
- 下属发"穷举验证 PASS / 全部完成"类消息 → 被 classify 为 progress → wake=0
- 旧行为：插入即 read=1，而 get_pending_messages 只取 read=0 AND wake=1
  → 消息永远不会进入接收方的任何对话上下文（CEO 看不到交付证据）
- 新行为：wake=0 消息 delivered=0 落库；下次自然触发时由
  get_undelivered_background 捎带进上下文；成功输出后随 mark_read_by_ids
  一并置 delivered=1（不重复捎带）；输出失败不标记 → 下次重试

测试策略与 test_task_service.py 一致：真实 per-project DB（tempfile），
仅 patch meta 路由 + 接收方状态 + 事件总线。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services.inbox import InboxService

PROJECT_ID = "test-bg-delivery"
CEO_ID = "test-ceo"
DEV_ID = "test-dev"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (CEO_ID, DEV_ID) else None

        async def fake_get_agent_by_id(aid: str):
            return {"id": aid, "name": "x", "status": "active"}

        async def fake_publish(*args, **kwargs):
            return None

        inbox_module._migrated.discard(CEO_ID)
        inbox_module._migrated.discard(DEV_ID)
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch(
                "hiveweave.realtime.event_bus.status_event_bus"
                ".publish_chat_message",
                fake_publish,
            ),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)


async def _fetch_one(env, sql, params):
    conn = await project_db.ensure_project_db(env["workspace_path"])
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


@pytest.mark.asyncio
async def test_progress_message_pending_background_delivery(env):
    """progress 消息：不唤醒、但 delivered=0 待捎带；处理成功后不再捎带。"""
    svc = InboxService()

    msg = await svc.send_message(
        DEV_ID, CEO_ID, "穷举验证 1720 节点全部完成，X_win=0"
    )
    assert msg["should_wake"] is False  # progress 不触发 LLM —— 保持不变

    row = await _fetch_one(
        env, "SELECT read, wake, delivered FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["read"] == 1      # 已读（watcher 忽略）
    assert row["wake"] == 0
    assert row["delivered"] == 0  # 但尚未交付进任何上下文 —— 关键修复点

    # wake 通道看不到它（行为不变）
    assert await svc.get_pending_messages(CEO_ID) == []
    # background 通道能看到 —— 修复前这里永远查不到（没有此通道）
    bg = await svc.get_undelivered_background(CEO_ID)
    assert [m["id"] for m in bg] == [msg["id"]]

    # 模拟一次成功输出后的 ACK（trigger 把 background id 并入 inbox_msg_ids，
    # agent 在非空输出后统一 mark_read_by_ids）
    await svc.mark_read_by_ids(CEO_ID, [msg["id"]])
    row = await _fetch_one(
        env, "SELECT delivered FROM inbox WHERE id = ?", [msg["id"]]
    )
    assert row["delivered"] == 1
    assert await svc.get_undelivered_background(CEO_ID) == []


@pytest.mark.asyncio
async def test_progress_collapse_keeps_single_pending_background(env):
    """同一发送者的 progress 按设计收敛（幂等键 sender+category+task）：
    无论发多少条，background 通道最多只有一条待捎带，不产生刷屏。"""
    svc = InboxService()
    m1 = await svc.send_message(DEV_ID, CEO_ID, "全部完成 1/3")
    m2 = await svc.send_message(DEV_ID, CEO_ID, "全部完成 2/3")

    bg = await svc.get_undelivered_background(CEO_ID)
    assert len(bg) == 1
    # 收敛到同一条（dedupe 返回已存在行的 id）
    assert bg[0]["id"] == m1["id"] == m2["id"]
    assert m2.get("deduped") is True

    # 处理后清空，不会反复捎带
    await svc.mark_read_by_ids(CEO_ID, [bg[0]["id"]])
    assert await svc.get_undelivered_background(CEO_ID) == []


@pytest.mark.asyncio
async def test_command_message_uses_wake_channel_not_background(env):
    """command/ask 消息走原 wake 通道，不进 background 通道。"""
    svc = InboxService()
    msg = await svc.send_message(DEV_ID, CEO_ID, "请在本轮明确二选一回复")

    pending = await svc.get_pending_messages(CEO_ID)
    assert [m["id"] for m in pending] == [msg["id"]]
    # wake 消息 delivered=1 落库（不需要 background 追踪）
    assert await svc.get_undelivered_background(CEO_ID) == []
