"""request_code_audit domain core — run_code_audit soft-fail paths + helpers.

Mock style mirrors test_attestation_auto_attach.py (AsyncMock patches of
module-level singletons + lazy-imported package attrs).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

HEAD_SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


def _fake_git(*pairs: tuple[tuple[str, ...], tuple[bool, str]]):
    """Async fake for ``hiveweave.services.git_worktree._git``."""
    table = {key: value for key, value in pairs}

    async def fake_git(args: list[str], cwd: str, timeout: float = 30.0) -> tuple[bool, str]:
        return table.get(tuple(args), (False, ""))

    return fake_git


def _attestation_service(att_id: str = "att-1"):
    svc = MagicMock()
    svc.create = AsyncMock(return_value=att_id)
    return svc


@pytest.mark.asyncio
async def test_no_worktree_soft_fail():
    from hiveweave.services.code_audit import run_code_audit

    with patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await run_code_audit("proj", "agent-1")
    assert result == {"audited": False, "reason": "no_worktree"}
    from hiveweave.services.code_audit import get_last_audit_attempt, reset_ledger

    assert get_last_audit_attempt("agent-1") is None
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_empty_diff_auto_pass_no_llm():
    from hiveweave.services.code_audit import (
        CODE_AUDIT_KIND,
        get_unaudited_lines,
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 5)  # within threshold

    git = _fake_git(
        (("rev-parse", "--verify", "refs/heads/main"), (True, "")),
        (("diff", "main...HEAD"), (True, "")),
        (("diff", "HEAD"), (True, "")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
        (("rev-parse", "HEAD"), (True, HEAD_SHA)),
    )
    att_svc = _attestation_service()
    llm_mock = AsyncMock()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=git),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
    ):
        result = await run_code_audit("proj", "agent-1", call_llm=llm_mock)

    assert result["audited"] is True
    assert result["verdict"] == "PASS"
    assert result["lines_audited"] == 0
    assert result["attestation_id"] == "att-1"
    llm_mock.assert_not_awaited()

    kw = att_svc.create.await_args.kwargs
    assert kw["kind"] == CODE_AUDIT_KIND
    assert kw["exit_code"] == 0
    assert kw["workspace"] == r"C:\fake\wt"
    assert kw["commit_hash"] == HEAD_SHA
    assert kw["stdout_hash"]  # sha256 of "no changes to audit"
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_normal_path_issues_verdict_and_reset():
    from hiveweave.services.code_audit import (
        CODE_AUDIT_KIND,
        get_unaudited_lines,
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    git = _fake_git(
        (("rev-parse", "--verify", "refs/heads/main"), (True, "")),
        (
            ("diff", "main...HEAD"),
            (True, "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"),
        ),
        (("diff", "HEAD"), (True, "")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
        (("rev-parse", "HEAD"), (True, HEAD_SHA)),
    )
    att_svc = _attestation_service()
    llm_text = "VERDICT: ISSUES\n- x.py:12 [high] crash risk on empty input\n- y.py:3 [low] style nit\n"
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=git),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\wt\.hiveweave\tool_outputs\code_audit_1.txt",
        ) as save_mock,
    ):
        result = await run_code_audit(
            "proj", "agent-1", task_id="task-9",
            call_llm=AsyncMock(return_value=llm_text),
        )

    assert result["audited"] is True
    assert result["verdict"] == "ISSUES"
    assert result["issues_count"] == 2
    assert result["top_issues"][0].startswith("x.py:12")
    assert result["top_issues"][1] == "y.py:3 [low] style nit"
    assert result["report_path"] == r"C:\fake\wt\.hiveweave\tool_outputs\code_audit_1.txt"
    assert result["attestation_id"] == "att-1"
    assert result["lines_audited"] == 25  # ledger value before reset
    assert get_unaudited_lines("agent-1") == 0  # reset_ledger called

    kw = att_svc.create.await_args.kwargs
    assert kw["kind"] == CODE_AUDIT_KIND
    assert kw["exit_code"] == 1
    assert kw["task_id"] == "task-9"
    assert kw["workspace"] == r"C:\fake\wt"
    assert kw["commit_hash"] == HEAD_SHA

    save_args = save_mock.call_args.args
    assert save_args[0] == llm_text
    assert save_args[1] == "agent-1"
    assert save_args[2] == CODE_AUDIT_KIND
    assert save_args[3] == r"C:\fake\wt"
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_llm_failed_soft_fail():
    from hiveweave.services.code_audit import (
        get_last_audit_attempt,
        get_unaudited_lines,
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    git = _fake_git(
        (("rev-parse", "--verify", "refs/heads/main"), (True, "")),
        (("diff", "main...HEAD"), (True, "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")),
        (("diff", "HEAD"), (True, "")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
        (("rev-parse", "HEAD"), (True, HEAD_SHA)),
    )
    att_svc = _attestation_service()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=git),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
    ):
        result = await run_code_audit(
            "proj", "agent-1", call_llm=AsyncMock(return_value=None)
        )

    assert result == {"audited": False, "reason": "llm_failed"}
    att_svc.create.assert_not_awaited()
    assert get_unaudited_lines("agent-1") == 25  # ledger untouched on failure
    attempt = get_last_audit_attempt("agent-1")
    assert attempt is not None
    assert attempt["reason"] == "llm_failed"
    reset_ledger("agent-1")
    assert get_last_audit_attempt("agent-1") is None


def _diff_git():
    """Fake git serving a non-empty committed diff (LLM path reached)."""
    return _fake_git(
        (("rev-parse", "--verify", "refs/heads/main"), (True, "")),
        (("diff", "main...HEAD"), (True, "--- a/x.py\n+++ b/x.py\n-old\n+new")),
        (("diff", "HEAD"), (True, "")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
        (("rev-parse", "HEAD"), (True, HEAD_SHA)),
    )


@pytest.mark.asyncio
async def test_no_callback_soft_fail():
    """call_llm=None（工具壳外调用 / ctx 未接线）→ no_callback，不动账本。"""
    from hiveweave.services.code_audit import (
        get_last_audit_attempt,
        get_unaudited_lines,
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
    ):
        result = await run_code_audit("proj", "agent-1")  # no call_llm

    assert result == {"audited": False, "reason": "no_callback"}
    assert get_unaudited_lines("agent-1") == 25
    attempt = get_last_audit_attempt("agent-1")
    assert attempt is not None
    assert attempt["reason"] == "no_callback"
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_callback_no_model_error_maps_no_model():
    """_review_llm_callback 的 NoModelConfiguredError → no_model（按类型捕获）。"""
    from hiveweave.services.code_audit import (
        get_last_audit_attempt,
        reset_ledger,
        run_code_audit,
    )
    from hiveweave.services.model import NoModelConfiguredError

    reset_ledger("agent-1")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
    ):
        result = await run_code_audit(
            "proj", "agent-1",
            call_llm=AsyncMock(
                side_effect=NoModelConfiguredError(
                    "No model configured for review LLM callback"
                )
            ),
        )
    assert result == {"audited": False, "reason": "no_model"}
    attempt = get_last_audit_attempt("agent-1")
    assert attempt is not None
    assert attempt["reason"] == "no_model"
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_callback_generic_error_maps_llm_failed():
    """其他异常（网络/HTTP）→ llm_failed。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger("agent-1")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
    ):
        result = await run_code_audit(
            "proj", "agent-1",
            call_llm=AsyncMock(side_effect=RuntimeError("HTTP 500 boom")),
        )
    assert result == {"audited": False, "reason": "llm_failed"}


