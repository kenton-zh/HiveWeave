"""46 轮 #2 主循环重试缝：上游类 stream error 在 _run_llm 不崩且重试一次。

审计 C1 回归锁：status=error 的流结果曾因重试缝读取未初始化局部量
（UnboundLocalError）把全部 LLM 错误路径打进 llm_task_crashed。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.test_agent_interruption_counting import _make_agent


class _FakeStreamerFactory:
    """让 `Streamer(max_tool_rounds=…)` 构造返回脚本化 stream 的桩。"""

    def __init__(self, outcomes: list[dict]):
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self, **kw):
        outer = self

        class _S:
            async def stream(self, **kw2):
                idx = min(outer.calls, len(outer.outcomes) - 1)
                outer.calls += 1
                return outer.outcomes[idx]

        return _S()


_ERROR_IDLE = {
    "status": "error",
    "content": "",
    "thinking": "",
    "tool_calls": [],
    "error": "Stream idle timeout (75s)",
    "error_status": None,
    "usage_rounds": [],
}

_OK = {"status": "ok", "content": "recovered", "thinking": "",
       "tool_calls": [], "usage_rounds": []}


def _prepared_agent():
    agent = _make_agent()
    agent._build_messages = AsyncMock(
        return_value=[{"role": "user", "content": "hi"}]
    )
    agent._get_model_config = AsyncMock(
        return_value={"model_id": "m", "provider_type": "fake"}
    )
    agent._start_heartbeat = lambda: None
    agent._heartbeat_task = None
    agent._settle_steer_channel = AsyncMock()
    agent._handle_completion = AsyncMock()
    agent._handle_error = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_upstream_stream_error_retried_once_then_ok(monkeypatch):
    """idle/EOF 类 stream error → 退避重试一次后成功，不落错误治理。"""
    factory = _FakeStreamerFactory([dict(_ERROR_IDLE), dict(_OK)])
    agent = _prepared_agent()

    monkeypatch.setattr("hiveweave.agents.agent.Streamer", factory)
    monkeypatch.setattr(
        "hiveweave.agents.agent.compute_backoff",
        lambda attempt, retry_after_ms=None: 0,
    )
    await agent._run_llm("user says hi", {}, interrupted_run_id=None)

    assert factory.calls == 2  # 首撞 + 重试
    agent._handle_error.assert_not_awaited()
    agent._handle_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_upstream_error_not_retried(monkeypatch):
    """非上游类（如权限/未知错误）不进重试缝——一次即落错误治理。"""
    factory = _FakeStreamerFactory([
        {"status": "error", "content": "", "thinking": "", "tool_calls": [],
         "error": "permission denied: write outside workspace",
         "error_status": None, "usage_rounds": []},
    ])
    agent = _prepared_agent()

    monkeypatch.setattr("hiveweave.agents.agent.Streamer", factory)
    await agent._run_llm("user says hi", {}, interrupted_run_id=None)

    assert factory.calls == 1  # 不重试
    agent._handle_error.assert_awaited_once()  # 落既有错误治理
