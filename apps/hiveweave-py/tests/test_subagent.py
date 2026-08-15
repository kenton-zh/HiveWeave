"""子代理（spawn_subagent）回归测试。"""
from __future__ import annotations

from hiveweave.llm.streamer.doom_loop import DOOM_LOOP_TOOL_LIMITS, doom_loop_limit
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
    READWRITE_TOOLS,
)


def test_spawn_subagent_in_builder_lists_not_ceo_hr():
    assert "spawn_subagent" not in CEO_TOOLS
    assert "spawn_subagent" not in HR_TOOLS
    for tools in (COORDINATOR_BUILDER_TOOLS, READONLY_TOOLS, READWRITE_TOOLS):
        assert "spawn_subagent" in tools, tools


def test_spawn_subagent_doom_bucket_tight():
    assert DOOM_LOOP_TOOL_LIMITS["spawn_subagent"] == 3
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


def _fake_parent_rich(
    *,
    role: str = "executor",
    permission_type: str = "executor",
    tool_names: list[str] | None = None,
    workspace: str = "/ws/exec-1",
) -> MagicMock:
    """父 fixture：可指定角色与工具列表，供子代理类型测试用。

    tool_names 默认覆盖三类型白名单的并集 + 几个白名单外工具
    （spawn_subagent / dispatch_task / attest_doc_review / hire_agent），
    用来同时验证“白名单内保留 / 白名单外丢弃 / spawn_subagent 永不出现”。
    """
    parent = MagicMock()
    parent.id = "exec-1"
    parent.project_id = "test-project"
    parent.config = {
        "name": role.capitalize(),
        "role": role,
        "permission_type": permission_type,
    }
    parent._current_run_id = None
    parent._run_ledger = AsyncMock()
    parent._memory = AsyncMock()
    parent._memory.build_project_context = AsyncMock(
        return_value="constitution: share work")
    parent._get_model_config = AsyncMock(return_value={
        "model_id": "m1", "base_url": "http://x", "api_key": "k"})
    if tool_names is None:
        from hiveweave.tools.subagent import _SUBAGENT_TYPE_TOOLS
        all_white: set[str] = set()
        for s in _SUBAGENT_TYPE_TOOLS.values():
            all_white |= s
        tool_names = sorted(all_white) + [
            "spawn_subagent",        # 深度 1 硬门
            "dispatch_task",         # 父有但白名单无
            "attest_doc_review",     # 出证据归父
            "waive_attestation",     # 豁免归父
            "hire_agent",            # staffing 归父
        ]
    parent._get_tool_definitions = AsyncMock(return_value=[
        {"type": "function",
         "function": {"name": n, "description": "", "parameters": {}}}
        for n in tool_names
    ])
    parent._get_workspace_path = AsyncMock(return_value=workspace)
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
            _run_subagent(parent, "please refactor X", "refactor", 240, "write"))

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
        result = asyncio.run(_run_subagent(parent, "x", "y", 0.05, "readonly"))
    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_run_subagent_unbounded_has_no_wall_clock():
    parent = _fake_parent()

    class OkStreamer:
        def __init__(self, **kw):
            pass

        async def stream(self, **kw):
            await __import__("asyncio").sleep(0.05)
            return {"status": "ok", "content": "done"}

    with patch("hiveweave.tools.subagent.Streamer", OkStreamer):
        import asyncio
        result = asyncio.run(_run_subagent(parent, "x", "y", None, "readonly"))
    assert result["status"] == "ok"
    assert result["content"] == "done"


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
        committed = asyncio.run(_run_subagent(parent, "task A", "a", 240, "write"))
        state["do_commit"] = False
        plain = asyncio.run(_run_subagent(parent, "task B", "b", 240, "readonly"))

    assert "[commit] done_slice: refactored X" in committed["content"]
    assert "[commit]" not in plain["content"]


# ──────────────────────────────────────────────────────────────────────────
# 子代理类型预制工具集（2026-08-01 设计）
# spec: docs/superpowers/specs/2026-08-01-subagent-tool-profiles-design.md
# ──────────────────────────────────────────────────────────────────────────

from hiveweave.tools.subagent import (
    _SUBAGENT_TYPE_TOOLS,
    _VALID_SUBAGENT_TYPES,
    _parent_has_source_write,
    _subagent_identity,
    spawn_subagent_tool,
    SpawnSubagentParams,
)


