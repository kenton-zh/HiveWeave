"""Inbox watcher 复活 — cancel() 永久杀死 watcher 的回归测试。

病灶：Agent.cancel() 置 _stop_watcher=True 并取消 watcher 协程，但全仓
没有任何代码把它重置回 False。Agent 对象仍留在 manager 里、仍能被
trigger，自主 watcher 却已永久死亡 —— agent 读不到同伴消息（"失联"），
直到后端进程重启。cancel() 的调用方正是 busy 强制重置 / reset_processing
（api/chat.py）以及 deactivate→activate 路径（supervisor 跳过已存在实例）。

修复：agent 再次被激活的入口（chat() / enqueue_wake()）调用
_ensure_watcher_alive() —— watcher 已死则重置 _stop_watcher=False 并
重启轮询协程；watcher 存活时幂等返回；cancel() 当下仍能即时停止。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from hiveweave.agents.agent import Agent, AgentState


def _make_agent() -> Agent:
    """在 running loop 内构造 Agent（watcher 协程随 __init__ 启动）。

    stub 掉 inbox 轮询，避免测试触碰真实 DB。
    """
    agent = Agent(
        "agent-watcher-1",
        "proj-watcher-1",
        {"name": "Watcher", "role": "executor"},
    )
    agent._inbox.get_pending_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return agent


async def _wait_until(predicate, timeout_s: float = 4.0) -> bool:
    """轮询等待条件成立（给 watcher 的 1s 起步延迟留余量）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_init_starts_watcher():
    """构造即启动 watcher 协程（有 running loop 时）。"""
    agent = _make_agent()
    try:
        assert agent._stop_watcher is False
        assert agent._inbox_watcher_task is not None
        assert not agent._inbox_watcher_task.done()
    finally:
        await agent.cancel()


async def test_cancel_kills_watcher():
    """cancel() 置标志并杀掉 watcher 协程（修复前此处即永久死亡）。"""
    agent = _make_agent()
    task = agent._inbox_watcher_task
    await agent.cancel()

    assert agent._stop_watcher is True
    assert task is not None and task.done()


async def test_chat_revives_watcher_after_cancel():
    """核心回归：cancel 杀死 watcher 后，再次 chat() 应复活它。

    用 paused 短路 chat 的后续 DB 流程 —— 复活发生在锁入口，早于
    busy/paused 检查，因此即使 chat 提前返回 watcher 也已复活。
    """
    agent = _make_agent()
    try:
        await agent.cancel()
        dead_task = agent._inbox_watcher_task
        assert dead_task is not None and dead_task.done()

        with patch("hiveweave.agents.agent.system_state") as mock_state:
            mock_state.paused.return_value = True
            result = await agent.chat("hello")

        assert result == {"error": "paused"}  # paused 短路，未触 DB
        assert agent._stop_watcher is False
        new_task = agent._inbox_watcher_task
        assert new_task is not None and new_task is not dead_task
        assert not new_task.done()
    finally:
        await agent.cancel()


async def test_revived_watcher_resumes_polling():
    """复活后的 watcher 真的恢复轮询 inbox（不只是任务对象存活）。"""
    agent = _make_agent()
    try:
        await agent.cancel()
        agent._inbox.get_pending_messages.reset_mock()  # type: ignore[union-attr]

        agent._ensure_watcher_alive()

        polled = await _wait_until(
            lambda: agent._inbox.get_pending_messages.await_count > 0  # type: ignore[union-attr]
        )
        assert polled, "revived watcher never polled inbox"
    finally:
        await agent.cancel()


async def test_ensure_watcher_alive_idempotent():
    """watcher 存活时重复调用不重启任务（同一 task 对象）。"""
    agent = _make_agent()
    try:
        task = agent._inbox_watcher_task
        agent._ensure_watcher_alive()
        agent._ensure_watcher_alive()
        assert agent._inbox_watcher_task is task
        assert not task.done()  # type: ignore[union-attr]
    finally:
        await agent.cancel()


async def test_cancel_still_stops_revived_watcher():
    """cancel 的即时停止语义不被破坏：复活后再次 cancel 仍然停掉。"""
    agent = _make_agent()
    await agent.cancel()

    agent._ensure_watcher_alive()
    revived = agent._inbox_watcher_task
    assert revived is not None and not revived.done()

    await agent.cancel()
    assert agent._stop_watcher is True
    assert revived.done()


async def test_enqueue_wake_revives_watcher():
    """busy 期间的 trigger wake（enqueue_wake 入口）同样复活 watcher。"""
    agent = _make_agent()
    try:
        await agent.cancel()
        assert agent._stop_watcher is True

        result = await agent.enqueue_wake("wake", {"trigger": True})

        assert result == {"ok": True, "queued": True}
        assert agent._stop_watcher is False
        assert agent._inbox_watcher_task is not None
        assert not agent._inbox_watcher_task.done()
    finally:
        await agent.cancel()


async def test_busy_chat_queues_and_revives_without_db_writes():
    """busy 分支（消息入队）也先复活 watcher。

    busy 分支会写 chat_messages DB —— stub 掉 _chat_msg，只验证
    复活发生在入队之前且不依赖后续流程。
    """
    agent = _make_agent()
    try:
        await agent.cancel()
        agent.status = AgentState.PROCESSING
        agent._chat_msg.get_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        result = await agent.chat("queued msg")

        assert result == {"ok": True, "queued": True}
        assert agent._stop_watcher is False
        assert agent._inbox_watcher_task is not None
        assert not agent._inbox_watcher_task.done()
        assert len(agent._message_queue) == 1
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()


