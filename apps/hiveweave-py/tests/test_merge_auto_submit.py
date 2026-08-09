"""merge → 任务结转：同分支 running 任务自动 submit（里程碑主任务死锁治愈）。

回归场景（后台 M0 里程碑现场）：owner 自营的「里程碑主任务」保持 running，
分支实际已 merge 进 main，但无任何「merge → 任务提交/关账」钩子，stall 只
催办不闭环，下游里程碑连带等待。

修复：merge 成功（或分支已合入 main 被 stall 扫描发现）时，反查稳定命名
分支 hw/<sid>/t-<taskid8> 对应的 running 任务自动置 submitted。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree.service_merge import (
    auto_submit_running_task_after_merge,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _agent(sid: str, aid: str) -> dict:
    return {"id": aid, "short_id": sid}


def _running_task(tid: str, assignee: str, title: str = "M0 里程碑") -> dict:
    return {"id": tid, "status": "running", "assignee_id": assignee, "title": title}


# ── helper：auto_submit_running_task_after_merge ─────────────


@pytest.mark.asyncio
async def test_helper_skips_legacy_slug_branch():
    """legacy slug 分支无法可靠反查任务 → 跳过。"""
    n, titles = await auto_submit_running_task_after_merge(
        "proj-x", "/fake/ws",
        branch="hw/A001/some-task-slug",
        short_id="A001", merged_by="ag-x",
    )
    assert n == 0 and titles == []


@pytest.mark.asyncio
async def test_helper_submits_running_task_of_branch_owner(monkeypatch):
    """t-<id8> 分支 → 匹配 assignee 的 running 任务自动 submit，evidence 带标记。"""
    submit_mock = AsyncMock()
    monkeypatch.setattr(
        TaskService, "list_tasks",
        AsyncMock(return_value=[_running_task("abc12345-0000", "ag-007")]),
    )
    monkeypatch.setattr(TaskService, "submit_task", submit_mock)
    monkeypatch.setattr(
        "hiveweave.services.org.OrgService.list_agents",
        AsyncMock(return_value=[_agent("A007", "ag-007")]),
    )

    n, titles = await auto_submit_running_task_after_merge(
        "proj-x", "/fake/ws",
        branch="hw/A007/t-abc12345",
        short_id="A007", merged_by="ag-003", merge_commit="c0ffee",
        already_on_main=True,
    )

    assert n == 1 and titles == ["M0 里程碑"]
    assert submit_mock.await_count == 1
    args, kwargs = submit_mock.await_args
    assert args[0] == "proj-x" and args[1] == "abc12345-0000"
    ev = args[2]
    assert ev["auto_submitted_by_merge"] is True
    assert ev["merged_by"] == "ag-003" and ev["merge_commit"] == "c0ffee"


@pytest.mark.asyncio
async def test_helper_skips_foreign_assignee_and_non_running(monkeypatch):
    """非分支 owner 的 running 任务 + owner 的非 running 任务都不动。"""
    submit_mock = AsyncMock()
    monkeypatch.setattr(
        TaskService, "list_tasks",
        AsyncMock(return_value=[
            _running_task("abc12345-0001", "ag-OTHER"),   # 他人任务
            {**_running_task("abc12345-0002", "ag-007"), "status": "submitted"},  # 已提交
            {**_running_task("abc12345-0003", "ag-007"), "status": "claimed"},    # 未开始
        ]),
    )
    monkeypatch.setattr(TaskService, "submit_task", submit_mock)
    monkeypatch.setattr(
        "hiveweave.services.org.OrgService.list_agents",
        AsyncMock(return_value=[_agent("A007", "ag-007")]),
    )

    n, titles = await auto_submit_running_task_after_merge(
        "proj-x", "/fake/ws",
        branch="hw/A007/t-abc12345",
        short_id="A007", merged_by="ag-003", already_on_main=True,
    )

    assert n == 0 and titles == []
    assert submit_mock.await_count == 0


@pytest.mark.asyncio
async def test_helper_checks_branch_merged_when_not_assumed(monkeypatch):
    """already_on_main=False 时须 git branch --merged 确认分支已合入才 submit。"""
    submit_mock = AsyncMock()
    monkeypatch.setattr(
        TaskService, "list_tasks",
        AsyncMock(return_value=[_running_task("abc12345-0000", "ag-007")]),
    )
    monkeypatch.setattr(TaskService, "submit_task", submit_mock)
    monkeypatch.setattr(
        "hiveweave.services.org.OrgService.list_agents",
        AsyncMock(return_value=[_agent("A007", "ag-007")]),
    )
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.service_merge._resolve_base_branch",
        AsyncMock(return_value="main"),
    )
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.service_merge._git",
        AsyncMock(return_value=(True, "hw/A007/t-abc12345\n")),
    )

    n, _ = await auto_submit_running_task_after_merge(
        "proj-x", "/fake/ws",
        branch="hw/A007/t-abc12345",
        short_id="A007", merged_by="system", already_on_main=False,
    )
    assert n == 1


@pytest.mark.asyncio
async def test_helper_no_submit_when_branch_not_merged(monkeypatch):
    """分支未合入 main → 不 submit（stall 维持原催办）。"""
    submit_mock = AsyncMock()
    monkeypatch.setattr(
        TaskService, "list_tasks",
        AsyncMock(return_value=[_running_task("abc12345-0000", "ag-007")]),
    )
    monkeypatch.setattr(TaskService, "submit_task", submit_mock)
    monkeypatch.setattr(
        "hiveweave.services.org.OrgService.list_agents",
        AsyncMock(return_value=[_agent("A007", "ag-007")]),
    )
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.service_merge._resolve_base_branch",
        AsyncMock(return_value="main"),
    )
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.service_merge._git",
        AsyncMock(return_value=(True, "")),
    )

    n, _ = await auto_submit_running_task_after_merge(
        "proj-x", "/fake/ws",
        branch="hw/A007/t-abc12345",
        short_id="A007", merged_by="system", already_on_main=False,
    )
    assert n == 0
    assert submit_mock.await_count == 0


# ── git_worktree_merge 工具：merge 成功后自动结转 ────────────


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


async def _make_stable_branch(repo: Path, short_id: str, task_id: str,
                              filename: str) -> None:
    """创建稳定命名分支 hw/<sid>/t-<taskid8> 并提交一个文件。"""
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, "milestone")
    assert res["success"] is True, res
    wt = Path(res["path"])
    _git(wt, "branch", "-m", res["branch"], f"hw/{short_id}/t-{task_id[:8].lower()}")
    (wt / filename).write_text("print('milestone work')\n", encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")


@pytest.mark.asyncio
async def test_merge_tool_auto_submits_running_task_after_merge(
    git_repo: Path,
) -> None:
    """核心回归：coordinator 合并他人 t- 分支成功 → running 任务自动 submit。"""
    await _make_stable_branch(git_repo, "A007", "abc12345-0000", "m0.py")

    submit_mock = AsyncMock()
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(git_repo), "A003", "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch(
        "hiveweave.services.obligation.ObligationLedger.fulfill_by_owner",
        new=AsyncMock(),
    ), patch(
        "hiveweave.services.task.TaskService.migrate_orphan_approved",
        new=AsyncMock(),
    ), patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[_agent("A007", "ag-007")]),
    ), patch(
        "hiveweave.services.task.TaskService.list_tasks",
        new=AsyncMock(return_value=[_running_task("abc12345-0000", "ag-007")]),
    ), patch(
        "hiveweave.services.task.TaskService.submit_task",
        new=submit_mock,
    ):
        params = GitWorktreeMergeParams(branchName="hw/A007/t-abc12345")
        result = await git_worktree_merge_tool(params, "agent-x", str(git_repo))

    assert result.success is True, result.error
    assert (git_repo / "m0.py").exists()
    assert "Auto-submitted" in result.output
    assert "M0 里程碑" in result.output
    assert submit_mock.await_count == 1
    args, _ = submit_mock.await_args
    assert args[0] == "proj-x" and args[1] == "abc12345-0000"
    assert args[2]["auto_submitted_by_merge"] is True


# ── stall 循环治愈：running 任务分支已合入 → 自动 submit，不再催办 ──


def _stall_state(project_id: str, stale_running: int) -> dict:
    return {
        "project_id": project_id,
        "ledger_nudge_cooldowns": {},
        "duty_session_started_at_ms": stale_running,
        "silence_trackers": {},
    }


def _running_stall_task(tid: str, assignee: str, updated_at: int) -> dict:
    return {
        "id": tid,
        "creator_id": assignee,
        "assignee_id": assignee,
        "status": "running",
        "title": "M0 里程碑",
        "tags": ["milestone"],
        "updated_at": updated_at,
    }


@pytest.mark.asyncio
async def test_stall_auto_submits_merged_running_task(monkeypatch):
    """running 任务分支已合入 main → 自动 submit，[TASK STALL] 不再发。"""
    from hiveweave.services import game_time as gt

    project_id = "proj-stall-heal"
    now = 1_700_000_000_000
    stale_running = now - gt.TASK_STALL_THRESHOLDS["running"] - 1000
    gt._states[project_id] = _stall_state(project_id, stale_running)

    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"id": "m1", "should_wake": True}

    svc = gt.GameTimeService(project_id)
    svc._watchdog_trigger = AsyncMock()

    with patch(
        "hiveweave.db.meta.query_one",
        new=AsyncMock(return_value={"is_started": 1}),
    ), patch(
        "hiveweave.services.system_state.system_state.paused",
        return_value=False,
    ), patch(
        "hiveweave.services.task.TaskService.list_tasks",
        new=AsyncMock(return_value=[_running_stall_task("abc12345-0000", "ag-007", stale_running)]),
    ), patch(
        "hiveweave.services.task.TaskService._is_verify_task",
        staticmethod(lambda t: False),
    ), patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[_agent("A007", "ag-007")]),
    ), patch(
        "hiveweave.services.inbox.InboxService",
        return_value=FakeInbox(),
    ), patch(
        "hiveweave.services.game_time._auto_submit_stalled_running_task_if_merged",
        new=AsyncMock(return_value=True),
    ), patch("time.time", return_value=now / 1000):
        await svc._nudge_stale_ledger(project_id)

    assert sent == [], "已治愈的任务不应再收到 [TASK STALL]"
    assert svc._watchdog_trigger.await_count == 0
    gt._states.pop(project_id, None)


@pytest.mark.asyncio
async def test_stall_nudges_when_task_not_merged(monkeypatch):
    """分支未合入 → 维持原 [TASK STALL] 催办（自动 submit 不触发）。"""
    from hiveweave.services import game_time as gt

    project_id = "proj-stall-nudge"
    now = 1_700_000_000_000
    stale_running = now - gt.TASK_STALL_THRESHOLDS["running"] - 1000
    gt._states[project_id] = _stall_state(project_id, stale_running)

    sent: list[dict] = []

    class FakeInbox:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"id": "m1", "should_wake": True}

    svc = gt.GameTimeService(project_id)
    svc._watchdog_trigger = AsyncMock()

    with patch(
        "hiveweave.db.meta.query_one",
        new=AsyncMock(return_value={"is_started": 1}),
    ), patch(
        "hiveweave.services.system_state.system_state.paused",
        return_value=False,
    ), patch(
        "hiveweave.services.task.TaskService.list_tasks",
        new=AsyncMock(return_value=[_running_stall_task("abc12345-0000", "ag-007", stale_running)]),
    ), patch(
        "hiveweave.services.task.TaskService._is_verify_task",
        staticmethod(lambda t: False),
    ), patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[_agent("A007", "ag-007")]),
    ), patch(
        "hiveweave.services.inbox.InboxService",
        return_value=FakeInbox(),
    ), patch(
        "hiveweave.services.game_time._auto_submit_stalled_running_task_if_merged",
        new=AsyncMock(return_value=False),
    ), patch("time.time", return_value=now / 1000):
        await svc._nudge_stale_ledger(project_id)

    assert any(
        m["message"].startswith("[TASK STALL]") for m in sent
    ), "未合入的任务应收到 [TASK STALL] 催办"
    assert svc._watchdog_trigger.await_count == 1
    gt._states.pop(project_id, None)
