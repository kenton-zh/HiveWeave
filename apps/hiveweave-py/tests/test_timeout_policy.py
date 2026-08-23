"""Declared-tool timeouts; turn budget always on; background bash unbounded."""
from __future__ import annotations

import asyncio

import pytest

from hiveweave.llm.streamer.constants import (
    FIRST_CHUNK_TIMEOUT_S,
    HARD_TOTAL_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    STREAM_SOCKET_READ_TIMEOUT_S,
    TOTAL_TIMEOUT_S,
    stream_chunk_wait_s,
)
from hiveweave.llm.streamer.tool_exec import ToolExecMixin
from hiveweave.services.offturn import (
    kill_offturn_job,
    reset_offturn_for_tests,
    start_offturn_job,
)
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    READWRITE_TOOLS,
)
from hiveweave.tools.timeout_policy import (
    UNDECLARED_SESSION_TOOLS,
    declared_timeout_s,
)


def test_turn_budget_constants_structurally_sane():
    """Turn 预算写死启用（DSH_11 复盘）：软 < 硬 < agent SAFETY(600s)。"""
    assert TOTAL_TIMEOUT_S < HARD_TOTAL_TIMEOUT_S < 600.0


def test_idle_watchdog_default_is_five_minutes():
    assert IDLE_TIMEOUT_S == 300.0
    assert stream_chunk_wait_s(got_event=False) == FIRST_CHUNK_TIMEOUT_S
    assert stream_chunk_wait_s(got_event=True) == IDLE_TIMEOUT_S
    assert STREAM_SOCKET_READ_TIMEOUT_S > IDLE_TIMEOUT_S


def test_job_kill_visible_to_builders_not_ceo():
    assert "job_kill" in COORDINATOR_BUILDER_TOOLS
    assert "job_kill" in READWRITE_TOOLS
    assert "job_kill" not in CEO_TOOLS
    assert "spawn_subagent" not in CEO_TOOLS


def test_bash_read_write_edit_undeclared():
    for name in (
        "bash", "run_command", "read_file", "write_file",
        "edit_file", "apply_patch", "spawn_subagent",
    ):
        assert name in UNDECLARED_SESSION_TOOLS
        assert declared_timeout_s(name) is None


def test_webfetch_declares_cooperative_timeout():
    assert declared_timeout_s("webfetch") == 30.0
    assert declared_timeout_s("websearch") == 15.0
    assert declared_timeout_s("question") == 200.0


@pytest.mark.asyncio
async def test_undeclared_tool_not_wait_for_wrapped():
    te = ToolExecMixin()
    called = []

    async def ok(name, arguments, tool_call_id):
        called.append(name)
        return {"content": "ok"}

    result = await te._execute_single_tool(
        "a1",
        {"id": "t1", "name": "read_file", "arguments": "{}"},
        ok,
        budget_s=0.01,
    )
    assert result["content"] == "ok"
    assert called == ["read_file"]


@pytest.mark.asyncio
async def test_declared_tool_times_out():
    te = ToolExecMixin()

    async def slow(name, arguments, tool_call_id):
        await asyncio.sleep(2)
        return {"content": "late"}

    import hiveweave.tools.timeout_policy as tp

    original = tp.DECLARED_TIMEOUT_S.get("webfetch")
    tp.DECLARED_TIMEOUT_S["webfetch"] = 0.05
    try:
        result = await te._execute_single_tool(
            "a1",
            {"id": "t1", "name": "webfetch", "arguments": "{}"},
            slow,
        )
    finally:
        if original is None:
            tp.DECLARED_TIMEOUT_S.pop("webfetch", None)
        else:
            tp.DECLARED_TIMEOUT_S["webfetch"] = original
    assert "[Tool Timeout]" in result["content"]
    assert "timed out after" in result["content"]


