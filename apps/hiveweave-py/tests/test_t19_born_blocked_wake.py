"""T19（S16 铁蛋/Iris）：出生即 blocked 的任务 assignee 轻量待命 wake.

事故锚点（S16 实测 2026-09）:
  任务派给 executor 后下一秒转 blocked（Waiting on dependency），派单消息
  wake=0/delivered=0；``claimed_at=NULL`` 不入义务表（obligations 闭式负
  空间），沉默看门狗按合法 idle 豁免 ⇒「雇而未用 2h45m」零信号。

修复面（本文件钉死）:
  - src/hiveweave/agents/trigger.py
      (maybe_notify_born_blocked：形态自校验 + 幂等键 born-blocked:{task_id}
       一次性 platform_notice)
  - src/hiveweave/services/dispatch.py
      (dispatch_task is_blocked 分支接线 —— 派单消息 wake=0 的同时补知情
       notice；非 blocked 派单绝不发)

变异验证约定（删掉修复代码对应测试必须失败）:
  - 删 dispatch 接线 → wiring 用例失败
  - 幂等键去掉/换语义 → idempotent 用例失败
  - 形态自校验删掉任一守卫 → 对应 non-born-blocked 用例失败
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import json

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services import task as task_mod
from hiveweave.services.dispatch import DispatchService
from hiveweave.services.handoff import HandoffService
from hiveweave.services.inbox import InboxService
from hiveweave.services.obligation import ObligationLedger
from hiveweave.services.task import TaskService

PROJECT_ID = "test-t19-project"
MID_ID = "t19-mid"
EXEC_A = "t19-exec-a"

_NOW = int(time.time() * 1000)


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_mod._migrated.clear()
        for aid in (MID_ID, EXEC_A):
            project_db._agent_cache[aid] = workspace_path

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace):
            yield {"project_id": PROJECT_ID,
                   "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            for aid in (MID_ID, EXEC_A):
                project_db._agent_cache.pop(aid, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _insert_task(
    env, task_id: str, *, status, title="t19 task", assignee_id=None,
    creator_id=MID_ID, claimed_at=_NOW, wait_kind=None, depends_on=None,
    archived=0,
):
    task_mod._migrated.clear()
    await task_mod._ensure_schema(PROJECT_ID)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, status, progress, "
        "creator_id, assignee_id, created_at, updated_at, is_archived, "
        "claimed_at, wait_kind, depends_on) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, title, status, creator_id, assignee_id,
         _NOW, _NOW, archived, claimed_at, wait_kind,
         json.dumps(depends_on) if depends_on else None],
    )
    await conn.commit()


async def _inbox_rows(env, agent_id):
    conn = await ensure_project_db(env["workspace_path"])
    cur = await conn.execute(
        "SELECT id, message, message_type, priority, wake, read, "
        "idempotency_key, task_id FROM inbox WHERE to_agent_id = ?",
        [agent_id])
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ── maybe_notify_born_blocked 单元（真库） ───────────────────


async def test_born_blocked_notice_once_and_idempotent(env):
    """出生 blocked（dependency + assignee + claimed_at=NULL）⇒ 恰好一条
    待命 wake：wake=1、幂等键 born-blocked:{task_id}、正文带依赖名/状态；
    重复调用零追加（一次性）。"""
    dep_id = str(uuid.uuid4())
    await _insert_task(env, dep_id, status="created", title="上游骨架任务")
    task_id = str(uuid.uuid4())
    await _insert_task(
        env, task_id, status="blocked", title="下游收尾任务",
        assignee_id=EXEC_A, creator_id=MID_ID, claimed_at=None,
        wait_kind="dependency", depends_on=[dep_id],
    )
    from hiveweave.agents.trigger import maybe_notify_born_blocked

    assert await maybe_notify_born_blocked(PROJECT_ID, task_id) is True
    rows = await _inbox_rows(env, EXEC_A)
    assert len(rows) == 1
    r = rows[0]
    assert r["wake"] == 1
    assert r["idempotency_key"] == f"born-blocked:{task_id}"
    assert r["task_id"] == task_id
    assert "下游收尾任务" in r["message"]
    assert "已登记" in r["message"]
    assert "无需轮询" in r["message"]
    assert "上游骨架任务" in r["message"]
    assert "(created)" in r["message"]

    # 一次性：同任务重复触发不追加
    assert await maybe_notify_born_blocked(PROJECT_ID, task_id) is True
    assert len(await _inbox_rows(env, EXEC_A)) == 1


async def test_born_blocked_title_with_braces_still_sent(env):
    """标题含 ``{}``（agent 自由文本）不得炸掉知情 wake —— 文案填充不用
    str.format（OCR 评审发现：KeyError 会让 notice 静默消失）。"""
    dep_id = str(uuid.uuid4())
    await _insert_task(env, dep_id, status="created", title="上游")
    task_id = str(uuid.uuid4())
    await _insert_task(env, task_id, status="blocked", title="修 dict{} 解析",
                       assignee_id=EXEC_A, claimed_at=None,
                       wait_kind="dependency", depends_on=[dep_id])
    from hiveweave.agents.trigger import maybe_notify_born_blocked

    assert await maybe_notify_born_blocked(PROJECT_ID, task_id) is True
    rows = await _inbox_rows(env, EXEC_A)
    assert len(rows) == 1
    assert "dict{} 解析" in rows[0]["message"]


async def test_non_born_blocked_shapes_stay_silent(env):
    """形态自校验五守卫：任一不满足 ⇒ 不发（已 claim 的 blocked 走正常
    义务/看门狗路径，timer 等待/无主/归档/非 blocked 同理）。"""
    from hiveweave.agents.trigger import maybe_notify_born_blocked

    # (a) timer 等待不是 dependency 知情范围
    tid = str(uuid.uuid4())
    await _insert_task(env, tid, status="blocked", assignee_id=EXEC_A,
                       claimed_at=None, wait_kind="timer")
    # (b) born-blocked 但无 assignee
    tid2 = str(uuid.uuid4())
    await _insert_task(env, tid2, status="blocked", assignee_id=None,
                       claimed_at=None, wait_kind="dependency")
    # (c) 已 claim 的 blocked（apply_depends_on 停泊前已认领）
    tid3 = str(uuid.uuid4())
    await _insert_task(env, tid3, status="blocked", assignee_id=EXEC_A,
                       claimed_at=_NOW, wait_kind="dependency")
    # (d) 非 blocked 状态
    tid4 = str(uuid.uuid4())
    await _insert_task(env, tid4, status="running", assignee_id=EXEC_A)
    # (e) 已归档
    tid5 = str(uuid.uuid4())
    await _insert_task(env, tid5, status="blocked", assignee_id=EXEC_A,
                       claimed_at=None, wait_kind="dependency", archived=1)

    for t in (tid, tid2, tid3, tid4, tid5):
        assert await maybe_notify_born_blocked(PROJECT_ID, t) is False, t
    # 不存在的任务同样静默
    assert await maybe_notify_born_blocked(
        PROJECT_ID, str(uuid.uuid4())) is False
    assert await _inbox_rows(env, EXEC_A) == []


# ── dispatch 接线 ────────────────────────────────────────────

_WRITER = {
    "id": EXEC_A,
    "permission_type": "executor",
    "parent_id": MID_ID,
    "name": "铁蛋",
    "short_id": "A009",
}


def _dispatch_patches(get_task_row) -> list:
    return [
        patch("hiveweave.services.dispatch._conn", AsyncMock()),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.db.meta.get_project_workspace",
            AsyncMock(return_value="/tmp/ws"),
        ),
        patch(
            "hiveweave.services.git_worktree.agent_gets_write_worktree",
            lambda a: (a or {}).get("permission_type") == "executor",
        ),
        patch(
            "hiveweave.services.git_worktree.ensure_executor_worktree",
            AsyncMock(return_value={
                "success": True, "path": "/tmp/wt", "short_id": "A009",
            }),
        ),
        patch(
            "hiveweave.services.git_worktree.pin_dispatch_message_to_worktree",
            lambda desc, short_id="", worktree_path="": desc,
        ),
        patch(
            "hiveweave.services.git_worktree.worktree_commits_behind_main",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.org.OrgService.resolve_agent",
            AsyncMock(return_value=_WRITER),
        ),
        patch.object(
            TaskService, "create_task", AsyncMock(return_value="task-1")
        ),
        patch.object(
            TaskService, "get_task",
            AsyncMock(side_effect=lambda *a, **k: get_task_row),
        ),
        patch.object(TaskService, "update_task", AsyncMock()),
        patch.object(TaskService, "require_task_id",
                     AsyncMock(return_value="task-1")),
        patch.object(TaskService, "ensure_assignee_claimed", AsyncMock()),
        patch.object(InboxService, "send_message", AsyncMock()),
        patch.object(
            HandoffService, "create_handoff", AsyncMock(return_value="h-1")
        ),
        patch.object(ObligationLedger, "create", AsyncMock()),
    ]


def _enter(patches: list) -> ExitStack:
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


def test_dispatch_wiring_blocked_sends_standby_wake():
    """出生 blocked 派单：派单消息 wake=0 的同时必须补 born-blocked 知情
    wake（变异：删 dispatch 接线 → 本用例失败）。"""
    blocked_row = {
        "id": "task-1", "title": "t", "kind": None, "status": "blocked",
        "is_archived": 0,
    }
    notify = AsyncMock(return_value=True)
    with _enter(_dispatch_patches(blocked_row)):
        with patch(
            "hiveweave.agents.trigger.maybe_notify_born_blocked", notify,
        ):
            svc = DispatchService()
            result = asyncio.run(
                svc.dispatch_task(PROJECT_ID, MID_ID, EXEC_A, "实现登录模块")
            )
        send = InboxService.send_message  # with 块内取 mock（出块即还原）
    assert result["success"] is True
    assert result.get("blocked") is True
    notify.assert_awaited_once_with(PROJECT_ID, "task-1")
    # 派单消息本身仍是 wake=0（不做持续催办）
    assert send.await_count >= 1
    assert send.await_args.kwargs.get("wake") is False


def test_dispatch_wiring_unblocked_never_notifies():
    """正常派单（非 blocked）绝不触发 born-blocked 知情 wake。"""
    claimed_row = {
        "id": "task-1", "title": "t", "kind": None, "status": "claimed",
        "is_archived": 0,
    }
    notify = AsyncMock(return_value=False)
    with _enter(_dispatch_patches(claimed_row)):
        with patch(
            "hiveweave.agents.trigger.maybe_notify_born_blocked", notify,
        ):
            svc = DispatchService()
            result = asyncio.run(
                svc.dispatch_task(PROJECT_ID, MID_ID, EXEC_A, "实现登录模块")
            )
    assert result["success"] is True
    assert result.get("blocked") is False
    notify.assert_not_awaited()