def test_subagent_type_whitelist_readonly_shape():
    """§6.1 readonly：有 read_file/grep；无 write_file/bash/browse/dispatch_task。"""
    s = _SUBAGENT_TYPE_TOOLS["readonly"]
    assert "read_file" in s and "grep" in s and "commit_turn" in s
    for forbidden in ("write_file", "edit_file", "bash", "run_tests",
                      "browse", "dispatch_task", "attest_doc_review",
                      "waive_attestation", "git_worktree_merge"):
        assert forbidden not in s, f"readonly leaked {forbidden}"


def test_subagent_type_whitelist_audit_shape():
    """§6.1 audit：有 bash/run_tests/browse；无 write_file/edit_file/dispatch_task/attest。"""
    s = _SUBAGENT_TYPE_TOOLS["audit"]
    for required in ("bash", "run_tests", "browse", "claim_task",
                     "submit_task", "request_review", "read_file"):
        assert required in s, f"audit missing {required}"
    for forbidden in ("write_file", "edit_file", "apply_patch",
                      "dispatch_task", "create_task", "review_task",
                      "attest_doc_review", "waive_attestation",
                      "git_worktree_merge"):
        assert forbidden not in s, f"audit leaked {forbidden}"


def test_subagent_type_whitelist_write_shape():
    """§6.1 write：有 write_file/edit_file/git list/status/checkpoint；无 merge/remove/browse/attest/dispatch。"""
    s = _SUBAGENT_TYPE_TOOLS["write"]
    for required in ("write_file", "edit_file", "apply_patch",
                     "bash", "run_command", "run_tests",
                     "git_worktree_status", "git_worktree_checkpoint",
                     "git_worktree_list", "claim_task", "submit_task"):
        assert required in s, f"write missing {required}"
    for forbidden in ("browse", "attest_doc_review", "waive_attestation",
                      "dispatch_task", "create_task", "review_task",
                      "hire_agent", "git_worktree_merge", "git_worktree_remove"):
        assert forbidden not in s, f"write leaked {forbidden}"


def test_subagent_tools_never_include_spawn_subagent():
    """§6.2 深度 1 硬门：三类型白名单本身都不含 spawn_subagent。"""
    for t, s in _SUBAGENT_TYPE_TOOLS.items():
        assert "spawn_subagent" not in s, f"{t} whitelist contains spawn_subagent"


def test_subagent_tool_filtering_drops_unknown_and_spawn():
    """§6.6 父 defs 含白名单外工具 + spawn_subagent → 子代理工具列表不含。"""
    parent = _fake_parent_rich()
    captured: dict = {}

    class FakeStreamer:
        def __init__(self, **kw):
            pass

        async def stream(self, **kw):
            captured["tools"] = kw["tools"]
            captured["messages"] = kw["messages"]
            return {"status": "ok", "content": "x",
                    "rounds": 1, "usage": {}, "end_turn": True}

    import asyncio
    with patch("hiveweave.tools.subagent.Streamer", FakeStreamer):
        asyncio.run(_run_subagent(parent, "do X", "x", 240, "audit"))

    names = [t["function"]["name"] for t in captured["tools"]]
    # 白名单内的保留
    assert "commit_turn" in names and "bash" in names and "browse" in names
    # 白名单外 + spawn_subagent 全部丢弃
    for dropped in ("spawn_subagent", "dispatch_task", "attest_doc_review",
                    "waive_attestation", "hire_agent"):
        assert dropped not in names, f"audit leaked {dropped}"
    # audit 不含写码工具
    assert "write_file" not in names and "edit_file" not in names


def test_write_subagent_tool_filtering_keeps_write_tools():
    """write 类型：父 defs 含写码/git_worktree → 子代理保留；browse 丢弃。"""
    parent = _fake_parent_rich()
    captured: dict = {}

    class FakeStreamer:
        def __init__(self, **kw):
            pass

        async def stream(self, **kw):
            captured["tools"] = kw["tools"]
            return {"status": "ok", "content": "x",
                    "rounds": 1, "usage": {}, "end_turn": True}

    import asyncio
    with patch("hiveweave.tools.subagent.Streamer", FakeStreamer):
        asyncio.run(_run_subagent(parent, "do X", "x", 240, "write"))

    names = [t["function"]["name"] for t in captured["tools"]]
    assert "write_file" in names and "edit_file" in names
    assert "bash" in names and "run_tests" in names  # write 能自测自改
    assert "git_worktree_list" in names
    assert "git_worktree_merge" not in names
    assert "git_worktree_remove" not in names
    assert "browse" not in names  # write 不给 browse
    assert "attest_doc_review" not in names
    assert "spawn_subagent" not in names