@pytest.mark.asyncio
async def test_kill_offturn_job_cancels_live_work():
    await reset_offturn_for_tests()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _work():
        started.set()
        try:
            await asyncio.sleep(30)
            return True, "done"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    jid = start_offturn_job(
        kind="bash",
        agent_id="agent-1",
        project_id="proj-1",
        worktree="/tmp",
        work=_work,
        wake_on_complete=False,
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    result = await kill_offturn_job(jid, agent_id="agent-1")
    assert result.get("ok") is True
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    await reset_offturn_for_tests()


@pytest.mark.asyncio
async def test_kill_offturn_job_rejects_other_agent():
    await reset_offturn_for_tests()

    async def _work():
        await asyncio.sleep(30)
        return True, "done"

    jid = start_offturn_job(
        kind="bash",
        agent_id="owner",
        project_id="proj-1",
        worktree="/tmp",
        work=_work,
        wake_on_complete=False,
    )
    result = await kill_offturn_job(jid, agent_id="intruder")
    assert result.get("ok") is False
    await reset_offturn_for_tests()


@pytest.mark.asyncio
async def test_unbounded_bash_does_not_clamp_timeout(monkeypatch, tmp_path):
    # P3 默认 on：本测试测超时钳制（native 路径），显式关沙箱
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    captured: dict = {}

    async def fake_native(command, cwd, timeout_s):
        captured["timeout_s"] = timeout_s
        return {
            "output": "ok",
            "stdout": "ok",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "error": None,
        }

    monkeypatch.setattr("hiveweave.tools.bash._run_native", fake_native)
    from hiveweave.tools.bash import execute_bash

    result = await execute_bash(
        command="echo hi",
        workdir="",
        workspace_path=str(tmp_path),
        timeout_ms=0,
        unbounded=True,
    )
    assert captured["timeout_s"] == 0
    assert result["success"] is True

    captured.clear()
    result = await execute_bash(
        command="echo hi",
        workdir="",
        workspace_path=str(tmp_path),
        timeout_ms=1000,
    )
    assert captured["timeout_s"] == 5
    assert result["success"] is True


def test_socket_timeout_after_tokens_is_permanent():
    from hiveweave.llm.retry import PermanentError, RetryableError
    from hiveweave.llm.streamer.http_stream import classify_stream_socket_timeout

    first = classify_stream_socket_timeout(got_event=False)
    later = classify_stream_socket_timeout(got_event=True)
    assert isinstance(first, RetryableError)
    assert isinstance(later, PermanentError)


def test_bash_timeout_copy_ignored_when_background():
    from hiveweave.tools.executor import get_tool_schema_for_llm

    schema = get_tool_schema_for_llm("bash")
    desc = schema["properties"]["timeout"]["description"]
    assert "Ignored when background=true" in desc
    assert "Foreground only" in desc


def test_ceo_hard_deny_spawn_subagent():
    from hiveweave.services.policy import tool_hard_deny

    deny = tool_hard_deny(
        {"role": "ceo", "permission_type": "coordinator"},
        "spawn_subagent",
    )
    assert deny is not None


@pytest.mark.asyncio
async def test_idle_timeout_cancels_abandoned_httpx_executor(monkeypatch):
    from types import SimpleNamespace

    from hiveweave.llm.retry import RetryableError
    from hiveweave.llm.streamer.http_stream import HttpStreamMixin

    hanging = asyncio.get_running_loop().create_future()
    loop = asyncio.get_running_loop()

    def fake_rie(executor, fn, *args):
        return hanging

    monkeypatch.setattr(loop, "run_in_executor", fake_rie)
    monkeypatch.setattr(
        "hiveweave.llm.streamer.http_stream.stream_chunk_wait_s",
        lambda *, got_event: 0.05,
    )
    mixin = HttpStreamMixin()

    async def _noop_delta(*_a, **_k):
        return None

    mixin._fire_delta = _noop_delta
    provider = SimpleNamespace(
        extract_usage=lambda _e: None,
        parse_stream_chunk=lambda _e: [],
    )
    with pytest.raises(RetryableError, match="First chunk timeout"):
        await mixin._do_streaming_request(
            agent_id="a1",
            provider=provider,
            url="http://example.invalid",
            headers={},
            body={},
            on_delta=None,
            delta_id="d1",
            round_num=1,
            budget_deadline=None,
        )
    assert hanging.cancelled()


@pytest.mark.asyncio
async def test_llm_queue_ping_while_waiting_on_semaphore(monkeypatch):
    from types import SimpleNamespace

    from hiveweave.llm.streamer.http_stream import HttpStreamMixin

    held = asyncio.Semaphore(0)
    monkeypatch.setattr(
        "hiveweave.llm.streamer.http_stream._get_llm_semaphore",
        lambda: held,
    )
    monkeypatch.setattr(
        "hiveweave.llm.streamer.http_stream.LLM_QUEUE_PING_S",
        0.05,
    )

    class _H(HttpStreamMixin):
        def __init__(self) -> None:
            self.events: list[dict] = []

            async def wr(fn):
                return await fn()

            self._retry_handler = SimpleNamespace(with_retry=wr)

            async def _ok(*_a, **_k):
                return None

            self._circuit_breaker = SimpleNamespace(
                report_success=_ok,
                report_failure=_ok,
            )

        async def _fire_delta(self, _on_delta, event):
            self.events.append(event)

    h = _H()
    provider = SimpleNamespace(
        build_url=lambda: "http://example.invalid/v1",
        build_headers=lambda: {},
        build_body=lambda **_k: {"model": "x"},
    )
    task = asyncio.create_task(
        h._stream_single_round(
            agent_id="a1",
            provider=provider,
            provider_name="openai",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            on_delta=None,
            round_num=1,
            delta_id="d1",
            budget_deadline=None,
        )
    )
    await asyncio.sleep(0.2)
    assert any(e.get("type") == "llm_queue" for e in h.events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
