"""F2 门禁前置状态可发现性（2026-08-11 slack-clone_01 成本审计根因二）。

硬门撞门 144 次：claim 54%（get_tasks 不透 VERIFY 串行锁）、merge 49%
（worktree/MAIN 未提交不可预检）、submit 25%、review 23%（attestation
基线过期不可见）。本文件覆盖修复：
- get_tasks 每行透出 verify_in_flight / verify_in_flight_id /
  verify_queue_position / attestation_baseline_ok（reviewing 任务）
- merge/submit 工具 dry_run=True 预检：只读返回全部缺失项，零写操作
- merge/submit 真实调用失败时聚合列出全部缺失项
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import attestation as att_module
from hiveweave.services import task as task_module
from hiveweave.services.attestation import attestation_service
from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.git_worktree.git_cmd import _git as _hive_git
from hiveweave.services.task import TaskService
from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)
from hiveweave.tools.task_tools import (
    GetTasksParams,
    SubmitTaskParams,
    get_tasks_tool,
    submit_task_tool,
)

PROJECT_ID = "test-f2-gate-discoverability"
COORD_ID = "coord-f2"
EXEC_ID = "exec-f2"


# ── task_env（get_tasks / submit 测试）────────────────────


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORD_ID, EXEC_ID) else None

        _FAKE_AGENTS = {
            COORD_ID: {
                "id": COORD_ID,
                "name": "测试协调员",
                "short_id": "C001",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXEC_ID: {
                "id": EXEC_ID,
                "name": "测试执行者",
                "short_id": "E001",
                "parent_id": COORD_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return _FAKE_AGENTS.get(aid)

        att_module._migrated.discard(PROJECT_ID)
        task_module._migrated.discard(PROJECT_ID)
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id", fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORD_ID,
                "executor_id": EXEC_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORD_ID, None)
        project_db._agent_cache.pop(EXEC_ID, None)


async def _create_verify(env, svc, title: str) -> str:
    return await svc.create_task(
        project_id=env["project_id"], title=title, description="d",
        creator_id=env["coordinator_id"], assignee_id=env["executor_id"],
        source="system",
    )


async def _run_get_tasks(env):
    from hiveweave.tools import helpers as _helpers

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=env["project_id"]),
    ):
        return await get_tasks_tool(
            GetTasksParams(), env["executor_id"], env["workspace_path"]
        )


def _task_rows(result) -> dict:
    return {str(t["id"]): t for t in (result.extra.get("tasks") or [])}


# ── get_tasks：VERIFY 串行锁 + 队列位置透出 ───────────────


@pytest.mark.asyncio
async def test_get_tasks_verify_in_flight_visible(env):
    """in-flight VERIFY 持有者不挡自己；排队 VERIFY 可见 verify_in_flight。"""
    svc = TaskService()
    v1 = await _create_verify(env, svc, "VERIFY: 验收 A")
    v2 = await _create_verify(env, svc, "VERIFY: 验收 B")
    await svc.claim_task(env["project_id"], v1, env["executor_id"],
                         bypass_verify_serialize=True)
    await svc.start_task(env["project_id"], v1)

    result = await _run_get_tasks(env)

    assert result.success, result.output or result.error
    rows = _task_rows(result)
    assert rows[v1]["verify_in_flight"] is False
    assert rows[v2]["verify_in_flight"] is True
    assert rows[v2]["verify_in_flight_id"] == v1
    out = result.output or ""
    assert "verify_lock: blocked" in out, out
    assert "verify_serial_lock: held by" in out, out


@pytest.mark.asyncio
async def test_get_tasks_verify_queue_position(env):
    """created VERIFY 队列位置透出（0 = 下一个可跑）；非 VERIFY 无位置。"""
    svc = TaskService()
    v1 = await _create_verify(env, svc, "VERIFY: 队列 1")
    v2 = await _create_verify(env, svc, "VERIFY: 队列 2")
    nv = await svc.create_task(
        project_id=env["project_id"], title="普通任务", description="d",
        creator_id=env["coordinator_id"], assignee_id=env["executor_id"],
    )

    result = await _run_get_tasks(env)

    assert result.success, result.output or result.error
    rows = _task_rows(result)
    assert sorted(rows[v]["verify_queue_position"] for v in (v1, v2)) == [0, 1]
    assert rows[nv]["verify_queue_position"] is None
    out = result.output or ""
    assert "verify_queue_position: 0" in out, out
    assert "verify_queue_position: 1" in out, out


@pytest.mark.asyncio
async def test_get_tasks_reviewing_verify_baseline_ok(env):
    """reviewing VERIFY 无 attestation → attestation_baseline_ok=True。"""
    svc = TaskService()
    tid = await _create_verify(env, svc, "VERIFY: 基线 OK")
    await svc.claim_task(env["project_id"], tid, env["executor_id"],
                         bypass_verify_serialize=True)
    await svc.start_task(env["project_id"], tid)
    await svc.submit_task(env["project_id"], tid, {"files": ["a.py"]})
    await svc.start_review(env["project_id"], tid)

    result = await _run_get_tasks(env)

    assert result.success, result.output or result.error
    rows = _task_rows(result)
    assert rows[tid]["attestation_baseline_ok"] is True
    out = result.output or ""
    assert "attestation_baseline: ok" in out, out


@pytest.mark.asyncio
async def test_get_tasks_reviewing_verify_baseline_stale(env):
    """attestation 钉在过期 commit → attestation_baseline_ok=False（审前可见）。"""
    svc = TaskService()
    tid = await _create_verify(env, svc, "VERIFY: 基线 STALE")
    await svc.claim_task(env["project_id"], tid, env["executor_id"],
                         bypass_verify_serialize=True)
    await svc.start_task(env["project_id"], tid)
    await svc.submit_task(
        env["project_id"], tid, {"target_merge_commit": "abc123"}
    )
    await svc.start_review(env["project_id"], tid)
    await attestation_service.create(
        env["project_id"],
        agent_id=env["executor_id"],
        kind="test_run",
        task_id=tid,
        commit_hash="def456",
        exit_code=0,
    )

    result = await _run_get_tasks(env)

    assert result.success, result.output or result.error
    rows = _task_rows(result)
    assert rows[tid]["attestation_baseline_ok"] is False
    out = result.output or ""
    assert "attestation_baseline: STALE" in out, out


# ── merge preflight（service 层）───────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


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


async def _make_branch_with_file(repo: Path, short_id: str, task: str,
                                 filename: str, content: str) -> str:
    gwt = GitWorktreeService()
    res = await gwt.create(str(repo), short_id, task)
    assert res["success"] is True, res
    wt = Path(res["path"])
    (wt / filename).write_text(content, encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")
    return res["branch"]


def _git_status(repo: Path) -> str:
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout or ""


@pytest.mark.asyncio
async def test_merge_preflight_reports_main_dirty_without_mutation(git_repo):
    """preflight 只读：main 脏 → 报告 main_dirty；不清理、不提交、不拆树。"""
    branch = await _make_branch_with_file(
        git_repo, "A004", "feat-x", "a.py", "x = 1\n"
    )
    (git_repo / "README.md").write_text("dirty edit\n", encoding="utf-8")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout.strip()

    gwt = GitWorktreeService()
    report = await gwt.preflight_merge(str(git_repo), "A004", branch)

    assert report["success"] is False
    assert report["dry_run"] is True
    codes = [i["code"] for i in report["missing"]]
    assert "main_dirty" in codes
    assert "worktree_husk" not in codes
    # 无任何改动：README 仍脏、无新提交、worktree 仍在
    st = _git_status(git_repo)
    assert "README.md" in st, st
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout.strip()
    assert head_after == head_before
    assert (git_repo / ".hiveweave" / "worktrees" / "A004").is_dir()


@pytest.mark.asyncio
async def test_merge_preflight_clean_ok_no_mutation(git_repo):
    """preflight 干净：missing 为空；不执行 merge（worktree 不被拆除）。"""
    branch = await _make_branch_with_file(
        git_repo, "A004", "feat-x", "a.py", "x = 1\n"
    )

    gwt = GitWorktreeService()
    report = await gwt.preflight_merge(str(git_repo), "A004", branch)

    assert report["success"] is True
    assert report["missing"] == []
    assert report["already_up_to_date"] is False
    # 没有真实 merge：worktree + 分支都还在
    assert (git_repo / ".hiveweave" / "worktrees" / "A004").is_dir()
    ok, out = await _hive_git(["branch", "--list", branch], str(git_repo))
    assert ok and branch in out


# ── merge tool dry_run ─────────────────────────────────────


async def _call_merge_tool_dry(repo: Path, branch_name: str):
    params = GitWorktreeMergeParams(branchName=branch_name, dry_run=True)
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(repo), "A003", "proj-x")),
    ):
        return await git_worktree_merge_tool(params, "agent-x", str(repo))


@pytest.mark.asyncio
async def test_merge_tool_dry_run_returns_missing_list(git_repo):
    """tool dry_run=True：返回缺失项列表，main 仍脏、worktree 不拆。"""
    branch = await _make_branch_with_file(
        git_repo, "A004", "feat-x", "a.py", "x = 1\n"
    )
    (git_repo / "README.md").write_text("dirty edit\n", encoding="utf-8")

    result = await _call_merge_tool_dry(git_repo, branch)

    assert result.success is True, result.error
    out = result.output or ""
    assert "main_dirty" in out, out
    assert "dry-run" in out, out
    assert result.extra.get("dry_run") is True
    missing = result.extra.get("missing") or []
    assert any(i["code"] == "main_dirty" for i in missing)
    # 零副作用
    assert "README.md" in _git_status(git_repo)
    assert (git_repo / ".hiveweave" / "worktrees" / "A004").is_dir()


@pytest.mark.asyncio
async def test_merge_tool_dry_run_clean_ok(git_repo):
    """tool dry_run=True 干净：提示可执行，无任何 merge 副作用。"""
    branch = await _make_branch_with_file(
        git_repo, "A004", "feat-x", "a.py", "x = 1\n"
    )

    result = await _call_merge_tool_dry(git_repo, branch)

    assert result.success is True, result.error
    assert result.extra.get("missing") == []
    assert (git_repo / ".hiveweave" / "worktrees" / "A004").is_dir()
    assert not (git_repo / "a.py").exists()  # 未合并 → 文件未上 main


# ── merge 真实失败聚合 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_real_failure_aggregates_all_blockers(git_repo):
    """真实 merge 撞 husk + main 脏 → 一条错误同时列出两个缺失项。"""
    husk = git_repo / ".hiveweave" / "worktrees" / "A023"
    husk.mkdir(parents=True)
    branch = "hw/A023/work"
    (git_repo / "README.md").write_text("dirty edit\n", encoding="utf-8")

    async def fake_git(cmd, cwd):
        head = cmd[0] if cmd else ""
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "add":
            return False, "fatal: worktree add failed"
        if head == "worktree" and len(cmd) >= 2 and cmd[1] == "list":
            return True, f"{husk}  {branch}"
        if head == "rev-parse":  # current_branch / HEAD
            return True, branch
        if head == "rev-list":  # ahead 检查
            return True, "1"
        if head == "branch":  # branch --list
            return True, branch
        return True, ""

    gwt = GitWorktreeService()
    with patch(
        "hiveweave.services.git_worktree.service_merge._git",
        new=AsyncMock(side_effect=fake_git),
    ):
        result = await gwt.merge(str(git_repo), "A023", "work", task_id=None)

    assert result["success"] is False
    assert result["reason"] == "precondition_failed"
    msg = result["message"]
    assert "husk" in msg, msg
    assert "README.md" in msg, f"main_dirty 缺失项未聚合:\n{msg}"
    assert "[additional blockers]" in msg, msg
    # 未清理 main 脏文件
    assert "README.md" in _git_status(git_repo)


# ── submit dry_run / 聚合 ──────────────────────────────────


def _submit_params(task_id: str, dry_run: bool, files_changed=None):
    return SimpleNamespace(
        task_id=task_id, summary="done", commit=None,
        files_changed=files_changed, test_output=None,
        tests_passed=True, attestation_ids=None,
        core_interaction_executed=None, failures_acknowledged=None,
        commit_hash=None, env_snapshot=None, dry_run=dry_run,
    )


async def _submit_task_running(env, svc) -> str:
    tid = await svc.create_task(
        project_id=env["project_id"], title="Feature", description="d",
        creator_id=env["coordinator_id"], assignee_id=env["executor_id"],
        tags=["generic_tests"],
    )
    await svc.claim_task(env["project_id"], tid, env["executor_id"])
    await svc.start_task(env["project_id"], tid)
    return tid


def _submit_patches(submit_mock):
    return [
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=frozenset({"bash_test"}),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.find_recent_for_agent",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.task.TaskService.submit_task", submit_mock,
        ),
    ]


async def _run_submit_with(env, svc, params, submit_mock, verify_ids_result):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for cm in _submit_patches(submit_mock):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "hiveweave.services.attestation.attestation_service.verify_ids",
                new=AsyncMock(return_value=verify_ids_result),
            )
        )
        return await submit_task_tool(params, EXEC_ID, env["workspace_path"])


@pytest.mark.asyncio
async def test_submit_dry_run_lists_all_issues_without_writing(env):
    """dry_run：同时列出 attestation 门 + files_changed 缺失；不 submit 不转态。"""
    svc = TaskService()
    tid = await _submit_task_running(env, svc)
    submit_mock = AsyncMock()
    params = _submit_params(tid, dry_run=True, files_changed=["bad/path.py"])
    result = await _run_submit_with(
        env, svc, params, submit_mock,
        (False, "no matching attestation for kind bash_test"),
    )

    assert result.success is True, result.error
    out = result.output or ""
    assert "attestation" in out, out
    assert "bad/path.py" in out, out
    missing = result.extra.get("missing") or []
    assert {i["code"] for i in missing} == {"attestation", "files_changed_missing"}
    submit_mock.assert_not_awaited()
    assert (await svc.get_task(env["project_id"], tid))["status"] == "running"


@pytest.mark.asyncio
async def test_submit_dry_run_clean_ok_no_write(env):
    """dry_run 干净：missing 为空；不 submit、任务仍 running。"""
    svc = TaskService()
    tid = await _submit_task_running(env, svc)
    submit_mock = AsyncMock()
    params = _submit_params(tid, dry_run=True)
    result = await _run_submit_with(env, svc, params, submit_mock, (True, ""))

    assert result.success is True, result.error
    assert result.extra.get("missing") == []
    submit_mock.assert_not_awaited()
    assert (await svc.get_task(env["project_id"], tid))["status"] == "running"


@pytest.mark.asyncio
async def test_submit_real_failure_aggregates_all_issues(env):
    """真实 submit：错误一次列出全部缺失项（不返回第一项就停）。"""
    svc = TaskService()
    tid = await _submit_task_running(env, svc)
    submit_mock = AsyncMock()
    params = _submit_params(tid, dry_run=False, files_changed=["bad/path.py"])
    result = await _run_submit_with(
        env, svc, params, submit_mock,
        (False, "no matching attestation for kind bash_test"),
    )

    assert result.success is False
    err = result.error or ""
    assert "attestation" in err, err
    assert "bad/path.py" in err, err
    assert "[additional blockers]" in err, err
    submit_mock.assert_not_awaited()
    assert (await svc.get_task(env["project_id"], tid))["status"] == "running"
