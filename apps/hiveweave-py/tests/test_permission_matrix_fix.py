"""Permission matrix 止血修复回归 — 三 bug。

1. cancel_task / unclaim_task / waive_attestation 未收录进 COORDINATOR_TOOLS
   → coordinator 合法出口被 permission 层 deny，任务台账只进不出。
2. qa 族 capabilities 缺 SOURCE_WRITE → readwrite executor（实测 Echo，
   role='游戏测试工程师'）写测试文件被 write_file 硬门误拒。
3. coordinator deny 提示谎称 'read-only role'，与 policy 受限写白名单不符；
   真实硬门原因只写日志、不返回模型。
"""

from __future__ import annotations

import pytest

from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import (
    Capability,
    has_capability,
    infer_role_family,
    policy_service,
)
from hiveweave.tools.pipeline import build_deny_hint, execute_registered_tool


def _agent(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "墨白",
        "role": "签到工程师",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "allowed_tools": "[]",
        "denied_tools": "[]",
        "ask_tools": "[]",
    }
    base.update(kwargs)
    return base


def _ceo(**kwargs) -> dict:
    """CEO 行政 family（role=ceo, permission_type=coordinator）。"""
    return _agent(
        role="ceo",
        name="归零",
        permission_type="coordinator",
        permission_mode="readonly",
        **kwargs,
    )


def _builder_coordinator(**kwargs) -> dict:
    """中层 builder coordinator（player-coach）：协调权 + 写码权。"""
    return _agent(
        role="前端架构师",
        name="云岫",
        permission_type="coordinator",
        permission_mode="readwrite",
        **kwargs,
    )


def _echo(**kwargs) -> dict:
    """实测事故行：Echo (A005), 游戏测试工程师, permission_type=executor + readwrite."""
    return _agent(role="游戏测试工程师", name="Echo", **kwargs)


@pytest.fixture
def svc() -> PermissionService:
    return PermissionService()


def _patch_agent(monkeypatch: pytest.MonkeyPatch, agent: dict) -> None:
    async def fake_get(_aid):
        return agent

    monkeypatch.setattr(
        "hiveweave.services.permission.meta_db.get_agent_by_id", fake_get
    )


# ── Bug 1: 台账出口三工具对 coordinator 放行、对 executor 拦截 ──

EXIT_TOOLS = ("cancel_task", "unclaim_task", "waive_attestation")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", EXIT_TOOLS)
@pytest.mark.parametrize("fixture", [_ceo, _builder_coordinator])
async def test_coordinator_can_use_exit_tools(svc, monkeypatch, tool, fixture):
    _patch_agent(monkeypatch, fixture())
    decision = await svc.evaluate("a1", tool, {"taskId": "t1", "reason": "r"})
    assert decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", EXIT_TOOLS)
async def test_executor_cannot_use_exit_tools(svc, monkeypatch, tool):
    # 工具本体（task_tools）无角色守卫 → 必须由 policy 硬能力门拦截
    # （cancel/unclaim 需 DISPATCH、waive 需 REVIEW，executor 族均不具备）
    _patch_agent(monkeypatch, _agent())
    decision = await svc.evaluate("a1", tool, {"taskId": "t1", "reason": "r"})
    assert decision == "deny"


def test_exit_tools_in_coordinator_tool_list(svc):
    for fixture in (_ceo, _builder_coordinator):
        tools = svc.get_tools_for_agent(fixture())
        for t in EXIT_TOOLS:
            assert t in tools


def test_exit_tools_not_in_executor_tool_list(svc):
    tools = svc.get_tools_for_agent(_agent())
    for t in EXIT_TOOLS:
        assert t not in tools


# ── Bug 2: readwrite 测试工程师（Echo）写文件不被误拒 ──────────


@pytest.mark.asyncio
async def test_echo_write_test_file_allowed(svc, monkeypatch):
    _patch_agent(monkeypatch, _echo())
    decision = await svc.evaluate(
        "a1", "write_file", {"filePath": "tests/test_game.py", "content": "x"}
    )
    assert decision == "allow"


