"""2026-08-11 slack-clone_01 ROUND3 三平台 bug 修复回归.

- Bug 1: husk 死区 —— reconcile 曾跳过 active agent 的 husk（无 .git 非活树）
  + rmtree 失败静默；现统一 _rmtree_husk（protected husk 也清 + 失败计数重试告警）
- Bug 2: merge husk 自动修复 —— D3 预条件检测到 husk 自动重建规范 worktree
- Bug 3: approve evidence gate 失败自动 rework —— assignee 可立即重交
  （不再死等 reviewer 手动 rework）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree.reconcile import (
    _HUSK_RETRY_MAX,
    _husk_remove_failures,
    _rmtree_husk,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.result import ToolResult
from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams  # noqa: F401
from hiveweave.tools.tasks.review import (
    ReviewTaskParams,
    _auto_rework_on_evidence_gate,
    review_task_tool,
)

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


@pytest.fixture(autouse=True)
def clean_husk_retry_state():
    """模块级 _husk_remove_failures 跨测试泄漏（审计发现：失败测试先跑会让
    成功测试断言全空失败）——每个测试前后清空。"""
    _husk_remove_failures.clear()
    yield
    _husk_remove_failures.clear()


# ── Bug 1: husk 删除 + 失败重试 ─────────────────────────────


def test_rmtree_husk_success_clears_retry_count(tmp_path):
    husk = tmp_path / "A023"
    husk.mkdir()
    report = {}
    _husk_remove_failures[str(husk).lower()] = 2  # 之前失败过
    with patch(
        "hiveweave.services.git_worktree.reconcile.shutil.rmtree",
        side_effect=lambda *a, **k: husk.rmdir(),  # 模拟成功删除
    ):
        _rmtree_husk(husk, report, str(tmp_path), source="husk")
    assert report["removed_dirs"] == 1
    assert not _husk_remove_failures  # 重试计数清除


def test_rmtree_husk_failure_counts_and_escalates(tmp_path, monkeypatch):
    husk = tmp_path / "A023"
    husk.mkdir()
    report = {}
    _husk_remove_failures.clear()
    # 模拟 rmtree 失败（Windows Device busy —— 目录仍在）
    def fake_rmtree(*a, **k):
        pass

    with patch(
        "hiveweave.services.git_worktree.reconcile.shutil.rmtree",
        side_effect=fake_rmtree,
    ):
        for i in range(1, _HUSK_RETRY_MAX + 1):
            _rmtree_husk(husk, report, str(tmp_path), source="husk")
    assert len(report["errors"]) == _HUSK_RETRY_MAX
    assert f"attempt {_HUSK_RETRY_MAX}" in report["errors"][-1]
    assert _husk_remove_failures[str(husk).lower()] == _HUSK_RETRY_MAX


# ── Bug 2: merge husk 自动修复 ──────────────────────────────


def _make_gwt():
    gwt = GitWorktreeService()
    return gwt


@pytest.mark.asyncio
async def test_auto_repair_husk_success(tmp_path, monkeypatch):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)
    branch = "hw/A023/work"

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(True, "added")),
        ) as git,
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), branch
        )
    assert err is None
    assert not husk.exists()
    git.assert_awaited_once()
    cmd = git.await_args.args[0]  # _git(args, cwd)
    assert cmd[:2] == ["worktree", "add"]


@pytest.mark.asyncio
async def test_auto_repair_husk_locked_dir_returns_error(tmp_path):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: None,  # 删不掉（锁住）
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(True, "")),
        ),
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), "hw/A023/work"
        )
    assert err is not None
    assert "locked" in err or "remove husk" in err


@pytest.mark.asyncio
async def test_auto_repair_husk_git_add_failure_returns_error(tmp_path):
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(return_value=(False, "fatal: branch in use")),
        ),
    ):
        err = await gwt._auto_repair_husk(
            str(tmp_path), "A023", str(husk), "hw/A023/work"
        )
    assert err is not None
    assert "worktree add" in err


@pytest.mark.asyncio
async def test_merge_preconditions_husk_auto_repairs(tmp_path, monkeypatch):
    """D3 预条件：husk 分支自动修复后继续校验（不再死报错误）。"""
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)
    branch = "hw/A023/work"

    calls = {"n": 0}

    async def fake_git(cmd, cwd):
        head = cmd[0] if cmd else ""
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "add":
            # 模拟 add 成功：重建目录 + 出现 .git 文件
            husk.mkdir(parents=True, exist_ok=True)
            (husk / ".git").write_text("gitdir: x")
            calls["n"] += 1
            return True, ""
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "list":
            return True, f"{husk}  {branch}"
        if head == "rev-parse":  # current_branch
            return True, branch
        if head == "rev-list":  # ahead 检查：1 个提交领先（非 no-op）
            return True, "1"
        return True, ""

    with (
        patch(
            "hiveweave.services.git_worktree.service_merge.shutil.rmtree",
            side_effect=lambda *a, **k: husk.rmdir() if husk.exists() else None,
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._git",
            new=AsyncMock(side_effect=fake_git),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._has_git",
            side_effect=lambda p: (Path(p) / ".git").exists(),
        ),
        patch(
            "hiveweave.services.git_worktree.service_merge._current_branch",
            new=AsyncMock(return_value=branch),
        ),
    ):
        result = await gwt._validate_merge_preconditions(
            str(tmp_path), "A023", branch
        )
    assert result is None  # 修复后预条件全过 → 继续 merge
    assert calls["n"] == 1


# ── Bug 1b: delete() removed 契约诚实化 ─────────────────────


@pytest.mark.asyncio
async def test_delete_reports_removed_false_when_dir_survives(tmp_path):
    """delete() rmtree 失败（目录残留）→ removed=False 透出（不再假装成功）。"""
    gwt = _make_gwt()
    husk = tmp_path / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)

    with (
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._git",
            new=AsyncMock(return_value=(False, "")),  # remove 链全失败
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle.shutil.rmtree",
            side_effect=lambda *a, **k: None,  # 删不掉（Device busy）
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._has_git",
            return_value=False,
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle._current_branch",
            new=AsyncMock(return_value="hw/A023/work"),
        ),
        patch(
            "hiveweave.services.git_worktree.service_lifecycle.LifecycleMixin._dispose_branch",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await gwt.delete(str(tmp_path), "A023")
    assert result["removed"] is False
    assert husk.exists()


# ── Bug 3: approve evidence gate 自动 rework ────────────────

async def _submit_approved_flow(ts, pid, tid):
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid, tid, evidence={
            "tests_passed": True,
            "test_output": "ok",
            "files_changed": ["bad/path.py"],
        }
    )


@pytest.mark.asyncio
async def test_evidence_gate_deny_auto_rework(task_env):
    """approve 时 files_changed 校验失败 → 自动 rework，任务转回 running，
    assignee 收到通知 —— 不再死等 reviewer 手动 rework。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)
    assert (await ts.get_task(pid, tid))["status"] == "submitted"

    deny_msg = "files_changed contains bad/path.py not in worktree"
    with (
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            new=AsyncMock(return_value=(None, {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            new=AsyncMock(return_value=deny_msg),
        ),
        # 绕过前序 attestation/reviewer-execution gate（本测试只关注
        # evidence gate 的自动 rework）
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.reviewer_required_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ) as trigger,
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is False
    assert "auto-rework" in (result.error or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"
    # assignee 收到 rework 通知
    assert send.await_count >= 1
    assert "REWORK REQUESTED" in send.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_auto_rework_ignores_verify_and_plain_gate_deny(task_env):
    """非 evidence-gate 拒绝（review_worktree_gate）不触发自动 rework
    （环境类问题不自动循环）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)

    with (
        patch(
            "hiveweave.services.worktree_review.review_worktree_gate",
            new=AsyncMock(return_value=("no worktree path", {})),
        ),
        patch(
            "hiveweave.services.worktree_review.check_evidence_verifiable",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is False
    assert "auto-rework" not in (result.error or "")
    assert (await ts.get_task(pid, tid))["status"] == "submitted"


@pytest.mark.asyncio
async def test_auto_rework_on_evidence_gate_direct(task_env):
    """_auto_rework_on_evidence_gate 直接调用：submitted → reviewing → running，
    review obligation fulfill。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    await _submit_approved_flow(ts, pid, tid)

    with (
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
    ):
        await _auto_rework_on_evidence_gate(
            pid, MagicMock(task_id=tid, feedback=None), COORD, None,
            "bad files_changed",
        )
    assert (await ts.get_task(pid, tid))["status"] == "running"


# ── E2 工具层配套（2026-08-25 TEST_DSH_28 实锤）───────────────
# approve 被 service 强制 rework（verdict=FAIL）后，工具层不得按 approve
# 收尾：不发 [TASK APPROVED]、不注入 merge/close；按 rework 通知+短路。


@pytest.mark.asyncio
async def test_tool_approve_forced_rework_no_approved_notice(task_env):
    """非 VERIFY 任务 + verdict=FAIL → review_task_tool(approve) 后任务
    running、assignee 收到 REWORK REQUESTED（非 TASK APPROVED）、无
    merge/close 注入、返回文案点名 verdict gate。

    变异: 删掉工具层 forced_rework 短路 → 通知变 [TASK APPROVED] →
    本测试失败（误发「已批准」+ 对运行中任务注入 merge pending 复现）。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "[probe] 非 VERIFY 的 FAIL 探针", "d",
        creator_id=COORD, assignee_id=EXEC,
    )
    await ts.claim_task(pid, tid, EXEC)
    await ts.start_task(pid, tid)
    await ts.submit_task(
        pid,
        tid,
        evidence={
            "verdict": "FAIL",
            "blocking_issues": ["/_admin 404"],
            "tests_passed": True,  # 满足 approve 前 attestation 软校验
        },
    )
    await ts.start_review(pid, tid)

    with (
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.reviewer_required_kinds",
            return_value=[],
        ),
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(),
        ) as send,
        patch(
            "hiveweave.agents.trigger.trigger_subordinate",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.services.worktree_review.agent_worktree_path",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.worktree_commits_ahead",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            new=AsyncMock(return_value=None),
        ),
        patch(
            # 加固（审计 m4）：forced_rework 时 merge pending 不得注入
            "hiveweave.tools.tasks.review._inject_merge_pending_wake",
            new=AsyncMock(),
        ) as merge_wake,
    ):
        params = ReviewTaskParams(task_id=tid, decision="approve")
        result = await review_task_tool(params, COORD, "/tmp/ws")

    assert result.success is True
    assert "forced back to rework" in (result.output or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"
    msgs = [c.kwargs.get("message") or "" for c in send.await_args_list]
    assert any("REWORK REQUESTED" in m for m in msgs), msgs
    assert not any("TASK APPROVED" in m for m in msgs), msgs
    assert not any("MERGE PENDING" in m for m in msgs), msgs
    merge_wake.assert_not_awaited()