def test_parent_has_source_write_by_family():
    """§6.3 executor/coordinator/qa 有 SOURCE_WRITE；CEO/HR 无。"""
    exec_parent = _fake_parent_rich(role="executor", permission_type="executor")
    coord_parent = _fake_parent_rich(role="coordinator", permission_type="coordinator")
    qa_parent = _fake_parent_rich(role="test_engineer", permission_type="executor")
    ceo_parent = _fake_parent_rich(role="ceo", permission_type="ceo")
    hr_parent = _fake_parent_rich(role="HR", permission_type="readonly")

    assert _parent_has_source_write(exec_parent) is True
    assert _parent_has_source_write(coord_parent) is True
    assert _parent_has_source_write(qa_parent) is True
    assert _parent_has_source_write(ceo_parent) is False
    assert _parent_has_source_write(hr_parent) is False


def test_spawn_subagent_rejects_missing_subagent_type():
    """§6.4 缺省 subagent_type → err，不进 _run_subagent。"""
    parent = _fake_parent()
    params = SpawnSubagentParams(subagent_type="", prompt="do something")
    with patch("hiveweave.agents.supervisor.agent_manager") as am:
        am.get_agent.return_value = parent
        import asyncio
        result = asyncio.run(spawn_subagent_tool(
            params, agent_id="exec-1", workspace="/ws"))
    assert result.success is False
    assert "subagent_type" in (result.error or "")
    # 父未被推进（没碰 _run_ledger / safety timer）
    parent._run_ledger.extend_elapsed_budget.assert_not_called()


def test_spawn_subagent_rejects_invalid_subagent_type():
    """§6.5 非法值 → err。"""
    parent = _fake_parent()
    params = SpawnSubagentParams(subagent_type="foo", prompt="do something")
    with patch("hiveweave.agents.supervisor.agent_manager") as am:
        am.get_agent.return_value = parent
        import asyncio
        result = asyncio.run(spawn_subagent_tool(
            params, agent_id="exec-1", workspace="/ws"))
    assert result.success is False
    assert "subagent_type" in (result.error or "")


def test_spawn_subagent_rejects_write_for_ceo_parent():
    """§6.3 CEO 父选 write → err 且含 SOURCE_WRITE 提示文案。"""
    ceo_parent = _fake_parent_rich(role="ceo", permission_type="ceo")
    params = SpawnSubagentParams(
        subagent_type="write", prompt="refactor the module")
    with patch("hiveweave.agents.supervisor.agent_manager") as am:
        am.get_agent.return_value = ceo_parent
        import asyncio
        result = asyncio.run(spawn_subagent_tool(
            params, agent_id="ceo-1", workspace="/ws"))
    assert result.success is False
    msg = result.error or ""
    assert "SOURCE_WRITE" in msg or "code-writing parent" in msg
    assert "CEO" in msg or "HR" in msg


def test_spawn_subagent_rejects_write_for_hr_parent():
    """§6.3 HR 父选 write → err。"""
    hr_parent = _fake_parent_rich(role="HR", permission_type="readonly")
    params = SpawnSubagentParams(
        subagent_type="write", prompt="refactor the module")
    with patch("hiveweave.agents.supervisor.agent_manager") as am:
        am.get_agent.return_value = hr_parent
        import asyncio
        result = asyncio.run(spawn_subagent_tool(
            params, agent_id="hr-1", workspace="/ws"))
    assert result.success is False
    assert "SOURCE_WRITE" in (result.error or "")


_WRITE_ROOT = "/proj"
_WRITE_TREE = "/proj/.hiveweave/worktrees/exec-1"


def _patch_write_tree_root():
    return patch(
        "hiveweave.db.meta.get_project_workspace",
        AsyncMock(return_value=_WRITE_ROOT),
    )