async def test_busy_chat_trigger_digest_saved_as_background():
    """busy 分支落库 trigger digest 必须带 background/context/team 标志。

    回归：修复前 busy 分支硬编码 is_background=False，平台注入的 trigger
    digest（role=user）被前端渲染成主聊天流的用户蓝泡。
    """
    agent = _make_agent()
    try:
        agent.status = AgentState.PROCESSING
        agent._chat_msg.get_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        result = await agent.chat(
            "## Messages (chronological) — digest",
            {"trigger": True, "from_agent_id": "sender-1"},
        )

        assert result == {"ok": True, "queued": True}
        assert len(agent._message_queue) == 1
        agent._chat_msg.save_message.assert_awaited_once()  # type: ignore[union-attr]
        saved = agent._chat_msg.save_message.await_args.args[0]  # type: ignore[union-attr]
        assert saved["role"] == "user"
        assert saved["is_background"] is True
        assert saved["is_context"] is True
        assert saved["team_from_agent_id"] == "sender-1"
        assert saved["team_to_agent_id"] == agent.id
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()


async def test_busy_chat_user_message_stays_foreground():
    """busy 分支落库真人消息必须保持前景，且不写 trigger 专属字段。"""
    agent = _make_agent()
    try:
        agent.status = AgentState.PROCESSING
        agent._chat_msg.get_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        result = await agent.chat("hello from user")

        assert result == {"ok": True, "queued": True}
        assert len(agent._message_queue) == 1
        agent._chat_msg.save_message.assert_awaited_once()  # type: ignore[union-attr]
        saved = agent._chat_msg.save_message.await_args.args[0]  # type: ignore[union-attr]
        assert saved["role"] == "user"
        assert saved["is_background"] is False
        assert "is_context" not in saved
        assert "team_from_agent_id" not in saved
        assert "team_to_agent_id" not in saved
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()


async def test_busy_chat_json_user_message_saved_as_plain():
    """busy 分支收到的 BUG-036 JSON 信封用户消息，落库必须存纯文本 content。

    回归：api/chat.py、phoenix_adapter.py 落库纯文本但传 JSON
    {"from":"用户","content":...} 给 chat()；busy 分支若把 JSON 外壳存给前端，
    会显示 JSON 串且去重永远失配。
    """
    agent = _make_agent()
    try:
        agent.status = AgentState.PROCESSING
        agent._chat_msg.get_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        import json as _json
        result = await agent.chat(_json.dumps({"from": "用户", "content": "hello"}))

        assert result == {"ok": True, "queued": True}
        assert len(agent._message_queue) == 1
        agent._chat_msg.save_message.assert_awaited_once()  # type: ignore[union-attr]
        saved = agent._chat_msg.save_message.await_args.args[0]  # type: ignore[union-attr]
        assert saved["content"] == "hello"
        assert saved["is_background"] is False
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()


async def test_busy_chat_json_user_message_deduped_by_plain_content():
    """JSON 信封用户消息在 3 秒窗口内命中已存纯文本时，避免重复落库。

    回归：调用方（api/chat.py 等）已落库纯文本 content，但传给 chat() 的是
    JSON 信封；busy 分支去重须按纯文本比对，否则重复落一条 JSON 前景消息。
    """
    agent = _make_agent()
    try:
        agent.status = AgentState.PROCESSING
        agent._chat_msg.get_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=[{"role": "user", "content": "hello", "created_at": int(time.time() * 1000)}]
        )
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        import json as _json
        result = await agent.chat(_json.dumps({"from": "用户", "content": "hello"}))

        assert result == {"ok": True, "queued": True}
        assert len(agent._message_queue) == 1
        agent._chat_msg.save_message.assert_not_awaited()  # type: ignore[union-attr]
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()


async def test_busy_chat_trigger_digest_deduped_by_dedup_content():
    """trigger digest 在 busy 竞态下按 opts.dedup_content 命中已存 digest 去重。

    回归：trigger.py 用 strip 后的 chat_context 落库，但传全量 context 给 chat()
    （经 opts.dedup_content 携带 strip 文本）；busy 分支须按 dedup_content 比对
    才能命中已存 digest，避免重复落库。
    """
    agent = _make_agent()
    try:
        agent.status = AgentState.PROCESSING
        stripped = "## Messages (chronological) — stripped digest"
        agent._chat_msg.get_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=[{"role": "user", "content": stripped, "created_at": int(time.time() * 1000)}]
        )
        agent._chat_msg.save_message = AsyncMock(return_value={"id": "m1"})  # type: ignore[method-assign]

        result = await agent.chat(
            "## Messages (chronological) — full digest with goals block",
            {"trigger": True, "dedup_content": stripped},
        )

        assert result == {"ok": True, "queued": True}
        assert len(agent._message_queue) == 1
        agent._chat_msg.save_message.assert_not_awaited()  # type: ignore[union-attr]
    finally:
        agent.status = AgentState.IDLE  # 让 cancel 走轻量路径
        await agent.cancel()
