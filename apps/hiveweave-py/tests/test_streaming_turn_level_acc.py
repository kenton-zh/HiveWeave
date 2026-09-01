"""流式 DB 快照必须是 turn 级累计 — round_start 不得清空 content。

此前 on_delta 在 round_start 时把 ``_streaming_text_acc`` 与 DB content
一起清零重写：重连/刷新后前端从 DB 恢复只能看到「当前轮」文本，用户已
看到的前几轮叙述整段消失。前端 beginStreamRound 已是 no-op（整轮时间线
口径），DB 快照与直播 draft 对齐后任何时刻恢复都不回退。

2026-09-01 契约升级（s3-clone_06「大段旁白」反馈）：turn 级累计保留，
但 round_start（round>=1）在 acc 尾部插入轮次分隔行并同步 DB——content
兜底渲染从此有轮次结构；finalize content 由 segments 重建，标记不泄进
终稿。"""

from __future__ import annotations

from typing import Any

import pytest

from hiveweave.agents.streaming import on_delta


class _FakeChatMsg:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str | None, dict]] = []

    async def update_message(self, agent_id: str, msg_id: str | None, attrs: dict) -> bool:
        self.updates.append((agent_id, msg_id, attrs))
        return True


class _FakeRunLedger:
    def __init__(self) -> None:
        self.llm_calls: list[str] = []

    async def increment_llm_calls(self, agent_id: str, run_id: str) -> None:
        self.llm_calls.append(run_id)


def _make_agent() -> Any:
    class _Agent:
        id = "a1"
        _streaming_msg_id = "m1"
        _current_run_id = "r1"
        _streaming_text_acc = ""
        _last_stream_activity_at = 0.0
        _chat_msg = _FakeChatMsg()
        _run_ledger = _FakeRunLedger()
        _on_stream_event = None

        def _stop_heartbeat(self) -> None:
            pass

    return _Agent()


@pytest.mark.asyncio
async def test_round_start_keeps_accumulated_text() -> None:
    agent = _make_agent()

    await on_delta(agent, {"type": "round_start", "round": 0})
    await on_delta(agent, {"type": "text_delta", "content": "第一轮旁白。"})
    await on_delta(agent, {"type": "round_start", "round": 1})
    await on_delta(agent, {"type": "text_delta", "content": "第二轮正文。"})

    # turn 级累计 + 轮次分隔标记：跨轮文本都保留，轮边界插入分隔行
    assert agent._streaming_text_acc == "第一轮旁白。\n\n—— 第 2 轮 ——\n\n第二轮正文。"
    content_writes = [
        attrs.get("content")
        for (_, _, attrs) in agent._chat_msg.updates
        if "content" in attrs
    ]
    assert content_writes == [
        "第一轮旁白。",
        "第一轮旁白。\n\n—— 第 2 轮 ——\n\n",
        "第一轮旁白。\n\n—— 第 2 轮 ——\n\n第二轮正文。",
    ]
    # 不再有 round_start 触发的 content="" 清空写
    assert "" not in content_writes


@pytest.mark.asyncio
async def test_round_0_and_invalid_round_insert_no_marker() -> None:
    """首轮（round 0）与缺号/非法轮号都不插分隔标记。"""
    agent = _make_agent()

    await on_delta(agent, {"type": "round_start", "round": 0})
    assert agent._streaming_text_acc == ""
    assert agent._chat_msg.updates == []

    await on_delta(agent, {"type": "round_start"})  # 缺 round 字段
    await on_delta(agent, {"type": "round_start", "round": "x"})  # 非法
    assert agent._streaming_text_acc == ""


@pytest.mark.asyncio
async def test_round_start_still_counts_llm_calls() -> None:
    agent = _make_agent()

    await on_delta(agent, {"type": "round_start", "round": 1})

    assert agent._run_ledger.llm_calls == ["r1"]


@pytest.mark.asyncio
async def test_new_turn_placeholder_resets_accumulator() -> None:
    """同一 agent 连续两 turn：round_start 不清零后，turn 起点必须重置
    累积器，否则上一 turn 的全文会泄进本 turn placeholder 的 DB 快照
    （重连恢复时同一段话在两个气泡里重复）。"""
    agent = _make_agent()

    # turn 1
    await on_delta(agent, {"type": "round_start", "round": 0})
    await on_delta(agent, {"type": "text_delta", "content": "turn1 全文"})
    assert agent._streaming_text_acc == "turn1 全文"

    # turn 2：chat() 创建新 placeholder 时重置（agent.py turn 起点逻辑）
    agent._streaming_msg_id = "m2"
    agent._streaming_text_acc = ""

    await on_delta(agent, {"type": "round_start", "round": 0})
    await on_delta(agent, {"type": "text_delta", "content": "turn2 开头"})

    assert agent._streaming_text_acc == "turn2 开头"
    turn2_writes = [
        attrs.get("content")
        for (_, mid, attrs) in agent._chat_msg.updates
        if mid == "m2" and "content" in attrs
    ]
    assert turn2_writes == ["turn2 开头"]