def test_spawn_subagent_allows_write_for_executor_parent():
    """§6.3 executor 父选 write → 立即返回 waiting_on（off-turn，不嵌套等待）。"""
    parent = _fake_parent()
    parent._get_workspace_path = AsyncMock(return_value=_WRITE_TREE)
    params = SpawnSubagentParams(
        subagent_type="write", prompt="refactor X", description="refactor")

    async def fake_run(*_a, **_k):
        return {"status": "ok", "content": "done",
                "rounds": 1, "usage": {}, "end_turn": True}

    import asyncio
    from hiveweave.services.offturn import is_live_job, reset_offturn_for_tests

    async def _go():
        await reset_offturn_for_tests()
        with patch("hiveweave.agents.supervisor.agent_manager") as am:
            am.get_agent.return_value = parent
            with _patch_write_tree_root():
                with patch("hiveweave.tools.subagent._run_subagent", fake_run):
                    result = await spawn_subagent_tool(
                        params, agent_id="exec-1", workspace=_WRITE_TREE)
        assert result.success is True
        assert "waiting" in (result.output or "").lower()
        job_id = result.extra["job_id"]
        assert job_id.startswith("bg-sub-")
        assert result.extra["waiting_on"][0]["ref"] == job_id
        parent._run_ledger.extend_elapsed_budget.assert_not_called()
        for _ in range(50):
            if not is_live_job(job_id):
                break
            await asyncio.sleep(0.02)
        await reset_offturn_for_tests()
        return result

    asyncio.run(_go())


def test_spawn_write_succeeds_with_only_role_type():
    """Restart remap: AgentManager 只有 role_type=executor 时 write spawn 仍放行。"""
    parent = _fake_parent()
    parent.config = {
        "name": "Exec1",
        "role": "签到排行榜工程师",
        "role_type": "executor",
    }
    parent._get_workspace_path = AsyncMock(return_value=_WRITE_TREE)
    params = SpawnSubagentParams(
        subagent_type="write", prompt="refactor X")

    async def fake_run(*_a, **_k):
        return {"status": "ok", "content": "done"}

    import asyncio
    from hiveweave.services.offturn import reset_offturn_for_tests

    async def _go():
        await reset_offturn_for_tests()
        with patch("hiveweave.agents.supervisor.agent_manager") as am:
            am.get_agent.return_value = parent
            with _patch_write_tree_root():
                with patch("hiveweave.tools.subagent._run_subagent", fake_run):
                    result = await spawn_subagent_tool(
                        params, agent_id="exec-1", workspace=_WRITE_TREE)
        await reset_offturn_for_tests()
        return result

    result = asyncio.run(_go())
    assert result.success is True
    assert result.extra["job_id"].startswith("bg-sub-")


def test_spawn_write_rejects_project_main():
    """write spawn on MAIN is fail-closed (must stay on the write worktree)."""
    parent = _fake_parent()
    parent._get_workspace_path = AsyncMock(return_value=_WRITE_ROOT)
    params = SpawnSubagentParams(subagent_type="write", prompt="refactor X")
    import asyncio

    async def _go():
        with patch("hiveweave.agents.supervisor.agent_manager") as am:
            am.get_agent.return_value = parent
            with _patch_write_tree_root():
                with patch("hiveweave.tools.subagent._run_subagent") as run:
                    result = await spawn_subagent_tool(
                        params, agent_id="exec-1", workspace=_WRITE_ROOT)
                    run.assert_not_called()
        return result

    result = asyncio.run(_go())
    assert result.success is False
    err = result.error or ""
    assert "MAIN" in err or "write worktree" in err


def test_spawn_write_rejects_missing_project_root():
    """Missing project root must not fail open onto MAIN."""
    parent = _fake_parent()
    parent._get_workspace_path = AsyncMock(return_value=_WRITE_TREE)
    params = SpawnSubagentParams(subagent_type="write", prompt="refactor X")
    import asyncio

    async def _go():
        with patch("hiveweave.agents.supervisor.agent_manager") as am:
            am.get_agent.return_value = parent
            with patch(
                "hiveweave.db.meta.get_project_workspace",
                AsyncMock(return_value=None),
            ):
                with patch("hiveweave.tools.subagent._run_subagent") as run:
                    result = await spawn_subagent_tool(
                        params, agent_id="exec-1", workspace=_WRITE_TREE)
                    run.assert_not_called()
        return result

    result = asyncio.run(_go())
    assert result.success is False
    assert "project root" in (result.error or "")