@pytest.mark.asyncio
async def test_callback_empty_text_maps_llm_failed():
    """callback 返回 ""（_review_llm_callback 空 choices 的真实行为）→ llm_failed。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger("agent-1")
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
    ):
        result = await run_code_audit(
            "proj", "agent-1", call_llm=AsyncMock(return_value="")
        )
    assert result == {"audited": False, "reason": "llm_failed"}


@pytest.mark.asyncio
async def test_auto_pass_without_callback():
    """call_llm=None + 空 diff + 账本未超阈 → auto-PASS（不需要 LLM）。"""
    from hiveweave.services.code_audit import reset_ledger, run_code_audit

    reset_ledger("agent-1")
    git = _fake_git(
        (("rev-parse", "--verify", "refs/heads/main"), (True, "")),
        (("diff", "main...HEAD"), (True, "")),
        (("diff", "HEAD"), (True, "")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
        (("rev-parse", "HEAD"), (True, HEAD_SHA)),
    )
    att_svc = _attestation_service()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=git),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
    ):
        result = await run_code_audit("proj", "agent-1")  # no call_llm

    assert result["audited"] is True
    assert result["verdict"] == "PASS"
    assert result["attestation_id"] == "att-1"


# ── 工具壳层：ctx.review_llm_callback 接线 ──────────────────


@pytest.mark.asyncio
async def test_tool_shell_passes_ctx_callback():
    """ctx.review_llm_callback / oneshot_llm_callback 原样传给 run_code_audit。"""
    from types import SimpleNamespace

    from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool

    callback = AsyncMock(return_value="VERDICT: PASS")
    oneshot = AsyncMock(return_value="VERDICT: PASS")
    run_mock = AsyncMock(return_value={"audited": True, "verdict": "PASS",
                                       "lines_audited": 0, "attestation_id": "att-1"})
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value="proj",
        ),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="task-1"),
            "agent-1",
            r"C:\fake\wt",
            ctx=SimpleNamespace(
                review_llm_callback=callback,
                oneshot_llm_callback=oneshot,
            ),
        )

    assert result.success is True
    assert run_mock.await_args.kwargs["call_llm"] is callback
    assert run_mock.await_args.kwargs["oneshot_llm"] is oneshot


@pytest.mark.asyncio
async def test_tool_shell_ctx_none_soft_fails():
    """ctx=None（无回调可接）→ call_llm=None 透传，工具仍软失败契约。"""
    from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool

    run_mock = AsyncMock(return_value={"audited": False, "reason": "no_callback"})
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value="proj",
        ),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="task-1"), "agent-1", r"C:\fake\wt"
        )

    assert result.success is True  # soft-fail: ok 带 reason，不是 err
    assert "no_callback" in (result.output or "")
    assert "retry" in (result.output or "").lower()
    assert "submit_task" in (result.output or "")
    assert run_mock.await_args.kwargs["call_llm"] is None
    assert run_mock.await_args.kwargs["oneshot_llm"] is None


@pytest.mark.asyncio
async def test_tool_shell_llm_failed_next_action_is_soft():
    """llm_failed 工具回执提示可重试一次或直接 submit（软门）。"""
    from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool

    run_mock = AsyncMock(return_value={"audited": False, "reason": "llm_failed"})
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value="proj",
        ),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="task-1"), "agent-1", r"C:\fake\wt"
        )

    assert result.success is True
    text = result.output or ""
    assert "llm_failed" in text
    assert "retry" in text.lower()
    assert "submit_task" in text
    assert "soft" in text.lower()


@pytest.mark.asyncio
async def test_tool_shell_no_project_err():
    """agent 无项目 → ToolResult.err（唯一硬失败分支）。"""
    from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(task_id="task-1"), "agent-1", r"C:\fake\wt"
        )
    assert result.success is False
    assert "no project" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_shell_auto_detects_running_task():
    """taskId 缺省时从唯一活动任务自动解析。"""
    from hiveweave.tools.code_audit import RequestCodeAuditParams, request_code_audit_tool

    run_mock = AsyncMock(return_value={"audited": True, "verdict": "PASS",
                                       "lines_audited": 0, "attestation_id": "att-1"})
    ts_instance = AsyncMock()
    ts_instance.list_tasks = AsyncMock(return_value=[
        {"id": "task-auto", "status": "running", "assignee_id": "agent-1"},
    ])
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value="proj",
        ),
        patch("hiveweave.services.task.TaskService", return_value=ts_instance),
        patch("hiveweave.services.code_audit.run_code_audit", new=run_mock),
    ):
        result = await request_code_audit_tool(
            RequestCodeAuditParams(), "agent-1", r"C:\fake\wt"
        )

    assert result.success is True
    assert run_mock.await_args.args[2] == "task-auto"


def test_build_audit_prompt_contract():
    """系统提示锁定 verdict 协议；user 提示携带任务上下文与 diff。"""
    from hiveweave.services.code_audit import build_audit_prompt

    system, user = build_audit_prompt("DIFF_BODY", "task-9")
    assert "VERDICT: PASS" in system
    assert "VERDICT: ISSUES" in system
    assert "second-pass" in system
    assert "task-9" in user
    assert "DIFF_BODY" in user


def test_count_change_lines_unit():
    from hiveweave.services.code_audit import count_change_lines

    assert count_change_lines("write_file", {"content": "a\nb\nc"}) == 3
    assert count_change_lines("write_file", {}) == 0
    assert count_change_lines("edit_file", {"oldString": "x\n", "newString": "y\nz\nw"}) == 3
    assert count_change_lines("edit_file", {"oldString": "x"}) == 1
    assert count_change_lines("apply_patch", {"patches": [
        {"op": "add", "content": "1\n2\n3\n4"},
        {"op": "update", "oldString": "a\n", "newString": "b\nc\nd\ne"},
        {"op": "delete", "content": "x\ny\nz"},
        {"op": "rename", "content": "1\n2"},
        {"op": "add"},
    ]}) == 8  # 4 + 4 + 0 + 0 + 0
    assert count_change_lines("apply_patch", {}) == 0
    assert count_change_lines("unknown_tool", {"content": "x\ny"}) == 0


def test_ledger_basic():
    from hiveweave.services.code_audit import (
        get_unaudited_lines,
        ledger_snapshot,
        record_change,
        reset_ledger,
    )

    reset_ledger("agent-x")
    record_change("agent-x", 10)
    record_change("agent-x", 3)
    record_change("agent-x", -5)  # clamped away
    assert get_unaudited_lines("agent-x") == 13
    assert get_unaudited_lines("nobody") == 0
    assert ledger_snapshot().get("agent-x") == 13
    reset_ledger("agent-x")
    assert get_unaudited_lines("agent-x") == 0


def test_submit_reminder_helper_llm_failed_vs_generic():
    from hiveweave.services.code_audit import (
        CODE_AUDIT_REMINDER,
        CODE_AUDIT_REMINDER_LLM_FAILED,
        code_audit_submit_reminder,
        record_audit_attempt,
        record_change,
        reset_ledger,
    )

    reset_ledger("agent-rem")
    record_change("agent-rem", 25)
    assert code_audit_submit_reminder("agent-rem") == CODE_AUDIT_REMINDER
    record_audit_attempt("agent-rem", "llm_failed", task_id="t1")
    assert code_audit_submit_reminder("agent-rem") == CODE_AUDIT_REMINDER_LLM_FAILED
    record_audit_attempt("agent-rem", "no_callback")
    no_cb = code_audit_submit_reminder("agent-rem")
    assert "no_callback" in no_cb
    assert "attempted" in no_cb
    record_audit_attempt("agent-rem", "no_model")
    no_model = code_audit_submit_reminder("agent-rem")
    assert "no_model" in no_model
    assert "attempted" in no_model
    reset_ledger("agent-rem")


def test_append_notice_idempotent():
    from hiveweave.services.code_audit import CODE_AUDIT_POLICY, append_code_audit_notice

    once = append_code_audit_notice("do the thing")
    assert once == f"do the thing\n{CODE_AUDIT_POLICY}"
    assert append_code_audit_notice(once) == once
    assert append_code_audit_notice("") == CODE_AUDIT_POLICY


# ── collect_worktree_diff 基分支解析（B1）──────────────────


@pytest.mark.asyncio
async def test_collect_worktree_diff_resolves_master_base():
    """默认分支为 master 的仓库：分支段 diff 用 master...HEAD，不硬编码 main。"""
    from hiveweave.services.code_audit import collect_worktree_diff

    calls: list[tuple] = []

    async def fake_git(args, cwd, timeout=30.0):
        calls.append(tuple(args))
        table = {
            ("rev-parse", "--verify", "refs/heads/main"): (False, ""),
            ("rev-parse", "--verify", "refs/heads/master"): (True, ""),
            ("diff", "master...HEAD"): (True, "--- a/x.py\n+++ b/x.py\n-old\n+new"),
            ("diff", "HEAD"): (True, ""),
            ("ls-files", "--others", "--exclude-standard"): (True, ""),
        }
        return table.get(tuple(args), (False, ""))

    with patch("hiveweave.services.git_worktree._git", new=fake_git):
        diff = await collect_worktree_diff(r"C:\fake\wt")

    assert ("diff", "master...HEAD") in calls
    assert ("diff", "main...HEAD") not in calls
    assert "== diff master...HEAD ==" in diff


@pytest.mark.asyncio
async def test_collect_worktree_diff_no_base_keeps_head_and_untracked():
    """无 main/master 的仓库：跳过分支段，保留 HEAD diff + untracked。"""
    from hiveweave.services.code_audit import collect_worktree_diff

    git = _fake_git(
        (("diff", "HEAD"), (True, "--- a/y.py\n+++ b/y.py\n-old\n+new")),
        (("ls-files", "--others", "--exclude-standard"), (True, "fresh.py")),
        (("diff", "main...HEAD"), (True, "SHOULD NOT BE USED")),
        (("diff", "master...HEAD"), (True, "SHOULD NOT BE USED")),
    )
    with patch("hiveweave.services.git_worktree._git", new=git):
        diff = await collect_worktree_diff(r"C:\fake\wt")

    assert "== diff HEAD (uncommitted) ==" in diff
    assert "== untracked fresh.py" in diff  # 文件不存在 → unreadable 标记兜底
    assert "main...HEAD" not in diff
    assert "master...HEAD" not in diff


@pytest.mark.asyncio
async def test_collect_worktree_diff_base_resolve_failure_fails_open():
    """基分支解析抛异常 → 视为 None，不阻断 diff 收集。"""
    from hiveweave.services.code_audit import collect_worktree_diff

    git = _fake_git(
        (("diff", "HEAD"), (True, "--- a/y.py\n+++ b/y.py\n-old\n+new")),
        (("ls-files", "--others", "--exclude-standard"), (True, "")),
    )
    with (
        patch("hiveweave.services.git_worktree._git", new=git),
        patch(
            "hiveweave.services.git_worktree._resolve_base_branch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        diff = await collect_worktree_diff(r"C:\fake\wt")

    assert "== diff HEAD (uncommitted) ==" in diff
    assert "main...HEAD" not in diff


# ── Peer model pick ──────────────────────────────────────────


def test_select_peer_prefers_other_tier():
    from hiveweave.services.code_audit import select_peer_audit_model

    ark = {"id": "m-ark", "model_id": "ark-code-latest"}
    claude = {"id": "m-claude", "model_id": "claude-sonnet"}
    chosen = select_peer_audit_model(
        "deepseek-v4-flash",
        "executor",
        [
            {"tier": "executor", "config": claude},
            {"tier": "management", "config": ark},
        ],
    )
    assert chosen is ark


def test_select_peer_none_when_same_family():
    from hiveweave.services.code_audit import select_peer_audit_model

    flash_b = {"id": "m2", "model_id": "DeepSeek-V4-Flash"}
    chosen = select_peer_audit_model(
        "deepseek-v4-flash",
        "executor",
        [{"tier": "management", "config": flash_b}],
    )
    assert chosen is None


def test_select_peer_stable_lexicographic_same_tier():
    from hiveweave.services.code_audit import select_peer_audit_model

    zzz = {"id": "b", "model_id": "zzz-model"}
    aaa = {"id": "a", "model_id": "aaa-model"}
    chosen = select_peer_audit_model(
        "own-model",
        "executor",
        [
            {"tier": "executor", "config": zzz},
            {"tier": "executor", "config": aaa},
        ],
    )
    assert chosen is aaa


@pytest.mark.asyncio
async def test_resolve_peer_picks_teammate_other_family():
    from hiveweave.services.code_audit import resolve_peer_audit_model

    author = {
        "id": "a1",
        "role": "签到工程师",
        "permission_type": "executor",
        "status": "active",
        "model_id": None,
    }
    boss = {
        "id": "a2",
        "role": "ceo",
        "permission_type": "coordinator",
        "status": "active",
        "model_id": None,
    }
    flash = {"id": "m1", "model_id": "deepseek-v4-flash"}
    ark = {"id": "m2", "model_id": "ark-code-latest"}

    async def fake_resolve(*, tier, preferred=None, skip_model_ids=None):
        return ark if tier == "management" else flash

    mock_org = MagicMock()
    mock_org.list_agents = AsyncMock(return_value=[author, boss])
    mock_org.get_agent = AsyncMock(return_value=author)
    mock_ms = MagicMock()
    mock_ms.resolve_model = fake_resolve

    with (
        patch("hiveweave.services.org.OrgService", return_value=mock_org),
        patch("hiveweave.services.model.ModelService", return_value=mock_ms),
    ):
        cfg, source = await resolve_peer_audit_model("proj", "a1")

    assert source == "peer"
    assert cfg["model_id"] == "ark-code-latest"


@pytest.mark.asyncio
async def test_resolve_peer_falls_back_own_when_team_same_family():
    from hiveweave.services.code_audit import resolve_peer_audit_model

    author = {
        "id": "a1",
        "role": "签到工程师",
        "permission_type": "executor",
        "status": "active",
    }
    boss = {
        "id": "a2",
        "role": "ceo",
        "permission_type": "coordinator",
        "status": "active",
    }
    flash = {"id": "m1", "model_id": "deepseek-v4-flash"}

    async def fake_resolve(*, tier, preferred=None, skip_model_ids=None):
        return flash

    mock_org = MagicMock()
    mock_org.list_agents = AsyncMock(return_value=[author, boss])
    mock_ms = MagicMock()
    mock_ms.resolve_model = fake_resolve

    with (
        patch("hiveweave.services.org.OrgService", return_value=mock_org),
        patch("hiveweave.services.model.ModelService", return_value=mock_ms),
    ):
        cfg, source = await resolve_peer_audit_model("proj", "a1")

    assert source == "own"
    assert cfg is flash


@pytest.mark.asyncio
async def test_resolve_peer_skips_archived_teammate():
    from hiveweave.services.code_audit import resolve_peer_audit_model

    author = {
        "id": "a1",
        "role": "签到工程师",
        "permission_type": "executor",
        "status": "active",
    }
    archived = {
        "id": "a2",
        "role": "ceo",
        "permission_type": "coordinator",
        "status": "archived",
    }
    flash = {"id": "m1", "model_id": "deepseek-v4-flash"}
    ark = {"id": "m2", "model_id": "ark-code-latest"}

    async def fake_resolve(*, tier, preferred=None, skip_model_ids=None):
        return ark if tier == "management" else flash

    mock_org = MagicMock()
    mock_org.list_agents = AsyncMock(return_value=[author, archived])
    mock_ms = MagicMock()
    mock_ms.resolve_model = fake_resolve

    with (
        patch("hiveweave.services.org.OrgService", return_value=mock_org),
        patch("hiveweave.services.model.ModelService", return_value=mock_ms),
    ):
        cfg, source = await resolve_peer_audit_model("proj", "a1")

    assert source == "own"
    assert cfg is flash


@pytest.mark.asyncio
async def test_oneshot_uses_peer_model_not_author_callback():
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    peer = {"id": "uuid-peer", "model_id": "ark-code-latest"}
    oneshot = AsyncMock(return_value="VERDICT: PASS\n")
    own_cb = AsyncMock(return_value="VERDICT: ISSUES\nshould-not-run")
    att_svc = _attestation_service()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\report.txt",
        ),
        patch(
            "hiveweave.services.code_audit.resolve_peer_audit_model",
            new_callable=AsyncMock,
            return_value=(peer, "peer"),
        ),
    ):
        result = await run_code_audit(
            "proj", "agent-1",
            call_llm=own_cb,
            oneshot_llm=oneshot,
        )

    assert result["audited"] is True
    assert result["verdict"] == "PASS"
    assert result["audit_model_id"] == "ark-code-latest"
    assert result["audit_model_source"] == "peer"
    oneshot.assert_awaited()
    sent = oneshot.await_args.args[0]
    assert sent["model_id"] == "ark-code-latest"
    assert sent["supports_thinking"] is False
    assert sent is not peer
    own_cb.assert_not_awaited()
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_oneshot_uses_own_config_when_no_peer_family():
    """Live team shares one model family → still oneshot, author's config."""
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    own = {"id": "uuid-own", "model_id": "deepseek-v4-flash"}
    oneshot = AsyncMock(return_value="VERDICT: PASS\n")
    own_cb = AsyncMock(return_value="should-not-run")
    att_svc = _attestation_service()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\report.txt",
        ),
        patch(
            "hiveweave.services.code_audit.resolve_peer_audit_model",
            new_callable=AsyncMock,
            return_value=(own, "own"),
        ),
    ):
        result = await run_code_audit(
            "proj", "agent-1",
            call_llm=own_cb,
            oneshot_llm=oneshot,
        )

    assert result["audit_model_source"] == "own"
    assert result["audit_model_id"] == "deepseek-v4-flash"
    oneshot.assert_awaited()
    sent = oneshot.await_args.args[0]
    assert sent["model_id"] == "deepseek-v4-flash"
    assert sent["supports_thinking"] is False
    assert sent is not own
    own_cb.assert_not_awaited()
    reset_ledger("agent-1")


