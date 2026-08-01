"""子代理（spawn_subagent）回归测试。"""
from __future__ import annotations

from hiveweave.llm.streamer.doom_loop import doom_loop_limit
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
    READWRITE_TOOLS,
)


def test_spawn_subagent_in_all_family_lists():
    for tools in (CEO_TOOLS, COORDINATOR_BUILDER_TOOLS, HR_TOOLS,
                  READONLY_TOOLS, READWRITE_TOOLS):
        assert "spawn_subagent" in tools, tools


def test_spawn_subagent_doom_bucket_tight():
    assert doom_loop_limit("spawn_subagent") == 3


from unittest.mock import AsyncMock, patch


def test_extend_elapsed_budget_shifts_started_at():
    from hiveweave.services.run_ledger import RunLedger

    ledger = RunLedger()
    execute = AsyncMock()
    with patch("hiveweave.services.run_ledger.project_db.execute", execute):
        # 不 await：execute 是 AsyncMock，直接调用返回 coroutine
        import asyncio
        asyncio.run(ledger.extend_elapsed_budget("a1", "r1", 240_000))

    sql = execute.await_args.args[1]
    assert "started_at = started_at - ?" in sql
    assert execute.await_args.args[2] == [240_000, "r1"]


import json

from unittest.mock import AsyncMock, MagicMock, patch

from hiveweave.services import turn_session
from hiveweave.tools.subagent import (
    _run_subagent,
    _subagent_on_tool_call,
)


def _fake_parent(*, include_write: bool = True) -> MagicMock:
    parent = MagicMock()
    parent.id = "exec-1"
    parent.project_id = "test-project"
    parent.config = {"name": "Exec1", "role": "executor",
                     "permission_type": "executor"}
    parent._current_run_id = None
    parent._run_ledger = AsyncMock()
    parent._memory = AsyncMock()
    parent._memory.build_project_context = AsyncMock(
        return_value="constitution: share work")
    parent._get_model_config = AsyncMock(return_value={
        "model_id": "m1", "base_url": "http://x", "api_key": "k"})
    base_defs = [
        {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
        for n in (["spawn_subagent", "commit_turn", "read_memory", "write_file"]
                  if include_write else
                  ["spawn_subagent", "commit_turn", "read_memory"])
    ]
    parent._get_tool_definitions = AsyncMock(return_value=base_defs)
    parent._get_workspace_path = AsyncMock(return_value="/ws/exec-1")
    return parent


def test_run_subagent_builds_fresh_context_and_returns_text():
    parent = _fake_parent()
    captured: dict = {}

    class FakeStreamer:
        def __init__(self, **kw):
            captured["max_tool_rounds"] = kw.get("max_tool_rounds")

        async def stream(self, **kw):
            captured["messages"] = kw["messages"]
            captured["tools"] = kw["tools"]
            return {"status": "ok", "content": "done the job",
                    "rounds": 1, "usage": {}, "end_turn": True}

    with patch("hiveweave.tools.subagent.Streamer", FakeStreamer):
        result = __import__("asyncio").run(
            _run_subagent(parent, "please refactor X", "refactor", 240))

    assert result["status"] == "ok"
    assert result["content"] == "done the job"
    assert captured["max_tool_rounds"] == 100
    # 上下文 = 身份 + 项目层 + 任务；工具 = 父工具 − spawn_subagent
    assert captured["tools"] == [
        {"type": "function", "function": {"name": "commit_turn", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "read_memory", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "write_file", "description": "", "parameters": {}}},
    ]
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system" and "subagent" in msgs[0]["content"]
    assert "constitution: share work" in msgs[1]["content"]
    assert msgs[2] == {"role": "user", "content": "please refactor X"}


def test_subagent_commit_intercepted_does_not_clobber_parent():
    parent = _fake_parent()
    executor = AsyncMock()
    callback = _subagent_on_tool_call(parent, executor, "/ws/exec-1", "/root")
    import asyncio
    result = asyncio.run(callback(
        "commit_turn",
        json.dumps({"phase": "done_slice", "summary": "refactored X"}),
        "tc-1",
    ))
    assert result["end_turn"] is True
    assert "TurnResult committed" in result["content"]
    executor.execute.assert_not_called()          # commit 不落 executor
    assert turn_session.get_pending_turn_result("exec-1") is None  # 父 pending 未被碰


def test_subagent_other_tools_forward_parent_agent_id():
    parent = _fake_parent()
    executor = AsyncMock()
    executor.execute.return_value = {"success": True, "output": "ok", "error": None}
    callback = _subagent_on_tool_call(parent, executor, "/ws/exec-1", "/root")
    import asyncio
    result = asyncio.run(callback(
        "read_memory", json.dumps({"agentId": "exec-1"}), "tc-2"))
    assert result["role"] == "tool"
    assert result["content"] == "ok"
    call = executor.execute.await_args
    assert call.args[0] == "exec-1"   # 父 agent_id 转发（权限继承）
    assert call.args[3] == "/ws/exec-1"


def test_run_subagent_timeout_returns_error():
    parent = _fake_parent()

    class SlowStreamer:
        def __init__(self, **kw):
            pass

        async def stream(self, **kw):
            await __import__("asyncio").sleep(5)
            return {"status": "ok", "content": "late"}

    with patch("hiveweave.tools.subagent.Streamer", SlowStreamer):
        import asyncio
        result = asyncio.run(_run_subagent(parent, "x", "y", 0.05))
    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_subagent_commit_rejects_empty_summary():
    parent = _fake_parent()
    executor = AsyncMock()
    callback = _subagent_on_tool_call(parent, executor, "/ws/exec-1", "/root")
    import asyncio
    result = asyncio.run(callback(
        "commit_turn", json.dumps({"phase": "done_slice", "summary": "  "}),
        "tc-3"))
    assert result["end_turn"] is not True
    assert "summary required" in result["content"]


def test_run_subagent_commit_summary_isolated_per_spawn():
    """审计修复：commit 摘要按 spawn 隔离 —— 未提交的子代理不带 [commit] 标注。"""
    parent = _fake_parent()
    state = {"do_commit": False}

    class FakeStreamer:
        def __init__(self, **kw):
            pass

        async def stream(self, **kw):
            if state["do_commit"]:
                await kw["on_tool_call"](
                    "commit_turn",
                    json.dumps({"phase": "done_slice", "summary": "refactored X"}),
                    "tc-x",
                )
            return {"status": "ok", "content": "done the job",
                    "rounds": 1, "usage": {}, "end_turn": True}

    import asyncio
    with patch("hiveweave.tools.subagent.Streamer", FakeStreamer):
        state["do_commit"] = True
        committed = asyncio.run(_run_subagent(parent, "task A", "a", 240))
        state["do_commit"] = False
        plain = asyncio.run(_run_subagent(parent, "task B", "b", 240))

    assert "[commit] done_slice: refactored X" in committed["content"]
    assert "[commit]" not in plain["content"]