def test_spawn_write_rejects_main_subdirectory():
    """MAIN subdirectory is not a write worktree."""
    parent = _fake_parent()
    sub = "/proj/src"
    parent._get_workspace_path = AsyncMock(return_value=sub)
    params = SpawnSubagentParams(subagent_type="write", prompt="refactor X")
    import asyncio

    async def _go():
        with patch("hiveweave.agents.supervisor.agent_manager") as am:
            am.get_agent.return_value = parent
            with _patch_write_tree_root():
                with patch("hiveweave.tools.subagent._run_subagent") as run:
                    result = await spawn_subagent_tool(
                        params, agent_id="exec-1", workspace=sub)
                    run.assert_not_called()
        return result

    result = asyncio.run(_go())
    assert result.success is False
    assert "write worktree" in (result.error or "")


def test_parent_has_source_write_role_type_alias():
    """Restart 后 config 仅有 role_type 时与 permission_type 同口径。"""
    parent = _fake_parent_rich(role="executor", permission_type="executor")
    parent.config.pop("permission_type", None)
    parent.config["role_type"] = "executor"
    assert _parent_has_source_write(parent) is True

    ceo = _fake_parent_rich(role="ceo", permission_type="ceo")
    ceo.config.pop("permission_type", None)
    ceo.config["role_type"] = "ceo"
    assert _parent_has_source_write(ceo) is False


def test_identity_includes_workspace_path():
    """§6.7 身份提示包含工作区路径。"""
    parent = _fake_parent()
    identity = _subagent_identity(
        parent, description="refactor", timeout_s=240,
        subagent_type="write", workspace="/ws/exec-1")
    assert "/ws/exec-1" in identity
    assert "write" in identity  # 类型出现在身份首行
    assert "continues its org turn" in identity
    assert "not blocked waiting" in identity
    assert "The parent is waiting for you" not in identity
    assert "will kill you" in identity


def test_identity_unbounded_has_no_kill_deadline():
    parent = _fake_parent()
    identity = _subagent_identity(
        parent, description="refactor", timeout_s=None,
        subagent_type="write", workspace="/ws/exec-1")
    assert "will kill you" not in identity
    assert "No session wall clock" in identity


def test_identity_includes_workspace_for_all_types():
    """§6.7 三类型身份提示都含 workspace（不只是 write）。"""
    parent = _fake_parent()
    for t in _VALID_SUBAGENT_TYPES:
        identity = _subagent_identity(
            parent, description="x", timeout_s=240,
            subagent_type=t, workspace="/ws/exec-1")
        assert "/ws/exec-1" in identity, f"{t} identity missing workspace"


def test_identity_states_no_attestation_for_subagent():
    """身份提示明确告知子代理不出 attest（证据归父）。"""
    parent = _fake_parent()
    identity = _subagent_identity(
        parent, description="x", timeout_s=240,
        subagent_type="audit", workspace="/ws")
    assert "attest" in identity.lower() or "evidence" in identity.lower()


# ──────────────────────────────────────────────────────────────────────────
# 第二轮审计修复（R1 白名单深度防御 + R2 QA 语义一致性）
# ──────────────────────────────────────────────────────────────────────────

from hiveweave.tools.subagent import _SUBAGENT_TYPE_TOOLS


def test_subagent_callback_rejects_whitelist_foreign_tool():
    """R1: 白名单外的 tool_name 直接拒绝，不转发给 executor。

    防止 LLM 幻觉/注入白名单外工具被父权限执行越权（如 readonly 子代理
    调 hire_agent，父是 HR 有 STAFFING → 会通过）。
    """
    parent = _fake_parent()
    executor = AsyncMock()
    # readonly 白名单不含 hire_agent
    readonly_wl = _SUBAGENT_TYPE_TOOLS["readonly"]
    assert "hire_agent" not in readonly_wl
    callback = _subagent_on_tool_call(
        parent, executor, "/ws", "/root", None, readonly_wl)
    import asyncio
    result = asyncio.run(callback(
        "hire_agent", json.dumps({"role": "x"}), "tc-x"))
    assert "whitelist" in result["content"]
    executor.execute.assert_not_called()  # 没转发给 executor


def test_subagent_callback_allows_whitelisted_tool():
    """R1: 白名单内的 tool_name 正常转发给 executor。"""
    parent = _fake_parent()
    executor = AsyncMock()
    executor.execute.return_value = {"success": True, "output": "ok"}
    audit_wl = _SUBAGENT_TYPE_TOOLS["audit"]
    assert "bash" in audit_wl
    callback = _subagent_on_tool_call(
        parent, executor, "/ws", "/root", None, audit_wl)
    import asyncio
    result = asyncio.run(callback(
        "bash", json.dumps({"command": "ls"}), "tc-y"))
    assert result["content"] == "ok"
    executor.execute.assert_awaited_once()


