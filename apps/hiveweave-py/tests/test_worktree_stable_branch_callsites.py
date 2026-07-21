"""P0 worktree 分支命名稳定化 — 调用方适配测试。

覆盖契约（parent 定死的接口）在三个调用点的落地：

1. dispatch_task / review_task 把 task_id 传给 ensure_executor_worktree
   → 分支名符合 hw/<shortId>/t-<taskid8>（无 task_id 时 hw/<shortId>/work）。
2. git_worktree_merge 提供 task_id 时对稳定名精确命中。
3. git_worktree_merge 不传 task_id（或稳定名不存在）时 legacy slug
   分支仍命中 fallback（老分支必须还能 merge）。

测试策略:
  - dispatch / review：mock 边界（DB / inbox / handoff / git_worktree），
    断言 ensure 收到 task_id；分支名按 parent 定死的契约公式核对。
  - merge：真实 git repo + 原生 git 命令造 worktree 分支（不经过
    GitWorktreeService.create，与命名实现解耦 — legacy slug 与稳定名
    分支都按字面量落地），merge 走真实 merge_by_branch；
    compute_branch_name 以契约公式 patch，另设 contract guard 测试
    锁定 git_worktree 真实实现的公式。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from hiveweave.services.dispatch import DispatchService
from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService
from hiveweave.services.org import OrgService
from hiveweave.services.task import TaskService
from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)
from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

PROJECT_ID = "proj-p0"
TASK_ID = "A1B2C3D4-E5F6-7890-ABCD-EF0123456789"
TASK_BRANCH_8 = TASK_ID[:8].lower()  # "a1b2c3d4"


def _contract_branch(short_id: str, task_id: str | None = None) -> str:
    """parent 定死的契约公式（compute_branch_name 的期望行为）。"""
    if task_id:
        return f"hw/{short_id}/t-{task_id[:8].lower()}"
    return f"hw/{short_id}/work"


# org_span 硬门（3049834 起）要求真实组织关系：dispatch 只能派直属下属，
# 且 assignee 须具备 SOURCE_WRITE。参照 b3d0eeb 的修法，注入最小合法
# 指挥链：boss-agent（coordinator，无上级）→ agent-009（executor）。
_FAKE_AGENTS = {
    "boss-agent": {
        "id": "boss-agent",
        "name": "Test Boss",
        "parent_id": None,
        "permission_type": "coordinator",
        "role": "架构师",
        "status": "active",
    },
    "agent-009": {
        "id": "agent-009",
        "name": "Test Executor",
        "parent_id": "boss-agent",
        "permission_type": "executor",
        "role": "engineer",
        "status": "active",
    },
}


async def _fake_get_agent_by_id(aid: str):
    return _FAKE_AGENTS.get(aid)


def _patch_org_span():
    """patch meta.get_agent_by_id，满足 dispatch 的 org_span 三道硬门。"""
    return patch("hiveweave.db.meta.get_agent_by_id", _fake_get_agent_by_id)


def test_compute_branch_name_contract():
    """锁定命名契约 — git_worktree 实现侧的 guard（共享接口不漂移）。"""
    from hiveweave.services.git_worktree import compute_branch_name

    assert compute_branch_name("A009") == "hw/A009/work"
    assert compute_branch_name("A009", TASK_ID) == f"hw/A009/t-{TASK_BRANCH_8}"


# ── 1. dispatch_task 传 task_id ─────────────────────────────


class TestDispatchPassesTaskId:
    async def test_dispatch_passes_task_id_to_ensure(self):
        seen: dict = {}

        async def fake_ensure(project_id, agent_id, task_name=None, task_id=None):
            seen["task_id"] = task_id
            seen["task_name"] = task_name  # 兼容保留，照常传
            seen["branch"] = _contract_branch("A009", task_id)
            return {
                "success": True,
                "path": "/tmp/wt-A009",
                "short_id": "A009",
                "branch": seen["branch"],
            }

        update_mock = AsyncMock()
        with (
            _patch_org_span(),
            patch("hiveweave.services.dispatch._ensure_schema", AsyncMock()),
            patch("hiveweave.services.dispatch._execute", AsyncMock()),
            patch.object(
                TaskService, "create_task", AsyncMock(return_value=TASK_ID)
            ),
            patch.object(TaskService, "update_task", update_mock),
            patch.object(
                OrgService, "resolve_agent",
                AsyncMock(return_value={
                    "id": "agent-009",
                    "permission_type": "executor",
                    "short_id": "A009",
                }),
            ),
            patch(
                "hiveweave.services.git_worktree.ensure_executor_worktree",
                fake_ensure,
            ),
            patch(
                "hiveweave.services.git_worktree."
                "pin_dispatch_message_to_worktree",
                Mock(side_effect=lambda desc, short_id, worktree_path:
                     f"{desc}\n[worktree: {worktree_path}]"),
            ),
            patch.object(InboxService, "send_message", AsyncMock()),
            patch.object(
                HandoffService, "create_handoff", AsyncMock(return_value="ho-1")
            ),
        ):
            result = await DispatchService().dispatch_task(
                PROJECT_ID, "boss-agent", "agent-009", "实现登录模块"
            )

        assert result["success"] is True
        assert result["task_id"] == TASK_ID
        # ensure 收到 canonical task_id → 分支名符合契约
        assert seen["task_id"] == TASK_ID
        assert seen["branch"] == f"hw/A009/t-{TASK_BRANCH_8}"
        # 钉路径后的 description 回写 task 行（与原行为一致）
        desc_updates = [
            c.kwargs["description"] for c in update_mock.call_args_list
            if c.kwargs.get("description")
        ]
        assert desc_updates and "[worktree:" in desc_updates[0]

    async def test_dispatch_with_existing_task_id_passes_it_to_ensure(self):
        """existing_task_id 复用路径同样把 task_id 传给 ensure。"""
        ensure_mock = AsyncMock(return_value={
            "success": True, "path": "/tmp/wt-A009", "short_id": "A009",
        })
        with (
            _patch_org_span(),
            patch("hiveweave.services.dispatch._ensure_schema", AsyncMock()),
            patch("hiveweave.services.dispatch._execute", AsyncMock()),
            patch.object(TaskService, "update_task", AsyncMock()),
            patch.object(
                OrgService, "resolve_agent",
                AsyncMock(return_value={
                    "id": "agent-009",
                    "permission_type": "executor",
                    "short_id": "A009",
                }),
            ),
            patch(
                "hiveweave.services.git_worktree.ensure_executor_worktree",
                ensure_mock,
            ),
            patch(
                "hiveweave.services.git_worktree."
                "pin_dispatch_message_to_worktree",
                Mock(side_effect=lambda desc, short_id, worktree_path: desc),
            ),
            patch.object(InboxService, "send_message", AsyncMock()),
            patch.object(
                HandoffService, "create_handoff", AsyncMock(return_value="ho-1")
            ),
        ):
            result = await DispatchService().dispatch_task(
                PROJECT_ID, "boss-agent", "agent-009", "实现登录模块",
                existing_task_id=TASK_ID,
            )

        assert result["success"] is True
        assert result["task_id"] == TASK_ID
        assert ensure_mock.call_args.kwargs["task_id"] == TASK_ID


# ── 2. review_task 传 task_id ───────────────────────────────


class TestReviewPassesTaskId:
    async def test_review_task_passes_task_id_to_ensure(self):
        ensure_mock = AsyncMock(return_value={
            "success": True, "path": "/tmp/wt-A004", "short_id": "A004",
        })
        task_row = {
            "id": TASK_ID,
            "assignee_id": "agent-004",
            "status": "reviewing",
            "title": "实现登录模块",
            "description": "d",
            "evidence": {"tests_passed": True},
            "tags": [],
        }
        with (
            patch("hiveweave.tools.task_tools.get_project_id",
                  AsyncMock(return_value=PROJECT_ID)),
            patch.object(TaskService, "get_task",
                         AsyncMock(return_value=dict(task_row))),
            patch.object(TaskService, "review_task", AsyncMock()),
            patch("hiveweave.services.attestation.resolve_task_policy",
                  Mock(return_value="docs_only")),
            patch("hiveweave.services.attestation.required_attestation_kinds",
                  Mock(return_value=[])),
            patch("hiveweave.services.git_worktree.ensure_executor_worktree",
                  ensure_mock),
            patch("hiveweave.services.worktree_review.review_worktree_gate",
                  AsyncMock(return_value=(None, {}))),
            patch("hiveweave.services.worktree_review.agent_worktree_path",
                  AsyncMock(return_value="/tmp/wt-A004")),
            patch.object(OrgService, "resolve_agent",
                         AsyncMock(return_value={"short_id": "A004"})),
            patch.object(InboxService, "send_message", AsyncMock()),
            patch("hiveweave.agents.trigger.trigger_subordinate", AsyncMock()),
        ):
            result = await review_task_tool(
                ReviewTaskParams(taskId=TASK_ID, decision="approve"),
                "coord-agent", "/tmp",
            )

        assert result.success is True, result.error
        ensure_mock.assert_awaited_once()
        assert ensure_mock.call_args.kwargs["task_id"] == TASK_ID
        # 契约: 该 task_id 对应稳定分支 hw/A004/t-<taskid8>
        assert _contract_branch("A004", TASK_ID) == f"hw/A004/t-{TASK_BRANCH_8}"


# ── 3-5. merge 稳定名命中 / legacy fallback ──────────────────


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


def _make_worktree_branch(repo: Path, branch: str, filename: str) -> Path:
    """用原生 git 造 worktree + 分支 + 一个提交。

    不经过 GitWorktreeService.create — 与命名实现解耦，legacy slug 与
    稳定名分支都按字面量落地，保证老分支场景不被 P0 命名变化影响。
    """
    wt = repo.parent / ("wt-" + branch.replace("/", "_"))
    _git(repo, "worktree", "add", "-b", branch, str(wt))
    (wt / filename).write_text(f"print('from {branch}')\n", encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")
    return wt


def _merge_patches(repo: Path):
    """merge 工具的通用边界 patch（调用者上下文 + VERIFY nudge）。"""
    return (
        patch(
            "hiveweave.tools.misc_tools._get_worktree_context",
            AsyncMock(return_value=(str(repo), "A003", PROJECT_ID)),
        ),
        patch(
            "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
            AsyncMock(return_value=0),
        ),
    )


def _task_lookup_patches():
    """稳定路径的任务/assignee 查询 patch（compute 按契约公式）。"""
    return (
        patch.object(
            TaskService, "get_task",
            AsyncMock(return_value={"id": TASK_ID, "assignee_id": "agent-004"}),
        ),
        patch.object(
            OrgService, "resolve_agent",
            AsyncMock(return_value={"id": "agent-004", "short_id": "A004"}),
        ),
        patch(
            "hiveweave.services.git_worktree.compute_branch_name",
            Mock(side_effect=_contract_branch),
        ),
    )


class TestMergeStableBranch:
    async def test_task_id_exact_hits_stable_branch(self, git_repo: Path):
        """提供 task_id → 按契约精确命中 hw/A004/t-<taskid8> 并合并。"""
        stable_branch = _contract_branch("A004", TASK_ID)
        _make_worktree_branch(git_repo, stable_branch, "login.py")

        p1, p2 = _merge_patches(git_repo)
        p3, p4, p5 = _task_lookup_patches()
        params = GitWorktreeMergeParams(branchName="whatever", taskId=TASK_ID)
        with p1, p2, p3, p4, p5:
            result = await git_worktree_merge_tool(
                params, "agent-003", str(git_repo)
            )

        assert result.success is True, result.error
        # 稳定分支内容已合入 main —— branchName='whatever' 在 legacy 路径
        # 下必然解析失败，成功即证明稳定路径精确命中（merge 日志亦指向
        # hw/A004/t-a1b2c3d4）。分支/目录的清理语义属 git_worktree 核心
        # （P0 delete-safety 可能按设计保留分支），不在本测试断言范围。
        assert (git_repo / "login.py").exists()

    async def test_task_id_falls_back_to_legacy_when_stable_missing(
        self, git_repo: Path
    ):
        """task_id 提供但稳定分支不存在 → 回落 legacy 解析，老分支照合。"""
        _make_worktree_branch(git_repo, "hw/A004/feat-x", "featx.py")

        p1, p2 = _merge_patches(git_repo)
        p3, p4, p5 = _task_lookup_patches()
        params = GitWorktreeMergeParams(branchName="feat-x", taskId=TASK_ID)
        with p1, p2, p3, p4, p5:
            result = await git_worktree_merge_tool(
                params, "agent-003", str(git_repo)
            )

        assert result.success is True, result.error
        assert (git_repo / "featx.py").exists()

    async def test_legacy_slug_branch_hits_fallback(self, git_repo: Path):
        """不传 task_id — legacy slug 分支（老命名）仍命中全局搜索 fallback。"""
        _make_worktree_branch(git_repo, "hw/A004/feat-legacy-x", "legacy.py")

        p1, p2 = _merge_patches(git_repo)
        params = GitWorktreeMergeParams(branchName="feat-legacy-x")
        with p1, p2:
            result = await git_worktree_merge_tool(
                params, "agent-003", str(git_repo)
            )

        assert result.success is True, result.error
        assert (git_repo / "legacy.py").exists()
