"""TEST_DSH_62 P5/L8 — 回执 outcome token / found 语义 / 同态幂等回归.

2026-09-18 施工组5 四项契约的钉子：
1. git_worktree_merge 连续两次同一分支 → 第一次 outcome=merged、
   第二次 outcome=already_merged（28 次调用 8 真合并/10 已合并/9 零
   commit 三种语义此前全压成 success=true 一句文案）；
2. git_worktree_status 查他人不存在的 worktree → success + found=false
   （真合并必拆 worktree，查不到是正常答案而非错误）；
3. update_task_status 同态幂等：running→running no-op；blocked→blocked
   带元数据走 outcome=metadata_refreshed（无状态转移事件），不带则
   no-op（状态机自环豁免在工具层，服务层 _TRANSITIONS 不动）；
4. review_task 重复 approve / blocked 的拒绝文案按状态拆分，不再共用
   "must be 'submitted' or 'reviewing'" 模板。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.task import TaskService


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (r.stdout or "").strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")
    _git(root, "branch", "-M", "main")


# ── 1. merge outcome tokens ───────────────────────────────


@pytest.mark.asyncio
async def test_merge_twice_same_branch_tokens_differ(tmp_path: Path):
    """真合并 → 已合并：两次回执的 outcome token 必须可区分。"""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _git(main, "checkout", "-b", "hw/A004/work")
    (main / "feature.py").write_text("feat\n", encoding="utf-8")
    _git(main, "add", "feature.py")
    _git(main, "commit", "-m", "feat")
    _git(main, "checkout", "main")

    gwt = GitWorktreeService()
    first = await gwt.merge_by_branch(
        str(main), "hw/A004/work", target_branch="main"
    )
    assert first.get("success") is True, first
    msg1 = str(first.get("message") or "")
    assert msg1.startswith("outcome=merged:"), msg1

    second = await gwt.merge_by_branch(
        str(main), "hw/A004/work", target_branch="main"
    )
    assert second.get("success") is True, second
    msg2 = str(second.get("message") or "")
    assert msg2.startswith("outcome=already_merged:"), msg2
    assert msg1.split(":")[0] != msg2.split(":")[0]
    # 已合并回执保留防重复调用指引（TEST_DSH_31 noop 重试教训）
    assert "git_worktree_merge again" in msg2


@pytest.mark.asyncio
async def test_merge_nothing_to_merge_token(tmp_path: Path):
    """零 commit no-op 路径：outcome=nothing_to_merge（非 merged）。"""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _git(main, "branch", "hw/A005/work")  # 与 main 同 tip → ahead=0

    gwt = GitWorktreeService()
    result = await gwt.merge_by_branch(
        str(main), "hw/A005/work", target_branch="main"
    )
    assert result.get("success") is True, result
    assert result.get("already_up_to_date") is True
    msg = str(result.get("message") or "")
    assert msg.startswith("outcome=nothing_to_merge:"), msg
    # noop 防重复文案保留（test_verify_approve_and_untracked_merge 同款钉子）
    assert "COMPLETE" in msg and "do not call git_worktree_merge again" in msg


# ── 2. git_worktree_status found 语义 ─────────────────────


@pytest.mark.asyncio
async def test_status_other_agent_missing_worktree_found_false():
    """查他人已拆 worktree → success + found=false + 无需重建说明。"""
    from hiveweave.tools import misc_tools

    gwt = MagicMock()
    gwt.ensure_git_repo = AsyncMock()
    gwt.info = AsyncMock(return_value={"success": True, "status": None})

    with (
        patch(
            "hiveweave.tools.misc_tools._get_worktree_context",
            AsyncMock(return_value=("/proj", "A001", "pid")),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            return_value=gwt,
        ),
    ):
        result = await misc_tools.git_worktree_status_tool(
            misc_tools.GitWorktreeStatusParams(shortId="A004"),
            "agent-1",
            "/proj",
            ctx=None,
        )
    assert result.success is True
    out = result.output or ""
    assert "found=false" in out
    # 短语保留（兼容既有引用），但语义已是正常答案而非错误
    assert "No worktree found" in out
    assert "无需重建" in out


# ── 3. update_task_status 同态幂等（工具层豁免） ───────────


PROJECT_ID = "test-receipt-token-project"
COORDINATOR_ID = "test-receipt-coord"
EXECUTOR_ID = "test-receipt-exec"


@pytest.fixture
async def task_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (COORDINATOR_ID, EXECUTOR_ID) else None

        agents = {
            COORDINATOR_ID: {
                "id": COORDINATOR_ID,
                "name": "Coord",
                "parent_id": None,
                "permission_type": "coordinator",
                "role": "架构师",
                "status": "active",
            },
            EXECUTOR_ID: {
                "id": EXECUTOR_ID,
                "name": "Exec",
                "parent_id": COORDINATOR_ID,
                "permission_type": "executor",
                "role": "engineer",
                "status": "active",
            },
        }

        async def fake_get_agent_by_id(aid: str):
            return agents.get(aid)

        from hiveweave.db import project as project_db
        from hiveweave.services import task as task_module

        task_module._migrated.clear()
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)

        with (
            patch(
                "hiveweave.db.meta.get_project_workspace",
                fake_get_project_workspace,
            ),
            patch(
                "hiveweave.db.meta.get_agent_project_id",
                fake_get_agent_project_id,
            ),
            patch(
                "hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id
            ),
        ):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
                "coordinator_id": COORDINATOR_ID,
                "executor_id": EXECUTOR_ID,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(COORDINATOR_ID, None)
        project_db._agent_cache.pop(EXECUTOR_ID, None)


async def _call_status_tool(params, project_id: str):
    from hiveweave.tools.tasks.lifecycle import update_task_status_tool

    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value=project_id),
    ):
        return await update_task_status_tool(params, EXECUTOR_ID, "/tmp/ws")


async def _blocked_task_with_dep(task_env, reason: str):
    """造一个 blocked 任务（depends_on=[b1]），返回 (task_id, b1, b2)。"""
    ts = TaskService()
    pid = task_env["project_id"]
    b1 = await ts.create_task(
        pid, "B1", "d", creator_id=COORDINATOR_ID, assignee_id=EXECUTOR_ID
    )
    b2 = await ts.create_task(
        pid, "B2", "d", creator_id=COORDINATOR_ID, assignee_id=EXECUTOR_ID
    )
    tid = await ts.create_task(
        pid, "Hold", "d", creator_id=COORDINATOR_ID, assignee_id=EXECUTOR_ID
    )
    await ts.claim_task(pid, tid, EXECUTOR_ID)
    await ts.start_task(pid, tid)
    await ts.block_task(
        pid, tid, reason, depends_on_task_ids=[b1]
    )
    return tid, b1, b2


@pytest.mark.asyncio
async def test_update_status_running_idempotent_noop(task_env):
    """running→running 确保态重申 → no-op 成功回执，不抛 Illegal transition。"""
    from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams

    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "T", "d", creator_id=COORDINATOR_ID, assignee_id=EXECUTOR_ID
    )
    await ts.claim_task(pid, tid, EXECUTOR_ID)
    await ts.start_task(pid, tid)

    result = await _call_status_tool(
        UpdateTaskStatusParams(task_id=tid, status="running"), pid
    )
    assert result.success is True, result.error
    assert "already running (no-op)" in (result.output or "")
    assert (await ts.get_task(pid, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_update_status_blocked_metadata_refresh(task_env):
    """blocked→blocked 带新元数据 → outcome=metadata_refreshed，就地更新字段。

    现场取证（17:23:04）：args 带了新 blockedReason + dependsOnTaskIds，实为
    元数据刷新需求——blocked 态此前无刷新通道，全被状态机判非法。
    """
    from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams

    ts = TaskService()
    pid = task_env["project_id"]
    tid, b1, b2 = await _blocked_task_with_dep(task_env, "第一版原因")

    result = await _call_status_tool(
        UpdateTaskStatusParams(
            task_id=tid,
            status="blocked",
            blocked_reason="刷新后的原因",
            depends_on_task_ids=[b2],
        ),
        pid,
    )
    assert result.success is True, result.error
    assert "outcome=metadata_refreshed" in (result.output or "")

    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"  # 无状态转移
    assert task["blocked_reason"] == "刷新后的原因"
    deps = task.get("depends_on") or []
    if isinstance(deps, str):
        import json as _json

        deps = _json.loads(deps)
    assert b1 in deps and b2 in deps  # 追加合并不丢旧依赖
    assert task["wait_kind"] == "dependency"

    # 刷新路径不写状态转移事件：task.blocked 事件只有初次 block 那一条
    from hiveweave.services.tasks.db import _query

    rows = await _query(
        pid,
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND event_type = 'task.blocked'",
        [tid],
    )
    assert rows and rows[0]["n"] == 1, rows


@pytest.mark.asyncio
async def test_update_status_blocked_noop_without_metadata(task_env):
    """blocked→blocked 不带元数据 → already blocked (no-op)，字段不动。"""
    from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams

    ts = TaskService()
    pid = task_env["project_id"]
    tid, _, _ = await _blocked_task_with_dep(task_env, "冻结原因")

    result = await _call_status_tool(
        UpdateTaskStatusParams(task_id=tid, status="blocked"), pid
    )
    assert result.success is True, result.error
    assert "already blocked (no-op)" in (result.output or "")
    task = await ts.get_task(pid, tid)
    assert task["status"] == "blocked"
    assert task["blocked_reason"] == "冻结原因"


@pytest.mark.asyncio
async def test_update_status_blocked_reason_only_refresh_keeps_wait_meta(
    task_env,
):
    """纯 reason 刷新：只动 blocked_reason，wait 元数据三件套不被推断值污染。"""
    from hiveweave.tools.tasks.lifecycle import UpdateTaskStatusParams

    ts = TaskService()
    pid = task_env["project_id"]
    tid, b1, _ = await _blocked_task_with_dep(task_env, "原始原因")

    result = await _call_status_tool(
        UpdateTaskStatusParams(
            task_id=tid, status="blocked", blocked_reason="仅刷新原因"
        ),
        pid,
    )
    assert result.success is True, result.error
    assert "outcome=metadata_refreshed" in (result.output or "")
    task = await ts.get_task(pid, tid)
    assert task["blocked_reason"] == "仅刷新原因"
    # wait 三件套原样保留（未传不得改写成推断默认值）
    assert task["wait_kind"] == "dependency"
    deps = task.get("depends_on") or []
    if isinstance(deps, str):
        import json as _json

        deps = _json.loads(deps)
    assert deps == [b1]


@pytest.mark.asyncio
async def test_update_blocked_metadata_rejects_non_blocked(task_env):
    """窄函数护栏：非 blocked 态调用 update_blocked_metadata → ValueError。"""
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Draft", "d", creator_id=COORDINATOR_ID, assignee_id=EXECUTOR_ID
    )
    with pytest.raises(ValueError, match="only applies to blocked"):
        await ts.update_blocked_metadata(pid, tid, reason="x")


# ── 4. review_task approve 拒绝文案按状态拆分 ──────────────


def _agent(agent_id: str, role: str, ptype: str) -> dict:
    return {
        "id": agent_id,
        "role": role,
        "permission_type": ptype,
        "status": "active",
    }


async def _review_approve_on_status(status: str):
    from hiveweave.tools.task_tools import ReviewTaskParams, review_task_tool

    task = {
        "id": "t-parent",
        "title": "T",
        "tags": [],
        "assignee_id": "coord1",
        "creator_id": "ceo1",
        "status": status,
        # tests_passed=true 跳过软证据门（本测试只钉状态闸文案）
        "evidence": {"tests_passed": True},
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        # 只测状态闸文案：把 attestation 证据门整个短路（needed=[]）
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            MagicMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.attestation.reviewer_required_kinds",
            MagicMock(return_value=[]),
        ),
        patch(
            "hiveweave.services.code_audit.drop_code_audit_kind_if_soft",
            AsyncMock(return_value=([], None)),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            return_value=_agent("ceo1", "ceo", "ceo")
        )
        Org.return_value.list_agents = AsyncMock(
            return_value=[
                _agent("ceo1", "ceo", "ceo"),
                _agent("coord1", "lead", "coordinator"),
            ]
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        return await review_task_tool(
            ReviewTaskParams(
                taskId="t-parent", decision="approve", comment="re-entry"
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )


@pytest.mark.asyncio
async def test_review_duplicate_approve_message_split():
    """重复 approve → 专属文案（勿重复 + 副作用只发生一次），非旧模板。"""
    result = await _review_approve_on_status("approved")
    assert result.success is False
    out = result.output or result.error or ""
    assert "already 'approved'" in out, out
    assert "exactly once" in out, out
    assert "must be 'submitted' or 'reviewing'" not in out, out


@pytest.mark.asyncio
async def test_review_approve_on_blocked_message_split():
    """blocked 态 approve → 专属文案（先解封再重交），非旧模板。"""
    result = await _review_approve_on_status("blocked")
    assert result.success is False
    out = result.output or result.error or ""
    assert "'blocked'" in out, out
    assert "cannot be approved" in out, out
    assert "must be 'submitted' or 'reviewing'" not in out, out