def test_subagent_callback_rejects_background_bash():
    """Off-turn bash 归父；子代理内 background=true 必须拒绝，避免 [BASH DONE] 误叫醒。"""
    parent = _fake_parent()
    executor = AsyncMock()
    audit_wl = _SUBAGENT_TYPE_TOOLS["audit"]
    callback = _subagent_on_tool_call(
        parent, executor, "/ws", "/root", None, audit_wl)
    import asyncio
    result = asyncio.run(callback(
        "bash", json.dumps({"command": "pytest", "background": True}), "tc-bg"))
    assert "background=true" in result["content"]
    executor.execute.assert_not_called()


def test_subagent_callback_commit_turn_bypasses_whitelist():
    """R1: commit_turn 走本地拦截，不受白名单校验影响。"""
    parent = _fake_parent()
    executor = AsyncMock()
    readonly_wl = _SUBAGENT_TYPE_TOOLS["readonly"]
    callback = _subagent_on_tool_call(
        parent, executor, "/ws", "/root", None, readonly_wl)
    import asyncio
    result = asyncio.run(callback(
        "commit_turn",
        json.dumps({"phase": "done_slice", "summary": "done"}),
        "tc-z"))
    assert result["end_turn"] is True
    executor.execute.assert_not_called()


def test_parent_has_source_write_rejects_qa_with_coordinator_perm():
    """R2: role='qa' + perm='coordinator'（hire_agent 推断）→ False。

    agent_gets_write_worktree 要求 perm=executor 或 (perm=coordinator
    且 family=coordinator)。role='qa' → family='qa' ≠ 'coordinator'，
    故 False。避免 QA 父能 spawn write 但无 worktree 的矛盾。
    """
    # role="qa" → is_test_engineer_role 匹配 → family="qa" ≠ "coordinator"
    # perm="coordinator" → agent_gets_write_worktree 要求 family=coordinator
    qa_coord_parent = _fake_parent_rich(
        role="qa", permission_type="coordinator")
    assert _parent_has_source_write(qa_coord_parent) is False

def test_parent_has_source_write_allows_qa_engineer_with_executor_perm():
    """R2: role='qa_engineer' + perm='executor'（常见 QA 配置）→ True。

    perm=executor → agent_gets_write_worktree 直接 True。
    """
    qa_exec_parent = _fake_parent_rich(
        role="qa_engineer", permission_type="executor")
    assert _parent_has_source_write(qa_exec_parent) is True


def test_subagent_whitelist_subset_of_registered_tools():
    """R2 防拼写：三类型白名单并集 ⊆ 注册工具名。

    TOOL_PARAM_SCHEMAS 是手动 schema，@tool 注册表是装饰器注册的工具；
    拼错/改名一个名字会静默少工具且旧测试全绿——本测试把白名单与
    注册表锚定，缺名立刻红。
    """
    import hiveweave.tools  # noqa: F401  populate @tool registry

    from hiveweave.tools.base import list_tool_names
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
    from hiveweave.tools.subagent import _SUBAGENT_TYPE_TOOLS

    registered = set(TOOL_PARAM_SCHEMAS) | set(list_tool_names())
    for typ, whitelist in _SUBAGENT_TYPE_TOOLS.items():
        missing = whitelist - registered
        assert not missing, (
            f"{typ} 白名单含未注册工具: {sorted(missing)}")


def test_spawn_subagent_rejects_write_for_qa_with_coordinator_perm():
    """R2 集成：QA 父（role='qa', perm='coordinator'）选 write → err。"""
    qa_parent = _fake_parent_rich(
        role="qa", permission_type="coordinator")
    params = SpawnSubagentParams(
        subagent_type="write", prompt="refactor X")
    with patch("hiveweave.agents.supervisor.agent_manager") as am:
        am.get_agent.return_value = qa_parent
        import asyncio
        result = asyncio.run(spawn_subagent_tool(
            params, agent_id="qa-1", workspace="/ws"))
    assert result.success is False
    assert "SOURCE_WRITE" in (result.error or "")