@pytest.mark.asyncio
async def test_echo_write_and_edit_source_allowed(svc, monkeypatch):
    _patch_agent(monkeypatch, _echo())
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "src/game.py", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate("a1", "edit_file", {"filePath": "src/game.py"})
        == "allow"
    )


def test_echo_family_still_qa_for_verify_discovery():
    # VERIFY 独立验收依赖 qa 族识别（_find_independent_qa）—— 分类保持不变，
    # 只补回 SOURCE_WRITE 能力。
    echo = _echo()
    assert infer_role_family(echo) == "qa"
    assert has_capability(echo, Capability.BROWSER_ACCEPTANCE)
    assert has_capability(echo, Capability.SOURCE_WRITE)


@pytest.mark.asyncio
async def test_ceo_write_scope_any_path_after_batch_a(svc, monkeypatch):
    """批 A 第 0 步：CEO 有 SOURCE_WRITE —— 文档与源码任意路径都放行。

    旧名 `test_ceo_write_scope_unchanged` 钉的是「源码硬拒」；本批有意翻转，
    新契约 = 路径硬门对 ceo 不再产生任何拒绝（拒绝只可能来自操作者规则）。
    """
    _patch_agent(monkeypatch, _ceo())
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "docs/plan.md", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "SHIP.md", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate(
            "a1", "edit_file", {"filePath": "notes/retro.md"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "src/app.py", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate("a1", "edit_file", {"filePath": "src/app.py"})
        == "allow"
    )


@pytest.mark.asyncio
async def test_builder_coordinator_write_source_allowed(svc, monkeypatch):
    # 中层 builder：SOURCE_WRITE 落地后写源码放行
    _patch_agent(monkeypatch, _builder_coordinator())
    assert (
        await svc.evaluate(
            "a1", "write_file", {"filePath": "src/app.py", "content": "x"}
        )
        == "allow"
    )
    assert (
        await svc.evaluate("a1", "edit_file", {"filePath": "src/app.py"})
        == "allow"
    )
    assert await svc.evaluate("a1", "bash", {"command": "pytest"}) == "allow"
    assert await svc.evaluate("a1", "run_tests", {}) == "allow"


@pytest.mark.asyncio
async def test_ceo_can_browse_and_shell_but_not_assert(svc, monkeypatch):
    """批 A 第 0 步：CEO 能力/工具面拿到 bash（含 MAIN 位）；出证特判仍拒。"""
    _patch_agent(monkeypatch, _ceo())
    assert await svc.evaluate("a1", "browse", {"args": ["goto", "http://127.0.0.1:1"]}) == "allow"
    assert await svc.evaluate(
        "a1", "browse_main", {"args": ["goto", "http://127.0.0.1:1"]}
    ) == "allow"
    assert await svc.evaluate("a1", "bash", {"command": "ls"}) == "allow"
    assert await svc.evaluate(
        "a1", "bash_main", {"command": "git status"}
    ) == "allow"
    assert await svc.evaluate("a1", "run_tests", {}) == "deny"
    assert await svc.evaluate(
        "a1",
        "assert_visual",
        {
            "observed": "x" * 50,
            "verdict": "pass",
            "screenshotPath": "evidence/x.png",
        },
    ) == "deny"
    assert await svc.evaluate("a1", "game_run_case", {}) == "deny"
    assert await svc.evaluate("a1", "game_run_case_main", {}) == "deny"


def test_ceo_tool_list_excludes_code_collab_tools(svc):
    """批 A 第 0 步 + 追平单兵补齐：shell 位可见（按宿主形态）；文件操作六件套
    随「先追平单兵」进可见集；run_tests/出证类仍不可见。

    apply_patch 等能力位（SOURCE_WRITE）与 allowlist 现已同时放行；仍排除的
    是 TEST_RUN 系（run_tests 经 bash 覆盖）与 spawn_subagent（派单链替代）。
    """
    tools = svc.get_tools_for_agent(_ceo())
    from hiveweave.services.host_env import host_hidden_tools

    hidden = host_hidden_tools()
    for t in ("bash", "bash_main", "pwsh", "pwsh_main"):
        if t in hidden:
            assert t not in tools, t
        else:
            assert t in tools, t
    for t in ("run_tests", "game_run_case",
              "game_run_case_main", "assert_visual"):
        assert t not in tools
    # 追平单兵：文件操作六件套可见（单兵对照 opencode registry 面）
    for t in ("apply_patch", "delete_file", "move_file",
              "create_directory", "delete_directory", "search_files"):
        assert t in tools, t
    assert "browse" in tools
    assert "browse_main" in tools
    # edit_file 在工具表内；SOURCE_WRITE 落地后源码路径也不再被路径硬门拦
    assert "edit_file" in tools
    assert "write_file" in tools
    for t in ("dispatch_task", "review_task", "git_worktree_merge",
              "save_charter", "update_goals", "message_user"):
        assert t in tools


def test_message_user_in_all_tools(svc):
    assert "message_user" in svc.get_tools_for_mode("full")


# ── Bug 3: deny 提示如实（白名单 + 真实原因，无 'read-only role'） ──


def test_deny_hint_ceo_write_points_to_mid_level():
    """批 A 第 0 步：CEO 写工具的拒绝提示如实反映 SOURCE_WRITE（不再谎称仅 DOC_WRITE）。"""
    hint = build_deny_hint("edit_file", "ceo")
    assert "SOURCE_WRITE" in hint
    assert "DOC_WRITE" in hint
    assert "dispatch_task" in hint
    assert "CEO" in hint
    assert "read-only" not in hint
    # 不再枚举文件名白名单
    assert "README.md" not in hint


def test_deny_hint_builder_coordinator_write_points_to_worktree():
    hint = build_deny_hint("write_file", "coordinator")
    assert "docs/" in hint
    assert ".hiveweave/shared/" in hint
    assert "worktree" in hint
    assert "read-only" not in hint


def test_deny_hint_includes_real_hard_reason():
    """硬门原因整段进提示。CEO 源码写已放行（批 A 第 0 步）——反例改 run_tests。"""
    reason = policy_service.hard_check(_ceo(), "run_tests", {})
    assert reason
    assert "test_run" in reason
    hint = build_deny_hint("run_tests", "ceo", reason)
    assert reason in hint
    assert "read-only" not in hint


def test_deny_hint_generic_for_executor():
    hint = build_deny_hint("bash", "executor")
    assert hint == "Permission denied: bash is blocked for this agent."


@pytest.mark.asyncio
async def test_pipeline_deny_hint_end_to_end(monkeypatch, tmp_path):
    """CEO 写工具被拒 → pipeline 返回 SOURCE_WRITE/DOC_WRITE 原则 + 委派指引。

    批 A 第 0 步后源码写本身放行 —— 这里用 `_DenyAll` 模拟操作者 deny 规则，
    验证的是「拒绝回执的提示文案与能力现状一致」。
    """
    import hiveweave.tools.file  # noqa: F401 — 确保 write_file 完成 @tool 注册

    _patch_agent(monkeypatch, _ceo())

    class _DenyAll:
        async def evaluate(self, *_a, **_k):
            return "deny"

    result = await execute_registered_tool(
        tool_name="write_file",
        raw_args={"filePath": "src/app.py", "content": "x"},
        agent_id="a1",
        workspace_path=str(tmp_path),
        permission=_DenyAll(),
        approval=None,
    )
    assert result is not None
    assert result["success"] is False
    assert "SOURCE_WRITE" in result["error"]
    assert "DOC_WRITE" in result["error"]
    assert "dispatch_task" in result["error"]
    assert "read-only" not in result["error"]
    assert "README.md" not in result["error"]
