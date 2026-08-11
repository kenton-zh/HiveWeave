"""submit_task code_audit 软提醒 + 账本重置 + attestationIds 过滤 + hook 台账。

覆盖：
(a) 账本 >20 且近期无审计凭证 → 提交成功但文本带 CODE_AUDIT_REMINDER，状态仍流转
(b) 近期已有 code_audit 审计凭证 → 无提醒
(c) ≤20 行 → 无提醒
(d) dry_run=True → 无提醒、不重置
(e) 真实提交成功 → reset_ledger 被调用
(f) attestationIds 含 code_audit kind → 被过滤，其余保留
(g) TOOL_EXECUTE_AFTER hook handler 单测（成功记账 / 失败与无关工具忽略 / 坏输入不抛）
(h) ToolExecutor.execute 发射 hook 触发 handler
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_PROJ = "proj"
_AGENT = "agent-1"

_REMINDER = (
    "[CODE AUDIT REMINDER] 本次代码变更超过 20 行且近期未执行代码审计，"
    "请执行 request_code_audit 后再重新提交。"
)


class _Ledger:
    def __init__(self) -> None:
        self.lines = 0
        self.reset_count = 0


def _install_code_audit_stub(ledger: _Ledger) -> None:
    """services/code_audit 由并行 agent 实现 —— 测试用同契约 stub 占位。"""
    mod = ModuleType("hiveweave.services.code_audit")
    mod.CODE_AUDIT_LINE_THRESHOLD = 20  # type: ignore[attr-defined]
    mod.CODE_AUDIT_KIND = "code_audit"  # type: ignore[attr-defined]
    mod.CODE_AUDIT_REMINDER = _REMINDER  # type: ignore[attr-defined]

    def count_change_lines(tool_name: str, params: dict) -> int:
        if not isinstance(params, dict):
            return 0
        if tool_name == "write_file":
            return len((params.get("content") or "").splitlines())
        if tool_name == "edit_file":
            return len((params.get("new_string") or "").splitlines())
        if tool_name == "apply_patch":
            total = 0
            for p in params.get("patches") or []:
                txt = p.get("newString") or p.get("content") or ""
                total += len(txt.splitlines())
            return total
        return 0

    def record_change(agent_id: str, lines: int) -> None:
        ledger.lines += int(lines or 0)

    def get_unaudited_lines(agent_id: str) -> int:
        return ledger.lines

    def reset_ledger(agent_id: str) -> None:
        ledger.reset_count += 1
        ledger.lines = 0

    def ledger_snapshot() -> dict:
        return {"lines": ledger.lines}

    mod.count_change_lines = count_change_lines  # type: ignore[attr-defined]
    mod.record_change = record_change  # type: ignore[attr-defined]
    mod.get_unaudited_lines = get_unaudited_lines  # type: ignore[attr-defined]
    mod.reset_ledger = reset_ledger  # type: ignore[attr-defined]
    mod.ledger_snapshot = ledger_snapshot  # type: ignore[attr-defined]
    sys.modules["hiveweave.services.code_audit"] = mod  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def code_audit_env():
    """Stub services.code_audit（并行 agent 拥有真实模块）+ 账本。"""
    ledger = _Ledger()
    prev = sys.modules.get("hiveweave.services.code_audit")
    _install_code_audit_stub(ledger)
    yield ledger
    if prev is None:
        sys.modules.pop("hiveweave.services.code_audit", None)
    else:
        sys.modules["hiveweave.services.code_audit"] = prev


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
        "policy_id": "generic_tests",  # 软策略：strict attestation 门跳过
        "title": "impl task",
        "description": "",
        "assignee_id": _AGENT,
        "creator_id": None,  # 无 creator → 跳过 reviewer 通知段
    }


async def _run(params, ledger, extra_patches=None):
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


# ── (a) 超阈 + 无审计 → 提醒但不阻断 ─────────────────────


@pytest.mark.asyncio
async def test_reminder_when_over_threshold_without_audit(code_audit_env):
    ledger = code_audit_env
    ledger.lines = 25
    result, _ = await _run(_params(), ledger)
    assert result.success is True
    assert "[CODE AUDIT REMINDER]" in result.output
    assert ledger.reset_count == 1


# ── (b) 近期已有审计凭证 → 无提醒 ─────────────────────────


@pytest.mark.asyncio
async def test_no_reminder_with_fresh_audit_attestation(code_audit_env):
    ledger = code_audit_env
    ledger.lines = 25
    fresh_audit = patch(
        "hiveweave.services.attestation.find_latest_attestation_by_kind",
        new_callable=AsyncMock,
        return_value={"id": "audit-1", "kind": "code_audit"},
    )
    result, _ = await _run(_params(), ledger, extra_patches=[fresh_audit])
    assert result.success is True
    assert "[CODE AUDIT REMINDER]" not in result.output
    assert ledger.reset_count == 1


# ── (c) ≤20 行 → 无提醒 ──────────────────────────────────


@pytest.mark.asyncio
async def test_no_reminder_within_threshold(code_audit_env):
    ledger = code_audit_env
    ledger.lines = 5
    result, _ = await _run(_params(), ledger)
    assert result.success is True
    assert "[CODE AUDIT REMINDER]" not in result.output
    assert ledger.reset_count == 1


# ── (d) dry_run → 无提醒、不重置 ──────────────────────────


@pytest.mark.asyncio
async def test_dry_run_no_reminder_no_reset(code_audit_env):
    ledger = code_audit_env
    ledger.lines = 25
    result, _ = await _run(_params(dry_run=True), ledger)
    assert result.success is True
    assert "[CODE AUDIT REMINDER]" not in result.output
    assert ledger.reset_count == 0
    assert "dry-run" in result.output


# ── (e) 真实提交成功 → reset_ledger 被调用 ────────────────
# (a)-(c) 里已断言 reset_count==1；此处显式覆盖「提交失败不重置」。


@pytest.mark.asyncio
async def test_reset_only_on_real_submit_success(code_audit_env):
    ledger = code_audit_env
    ledger.lines = 30
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
    assert ledger.reset_count == 0


# ── (f) attestationIds 含 code_audit kind → 过滤，其余保留 ──


@pytest.mark.asyncio
async def test_code_audit_attestation_ids_filtered(code_audit_env):
    ledger = code_audit_env
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
        ledger,
        extra_patches=[get_patch],
    )
    assert result.success is True
    assert evidence["attestation_ids"] == ["att-test"]


# ── (g) hook handler 单测 ─────────────────────────────────


@pytest.mark.asyncio
async def test_hook_records_write_file_success(code_audit_env):
    ledger = code_audit_env
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after

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
    assert ledger.lines == 3


@pytest.mark.asyncio
async def test_hook_ignores_failure_and_non_write_tools(code_audit_env):
    ledger = code_audit_env
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after

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
    assert ledger.lines == 0


@pytest.mark.asyncio
async def test_hook_never_raises_on_bad_input(code_audit_env):
    ledger = code_audit_env
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after

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
    assert ledger.lines == 0


@pytest.mark.asyncio
async def test_hook_swallows_module_failure(code_audit_env):
    ledger = code_audit_env
    sys.modules.pop("hiveweave.services.code_audit", None)
    from hiveweave.hooks.handlers.code_audit_ledger import on_tool_execute_after

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
    assert ledger.lines == 0


# ── (h) ToolExecutor.execute 发射 hook 触发 handler ──────


@pytest.mark.asyncio
async def test_executor_emission_fires_ledger_handler(code_audit_env):
    ledger = code_audit_env
    from hiveweave.hooks import TOOL_EXECUTE_AFTER, hooks
    from hiveweave.hooks.handlers import register_builtin_handlers
    from hiveweave.hooks.handlers.code_audit_ledger import register as register_ledger

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
        assert ledger.lines == 4
    finally:
        hooks.clear(TOOL_EXECUTE_AFTER)
