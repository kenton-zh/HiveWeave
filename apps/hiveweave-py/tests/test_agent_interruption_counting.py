"""turn 中断恢复统一化 + 安全超时计数 — 回归测试。

覆盖:
- 安全超时未超限: 计数 + 保留 inbox 未读 + RESUME CHECKPOINT + 冷却 resume
- 安全超时超限: ACK inbox 放弃 + 升级上级一次 + 广播 error 健康红框（堵死循环）
- 升级每个失败 streak 只发一次（防跨 agent 振荡）
- _handle_error 与 _handle_safety_timeout 共用同一计数（混合 streak 也收口）
- 成功完成一轮后计数与冷却归零
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents.agent import (
    ERROR_RESUME_COOLDOWN_S,
    TIMEOUT_RESUME_COOLDOWN_S,
    Agent,
    AgentState,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)


PROJECT_ID = "interruption-test-project"
AGENT_ID = "interruption-exec"
SUPERIOR_ID = "interruption-boss"


def _make_agent(stream_events: list | None = None) -> Agent:
    """轻量构造：绕过 __init__（不起 watcher/服务），直接装配 mock 依赖。"""
    agent = Agent.__new__(Agent)
    agent.id = AGENT_ID
    agent.project_id = PROJECT_ID
    agent.config = {"name": "Exec", "role": "executor"}
    agent.status = AgentState.PROCESSING
    agent.empty_retry_count = 0
    agent.pending_inbox_msg_ids = ["inbox-1", "inbox-2"]
    agent.current_job = None
    agent._cancel_reason = None
    agent._message_queue = []
    agent._streaming_msg_id = None
    agent._resume_cooldown_until = 0.0
    agent._consecutive_errors = 0
    agent._CONSECUTIVE_ERROR_MAX = 3
    agent._resume_suppressed = False
    agent._pending_resume_hint = None
    agent.disposition = "runnable"
    agent._llm_task = None
    agent._safety_timer = None
    agent._on_status_change = None
    agent._on_stream_event = (
        (lambda aid, ev: stream_events.append(ev))
        if stream_events is not None
        else None
    )
    # turn-gate 计数器（_reset_to_idle / _handle_completion 会引用）
    agent._reply_reminder_count = 0
    agent._task_reminder_count = 0
    agent._turn_gate_count = 0
    agent._TURN_GATE_MAX = 1
    agent._slice_budget = 0
    agent._SLICE_BUDGET_MAX = 2
    agent._progress_fingerprint = None
    agent._no_progress_streak = 0
    agent.visibility = "foreground"
    agent._MERGE_WINDOW_MS = 300
    agent._workspace_path = None
    # 服务依赖全部 mock
    agent._conversation = AsyncMock()
    agent._inbox = AsyncMock()
    agent._org = AsyncMock()
    agent._chat_msg = AsyncMock()
    agent._work_log = AsyncMock()
    return agent


def _arm_pending(agent: Agent) -> None:
    """每轮中断后 pending 会被清空；模拟下一轮 trigger 重新认领 inbox。"""
    agent.pending_inbox_msg_ids = ["inbox-1", "inbox-2"]


def _health_events(stream_events: list) -> list[dict]:
    return [e for e in stream_events if e.get("type") == "agent_health"]


# ── 安全超时: 未超限 → resume 且计数 ─────────────────────────


class TestSafetyTimeoutResume:
    async def test_timeout_counts_and_arms_resume(self):
        events: list = []
        agent = _make_agent(events)

        await agent._handle_safety_timeout()

        # 计数 +1，未超限 → 保留 resume 语义
        assert agent._consecutive_errors == 1
        assert agent._in_resume_cooldown() is True
        remaining = agent._resume_cooldown_until - time.monotonic()
        assert 0 < remaining <= TIMEOUT_RESUME_COOLDOWN_S
        # inbox 未 ACK（保持未读，等 watcher 冷却后恢复）
        agent._inbox.mark_read_by_ids.assert_not_called()
        # RESUME CHECKPOINT is ephemeral — not appended to conversation history
        agent._conversation.append_turn.assert_not_called()
        assert agent._pending_resume_hint is not None
        assert "[RESUME CHECKPOINT]" in agent._pending_resume_hint
        assert "safety_timeout" in agent._pending_resume_hint
        # 本轮 pending 清空、状态回 idle、广播 error 红框
        assert agent.pending_inbox_msg_ids is None
        assert agent.status == AgentState.IDLE
        health = _health_events(events)
        assert health and health[-1]["health"] == "error"

    async def test_consecutive_timeouts_increment_counter(self):
        agent = _make_agent([])

        await agent._handle_safety_timeout()
        _arm_pending(agent)
        await agent._handle_safety_timeout()

        assert agent._consecutive_errors == 2
        # 仍未超限: 继续 resume，不升级、不 ACK
        agent._inbox.mark_read_by_ids.assert_not_called()
        agent._inbox.send_message.assert_not_called()


# ── 安全超时: 超限 → 放弃 + 升级 + 广播 ──────────────────────


class TestSafetyTimeoutGiveUp:
    async def test_give_up_acks_inbox_and_escalates(self):
        events: list = []
        agent = _make_agent(events)
        agent._consecutive_errors = agent._CONSECUTIVE_ERROR_MAX  # 本次即越限
        agent._org.get_superior = AsyncMock(return_value={"id": SUPERIOR_ID})

        with patch(
            "hiveweave.agents.trigger.trigger_coordinator", AsyncMock()
        ) as trig:
            await agent._handle_safety_timeout()

        assert agent._consecutive_errors == agent._CONSECUTIVE_ERROR_MAX + 1
        # 放弃: ACK inbox 停止 resume 循环 + latch
        assert agent._resume_suppressed is True
        agent._inbox.mark_read_by_ids.assert_awaited_once_with(
            AGENT_ID, ["inbox-1", "inbox-2"]
        )
        # 不再注入 CHECKPOINT（避免撑大上下文）、不再 arm 冷却
        agent._conversation.append_turn.assert_not_called()
        assert agent._pending_resume_hint is None
        assert agent._in_resume_cooldown() is False
        # 升级上级一次 + 触发上级
        agent._inbox.send_message.assert_awaited_once()
        kwargs = agent._inbox.send_message.await_args.kwargs
        assert kwargs["to_agent_id"] == SUPERIOR_ID
        assert kwargs["message_type"] == "escalation"
        assert kwargs["priority"] == "urgent"
        assert "gave up" in kwargs["message"]
        trig.assert_awaited_once_with(SUPERIOR_ID)
        # 广播 error 健康红框 + 落盘消息说明已放弃
        health = _health_events(events)
        assert health and health[-1]["health"] == "error"
        saved = agent._chat_msg.save_message.await_args.args[0]
        assert "Gave up" in saved["content"]

    async def test_escalation_sent_only_once_per_streak(self):
        """同一失败 streak 的后续放弃只 ACK 止血，不重复打扰上级。"""
        agent = _make_agent([])
        agent._consecutive_errors = agent._CONSECUTIVE_ERROR_MAX + 1  # 已升级过
        agent._org.get_superior = AsyncMock(return_value={"id": SUPERIOR_ID})

        with patch(
            "hiveweave.agents.trigger.trigger_coordinator", AsyncMock()
        ) as trig:
            await agent._handle_safety_timeout()

        assert agent._consecutive_errors == agent._CONSECUTIVE_ERROR_MAX + 2
        agent._inbox.mark_read_by_ids.assert_awaited_once()  # 仍 ACK 止血
        agent._inbox.send_message.assert_not_called()        # 但不重复升级
        trig.assert_not_called()

    async def test_escalate_without_superior_is_safe(self):
        agent = _make_agent([])
        agent._consecutive_errors = agent._CONSECUTIVE_ERROR_MAX
        agent._org.get_superior = AsyncMock(return_value=None)

        await agent._handle_safety_timeout()  # 不抛异常

        agent._inbox.mark_read_by_ids.assert_awaited_once()
        agent._inbox.send_message.assert_not_called()


# ── _handle_error: 同一计数路径 ──────────────────────────────


class TestHandleErrorUnifiedCounting:
    async def test_error_counts_and_resumes_below_limit(self):
        events: list = []
        agent = _make_agent(events)

        with patch(
            "hiveweave.services.event_audit.event_audit.log", AsyncMock()
        ):
            await agent._handle_error(ValueError("boom"))

        assert agent._consecutive_errors == 1
        assert agent._in_resume_cooldown() is True
        remaining = agent._resume_cooldown_until - time.monotonic()
        assert 0 < remaining <= ERROR_RESUME_COOLDOWN_S
        agent._inbox.mark_read_by_ids.assert_not_called()
        agent._conversation.append_turn.assert_not_called()
        assert agent._pending_resume_hint is not None
        assert "llm_error:ValueError" in agent._pending_resume_hint
        health = _health_events(events)
        assert health and health[-1]["health"] == "error"

    async def test_error_give_up_escalates(self):
        agent = _make_agent([])
        agent._consecutive_errors = agent._CONSECUTIVE_ERROR_MAX
        agent._org.get_superior = AsyncMock(return_value={"id": SUPERIOR_ID})

        with patch(
            "hiveweave.services.event_audit.event_audit.log", AsyncMock()
        ), patch(
            "hiveweave.agents.trigger.trigger_coordinator", AsyncMock()
        ) as trig:
            await agent._handle_error(ValueError("boom"))

        assert agent._consecutive_errors == agent._CONSECUTIVE_ERROR_MAX + 1
        assert agent._resume_suppressed is True
        agent._inbox.mark_read_by_ids.assert_awaited_once_with(
            AGENT_ID, ["inbox-1", "inbox-2"]
        )
        agent._conversation.append_turn.assert_not_called()
        agent._inbox.send_message.assert_awaited_once()
        kwargs = agent._inbox.send_message.await_args.kwargs
        assert "llm_error:ValueError" in kwargs["message"]
        trig.assert_awaited_once_with(SUPERIOR_ID)

    async def test_mixed_interruptions_share_counter(self):
        """doom/LLM 错误与安全超时共用同一计数，混合 streak 同样收口。"""
        agent = _make_agent([])
        agent._org.get_superior = AsyncMock(return_value={"id": SUPERIOR_ID})

        with patch(
            "hiveweave.services.event_audit.event_audit.log", AsyncMock()
        ), patch(
            "hiveweave.agents.trigger.trigger_coordinator", AsyncMock()
        ) as trig:
            await agent._handle_safety_timeout()          # 1
            _arm_pending(agent)
            await agent._handle_error(ValueError("x"))    # 2
            _arm_pending(agent)
            await agent._handle_safety_timeout()          # 3
            _arm_pending(agent)
            await agent._handle_error(ValueError("x"))    # 4 → 放弃

        assert agent._consecutive_errors == 4
        agent._inbox.mark_read_by_ids.assert_awaited_once()
        agent._inbox.send_message.assert_awaited_once()
        trig.assert_awaited_once_with(SUPERIOR_ID)


# ── 成功完成一轮 → 计数与冷却归零 ────────────────────────────


class TestSuccessResetsCounter:
    async def test_completion_resets_counter_and_cooldown(self):
        events: list = []
        agent = _make_agent(events)
        agent._consecutive_errors = 2
        agent._resume_cooldown_until = time.monotonic() + 90.0
        agent.pending_inbox_msg_ids = None
        # 预置合法 TurnResult，走 turn_exit 的 ok 分支
        set_pending_turn_result(
            AGENT_ID, {"phase": "done_slice", "summary": "turn done"}
        )

        result = {
            "status": "ok",
            "content": "done",
            "thinking": None,
            "tool_calls": [],
            "tool_turn_messages": [],
            "rounds": 1,
            "usage": None,
        }
        try:
            with patch(
                "hiveweave.services.task.TaskService.get_actionable_obligations",
                AsyncMock(return_value=[]),
            ), patch.object(
                Agent, "_maybe_self_retrigger", AsyncMock()
            ):
                await agent._handle_completion(result, "user msg", {})
        finally:
            clear_pending_turn_result(AGENT_ID)

        assert agent._consecutive_errors == 0
        assert agent._in_resume_cooldown() is False
        assert agent._resume_cooldown_until == 0.0
        assert agent._resume_suppressed is False
        assert agent.status == AgentState.IDLE
        health = _health_events(events)
        assert health and health[-1]["health"] == "ok"


class TestResumeHintEphemeral:
    async def test_build_messages_consumes_hint_once(self):
        agent = _make_agent([])
        agent._pending_resume_hint = "[RESUME CHECKPOINT]\nreason: test\n"
        agent._conversation.get_compacted_prefix = lambda *_a, **_k: None
        agent._conversation.get_history = AsyncMock(return_value=[])
        agent._get_identity_prompt = lambda: "identity"
        agent._get_context_window = lambda: 32_000
        agent._build_context_prompt = AsyncMock(return_value="")

        msgs = await agent._build_messages("hello", {})
        assert any(
            "[RESUME CHECKPOINT]" in (m.get("content") or "") for m in msgs
        )
        assert agent._pending_resume_hint is None

        msgs2 = await agent._build_messages("hello again", {})
        assert not any(
            "[RESUME CHECKPOINT]" in (m.get("content") or "") for m in msgs2
        )


class TestResumeSuppressedLatch:
    async def test_give_up_blocks_trigger_chat(self):
        agent = _make_agent([])
        agent.status = AgentState.IDLE
        agent._resume_suppressed = True
        agent._ensure_watcher_alive = lambda: None
        agent._lock = __import__("asyncio").Lock()

        with patch(
            "hiveweave.services.system_state.system_state.paused",
            return_value=False,
        ), patch(
            "hiveweave.db.meta.query_one",
            AsyncMock(return_value={"is_started": 1}),
        ):
            result = await agent.chat("trigger ctx", {"trigger": True})

        assert result.get("suppressed") is True
        assert agent.status == AgentState.IDLE

    async def test_user_chat_clears_suppressed(self):
        agent = _make_agent([])
        agent.status = AgentState.IDLE
        agent._resume_suppressed = True
        agent._ensure_watcher_alive = lambda: None
        agent._lock = __import__("asyncio").Lock()
        agent._chat_msg.update_streaming_messages_done = AsyncMock()
        agent._chat_msg.save_message = AsyncMock(
            return_value={"id": "msg-1"}
        )
        agent._broadcast_status = lambda *a, **k: None
        agent._start_safety_timer = lambda: None
        agent._run_llm = AsyncMock()

        with patch(
            "hiveweave.services.system_state.system_state.paused",
            return_value=False,
        ), patch(
            "hiveweave.db.meta.query_one",
            AsyncMock(return_value={"is_started": 1}),
        ), patch(
            "hiveweave.services.turn_session.clear_pending_turn_result",
        ):
            # chat will set PROCESSING and start llm task — mock create_task
            with patch("asyncio.create_task", return_value=None):
                result = await agent.chat("user says hi", {})

        assert agent._resume_suppressed is False
        assert result.get("ok") is True
        assert result.get("suppressed") is not True
