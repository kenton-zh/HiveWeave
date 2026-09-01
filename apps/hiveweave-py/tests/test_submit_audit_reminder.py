"""submit_task code_audit 软提醒 + 账本重置 + attestationIds 过滤 + hook 台账。

直接使用真实的 services/code_audit 模块（不再 sys.modules 注入副本 stub）。
覆盖：
(a) 账本 >20 且无审计凭证 → 提交成功但文本带 CODE_AUDIT_REMINDER，状态仍流转
(b) 审计凭证晚于最后一次编辑 → 无提醒
(b2) 审计凭证早于最后一次编辑 → 提醒仍触发（B2 回归钉）
(c) ≤20 行 → 无提醒
(d) dry_run=True → 无提醒、不重置
(e) 真实提交成功 → reset_ledger 被调用
(f) attestationIds 含 code_audit kind → 被过滤，其余保留
(g) TOOL_EXECUTE_AFTER hook handler 单测（成功记账 / 失败与无关工具忽略 / 坏输入不抛 / 账本异常吞掉）
(h) ToolExecutor.execute 发射 hook 触发 handler
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_PROJ = "proj"
_AGENT = "agent-1"

_REMINDER_MARKER = "[CODE AUDIT REMINDER]"


@pytest.fixture(autouse=True)
def code_audit_env(monkeypatch):
    """真实 services/code_audit 模块 + 按 agent 隔离账本 + reset_ledger 调用计数。"""
    from hiveweave.services import code_audit

    code_audit.reset_ledger(_AGENT)
    state = {"reset_count": 0}
    real_reset = code_audit.reset_ledger

    def _spy_reset(agent_id: str) -> None:
        state["reset_count"] += 1
        real_reset(agent_id)

    monkeypatch.setattr(code_audit, "reset_ledger", _spy_reset)
    yield state
    real_reset(_AGENT)


def _params(attestation_ids=None, dry_run=False, summary="done"):
    return SimpleNamespace(
        task_id="task-1",
        summary=summary,
        commit=None,
        files_changed=None,
        test_output=None,
        tests_passed=True,
        attestation_ids=attestation_ids,
        dry_run=dry_run,
        core_interaction_executed=None,
        failures_acknowledged=None,
        env_snapshot=None,
        commit_hash=None,
    )


def _task():
    return {
        "id": "task-1",
        "status": "running",
        "tags": [],
        "policy_id": "coordinator_review",  # 软策略：strict attestation 门跳过
        "title": "impl task",
        "description": "",
        "assignee_id": _AGENT,
        "creator_id": None,  # 无 creator → 跳过 reviewer 通知段
    }


async def _run(params, extra_patches=None):
    """驱动 submit_task_tool，返回 (result, evidence)。"""
    captured: dict = {}

    async def _capture(project_id, task_id, evidence):
        captured["evidence"] = evidence

    stack = ExitStack()
    TS = stack.enter_context(patch("hiveweave.services.task.TaskService"))
    stack.enter_context(
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value=_PROJ,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new_callable=AsyncMock,
            return_value=(True, ""),
        )
    )
    for p in extra_patches or []:
        stack.enter_context(p)
    with stack:
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=_task())
        ts.submit_task = AsyncMock(side_effect=_capture)

        from hiveweave.tools.tasks.submit import submit_task_tool

        result = await submit_task_tool(params, _AGENT, "/tmp/ws")
    return result, captured.get("evidence") or {}


def _record_over_threshold() -> None:
    from hiveweave.services.code_audit import record_change

    record_change(_AGENT, 25)


def _last_change_ts_ms() -> int:
    from hiveweave.services.code_audit import get_last_change_ts

    return int(get_last_change_ts(_AGENT) * 1000)


# ── (a) 超阈 + 无审计 → 提醒但不阻断 ─────────────────────


@pytest.mark.asyncio
async def test_reminder_when_over_threshold_without_audit(code_audit_env):
    _record_over_threshold()
    result, _ = await _run(_params())
    assert result.success is True
    assert _REMINDER_MARKER in result.output
    assert "no fresh" in result.output
    assert code_audit_env["reset_count"] == 1


# ── (a2) llm_failed 之后提交：诚实措辞，不说从未审计 ──────────


@pytest.mark.asyncio
async def test_reminder_after_llm_failed_says_attempted(code_audit_env):
    from hiveweave.services.code_audit import (
        CODE_AUDIT_REMINDER_LLM_FAILED,
        effective_audit_timeout_s,
        record_audit_attempt,
    )

    _record_over_threshold()
    record_audit_attempt(_AGENT, "llm_failed", task_id="task-1")
    result, _ = await _run(_params())
    assert result.success is True
    assert _REMINDER_MARKER in result.output
    assert "attempted" in result.output
    assert "llm_failed" in result.output
    assert "no fresh" not in result.output
    # 报有效帽（agent 侧首读帽内），不报配置值
    eff = effective_audit_timeout_s()
    shown = int(eff) if float(eff).is_integer() else eff
    assert CODE_AUDIT_REMINDER_LLM_FAILED.format(timeout_s=shown) in result.output
    # s3-clone_06 P0-1/P0-3：fail-loud 后不得再宣称"不阻断"
    assert "does not block" not in result.output
    assert code_audit_env["reset_count"] == 1


# ── (a3) ISSUES 凭证仍是软门：提交成功、不阻断 ──────────────


@pytest.mark.asyncio
async def test_issues_attestation_does_not_block_submit(code_audit_env):
    _record_over_threshold()
    issues_audit = patch(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        new_callable=AsyncMock,
        return_value={
            "id": "audit-issues",
            "kind": "code_audit",
            "exit_code": 1,
            "created_at": _last_change_ts_ms() + 1000,
        },
    )
    result, _ = await _run(_params(), extra_patches=[issues_audit])
    assert result.success is True
    assert _REMINDER_MARKER not in result.output
    assert code_audit_env["reset_count"] == 1


# ── (b) 审计凭证晚于最后一次编辑 → 无提醒 ─────────────────


@pytest.mark.asyncio
async def test_no_reminder_with_audit_after_last_edit(code_audit_env):
    _record_over_threshold()
    fresh_audit = patch(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        new_callable=AsyncMock,
        return_value={
            "id": "audit-1",
            "kind": "code_audit",
            "created_at": _last_change_ts_ms() + 1000,
        },
    )
    result, _ = await _run(_params(), extra_patches=[fresh_audit])
    assert result.success is True
    assert _REMINDER_MARKER not in result.output
    assert code_audit_env["reset_count"] == 1


# ── (b2) 审计凭证早于最后一次编辑 → 提醒仍触发（B2 回归钉）─────


@pytest.mark.asyncio
async def test_reminder_when_audit_predates_last_edit(code_audit_env):
    _record_over_threshold()
    stale_audit = patch(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        new_callable=AsyncMock,
        return_value={
            "id": "audit-1",
            "kind": "code_audit",
            "created_at": _last_change_ts_ms() - 1000,
        },
    )
    result, _ = await _run(_params(), extra_patches=[stale_audit])
    assert result.success is True
    assert _REMINDER_MARKER in result.output
    assert code_audit_env["reset_count"] == 1


# ── (c) ≤20 行 → 无提醒 ──────────────────────────────────


@pytest.mark.asyncio
async def test_no_reminder_within_threshold(code_audit_env):
    from hiveweave.services.code_audit import record_change

    record_change(_AGENT, 5)
    result, _ = await _run(_params())
    assert result.success is True
    assert _REMINDER_MARKER not in result.output
    assert code_audit_env["reset_count"] == 1


# ── (d) dry_run → 无提醒、不重置 ──────────────────────────


@pytest.mark.asyncio
async def test_dry_run_no_reminder_no_reset(code_audit_env):
    _record_over_threshold()
    result, _ = await _run(_params(dry_run=True))
    assert result.success is True
    assert _REMINDER_MARKER not in result.output
    assert code_audit_env["reset_count"] == 0
    assert "dry-run" in result.output


# ── (e) 真实提交成功 → reset_ledger 被调用 ────────────────
# (a)-(c) 里已断言 reset_count==1；此处显式覆盖「提交失败不重置」。


@pytest.mark.asyncio
async def test_reset_only_on_real_submit_success(code_audit_env):
    _record_over_threshold()
    stack = ExitStack()
    TS = stack.enter_context(patch("hiveweave.services.task.TaskService"))
    stack.enter_context(
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value=_PROJ,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    with stack:
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=_task())
        ts.submit_task = AsyncMock(side_effect=RuntimeError("boom"))

        from hiveweave.tools.tasks.submit import submit_task_tool

        result = await submit_task_tool(_params(), _AGENT, "/tmp/ws")
    assert result.success is False
    assert code_audit_env["reset_count"] == 0


# ── (f) attestationIds 含 code_audit kind → 过滤，其余保留 ──


@pytest.mark.asyncio
async def test_code_audit_attestation_ids_filtered(code_audit_env):
    kinds = {"att-audit": "code_audit", "att-test": "test_run"}

    async def _fake_get(project_id, attestation_id):
        kind = kinds.get(attestation_id)
        if kind is None:
            return None
        return {"id": attestation_id, "kind": kind}

    get_patch = patch(
        "hiveweave.services.attestation.attestation_service.get",
        new_callable=AsyncMock,
        side_effect=_fake_get,
    )
    result, evidence = await _run(
        _params(attestation_ids=["att-audit", "att-test"]),
        extra_patches=[get_patch],
    )
    assert result.success is True
    assert evidence["attestation_ids"] == ["att-test"]


# ── (g) hook handler 单测 ─────────────────────────────────


@pytest.mark.asyncio
async def test_hook_records_write_file_success(code_audit_env):
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after
    from hiveweave.services.code_audit import get_unaudited_lines

    await on_tool_execute_after(
        {
            "agent_id": _AGENT,
            "tool_name": "write_file",
            "success": True,
            "params": {"filePath": "a.py", "content": "l1\nl2\nl3"},
            "output": "",
        },
        {},
    )
    assert get_unaudited_lines(_AGENT) == 3


@pytest.mark.asyncio
async def test_hook_ignores_failure_and_non_write_tools(code_audit_env):
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after
    from hiveweave.services.code_audit import get_unaudited_lines

    await on_tool_execute_after(
        {
            "agent_id": _AGENT,
            "tool_name": "write_file",
            "success": False,
            "params": {"filePath": "a.py", "content": "x\ny"},
            "output": "",
        },
        {},
    )
    await on_tool_execute_after(
        {
            "agent_id": _AGENT,
            "tool_name": "read_file",
            "success": True,
            "params": {"filePath": "a.py"},
            "output": "",
        },
        {},
    )
    assert get_unaudited_lines(_AGENT) == 0


@pytest.mark.asyncio
async def test_hook_never_raises_on_bad_input(code_audit_env):
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after
    from hiveweave.services.code_audit import get_unaudited_lines

    for bad in (
        {},
        {"tool_name": "write_file"},
        {"tool_name": "write_file", "success": True},
        {
            "tool_name": "write_file",
            "success": True,
            "agent_id": _AGENT,
            "params": None,
        },
        {
            "tool_name": "write_file",
            "success": True,
            "agent_id": _AGENT,
            "params": {"filePath": "a.py"},
        },
    ):
        await on_tool_execute_after(bad, {})
    assert get_unaudited_lines(_AGENT) == 0


@pytest.mark.asyncio
async def test_hook_swallows_ledger_failure(code_audit_env, monkeypatch):
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after
    from hiveweave.services import code_audit
    from hiveweave.services.code_audit import get_unaudited_lines

    def _boom(*args, **kwargs) -> None:
        raise RuntimeError("ledger boom")

    monkeypatch.setattr(code_audit, "record_change", _boom)
    await on_tool_execute_after(
        {
            "agent_id": _AGENT,
            "tool_name": "write_file",
            "success": True,
            "params": {"filePath": "a.py", "content": "l1\nl2"},
            "output": "",
        },
        {},
    )
    assert get_unaudited_lines(_AGENT) == 0


# ── (h) ToolExecutor.execute 发射 hook 触发 handler ──────


@pytest.mark.asyncio
async def test_executor_emission_fires_ledger_handler(code_audit_env):
    from hiveweave.hooks import TOOL_EXECUTE_AFTER, hooks
    from hiveweave.hooks.handlers import register_builtin_handlers
    from hiveweave.hooks.handlers.code_audit_ledger import register as register_ledger
    from hiveweave.services.code_audit import get_unaudited_lines

    register_builtin_handlers()
    hooks.clear(TOOL_EXECUTE_AFTER)
    register_ledger()
    try:
        from hiveweave.tools.executor import ToolExecutor

        class _Perm:
            async def evaluate_detailed(self, agent_id, tool_name, args):
                return ("allow", None)

        class _Approval:
            pass

        with (
            patch(
                "hiveweave.tools.pipeline._refuse_project_root_write",
                new_callable=AsyncMock,
                return_value=None,
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            ws = str(Path(tmp) / "ws")
            Path(ws).mkdir()
            executor = ToolExecutor(_Perm(), _Approval())
            result = await executor.execute(
                _AGENT,
                "write_file",
                {"filePath": "x.txt", "content": "a\nb\nc\nd"},
                ws,
            )
        assert result.get("success") is True
        assert get_unaudited_lines(_AGENT) == 4
    finally:
        hooks.clear(TOOL_EXECUTE_AFTER)