@pytest.mark.asyncio
async def test_oneshot_falls_back_to_call_llm_when_peer_resolve_fails():
    from hiveweave.services.code_audit import (
        record_change,
        reset_ledger,
        run_code_audit,
    )

    reset_ledger("agent-1")
    record_change("agent-1", 25)

    oneshot = AsyncMock(return_value="VERDICT: PASS\n")
    own_cb = AsyncMock(return_value="VERDICT: ISSUES\nx.py:1 [low] nit\n")
    att_svc = _attestation_service()
    with (
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new_callable=AsyncMock,
            return_value=r"C:\fake\wt",
        ),
        patch("hiveweave.services.git_worktree._git", new=_diff_git()),
        patch("hiveweave.services.attestation.attestation_service", att_svc),
        patch(
            "hiveweave.tools.executor.ToolExecutor._save_tool_output_file",
            return_value=r"C:\fake\report.txt",
        ),
        patch(
            "hiveweave.services.code_audit.resolve_peer_audit_model",
            new_callable=AsyncMock,
            side_effect=RuntimeError("org down"),
        ),
    ):
        result = await run_code_audit(
            "proj", "agent-1",
            call_llm=own_cb,
            oneshot_llm=oneshot,
        )

    assert result["audited"] is True
    assert result["verdict"] == "ISSUES"
    assert result["audit_model_source"] == "own"
    oneshot.assert_not_awaited()
    own_cb.assert_awaited()
    reset_ledger("agent-1")


def test_format_verdict_includes_peer_model():
    from hiveweave.tools.code_audit import _format_verdict

    out = _format_verdict({
        "verdict": "PASS",
        "lines_audited": 21,
        "audit_model_id": "ark-code-latest",
        "audit_model_source": "peer",
        "attestation_id": "att-1",
    })
    text = out.output if hasattr(out, "output") else str(out)
    assert "ark-code-latest" in text
    assert "团队其它" in text
