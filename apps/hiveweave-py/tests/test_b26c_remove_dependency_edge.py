"""批26 修复3：depends_on 摘边入口（死依赖 / 循环依赖的平台账）。

病灶（d89 三条取消单）：依赖边建错（循环 / 死依赖）后平台不支持摘除，
只能整单 cancel 重铸。

验收（可机检）：
  ① 摘除死依赖 ⇒ 任务自动 unblock（blocked → running）+ 唤醒 assignee；
  ② 非 creator 非 CEO 摘除被拒（creator / CEO 可摘）；
  ③ 幂等：边不存在 ⇒ no-op 成功（removed=False，不改动）；
  ④ 循环依赖场景摘一边后本侧解锁（对侧仍 blocked，走正常 reconcile）。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-remove-dep-edge"
CREATOR = "creator-agent-1"
ASSIGNEE = "assignee-agent-2"
TASK_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001"
TASK_B = "bbbbbbbb-cccc-4ddd-8eee-eeeeeeee0002"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _insert_task(
    ws: str,
    task_id: str,
    *,
    status: str = "created",
    creator: str = CREATOR,
    assignee: str | None = ASSIGNEE,
):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", creator, assignee, status, now, now],
    )
    await conn.commit()


def _depends_on_ids(task: dict) -> list[str]:
    raw = task.get("depends_on") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            raw = []
    return list(raw) if isinstance(raw, list) else []


# ── ① 摘除死依赖 ⇒ 自动 unblock ─────────────────────────────────────


@pytest.mark.asyncio
async def test_removing_dead_dependency_unblocks(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, TASK_A, status="created")
    await _insert_task(ws, TASK_B, status="created")  # 死依赖：永远不推进
    ts = TaskService()

    blocked = await ts.apply_depends_on(pid, TASK_A, [TASK_B])
    assert blocked is True
    row = await ts.get_task(pid, TASK_A)
    assert (row or {}).get("status") == "blocked"

    info = await ts.remove_dependency_edge(pid, TASK_A, TASK_B)
    assert info["removed"] is True
    assert info["unblocked"] is True
    assert info["depends_on"] == []

    after = await ts.get_task(pid, TASK_A)
    assert (after or {}).get("status") == "running"
    assert _depends_on_ids(after) == []
    assert (after or {}).get("wait_kind") in (None, "")


# ── ③ 幂等：边不存在 ⇒ no-op 成功 ───────────────────────────────────


@pytest.mark.asyncio
async def test_removing_absent_edge_is_idempotent_noop(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, TASK_A, status="created")
    await _insert_task(ws, TASK_B, status="created")
    ts = TaskService()

    # 任务本无依赖
    info = await ts.remove_dependency_edge(pid, TASK_A, TASK_B)
    assert info["removed"] is False and info["idempotent"] is True

    # 挂上依赖后摘一条不存在的边：剩余边原样保留
    await ts.apply_depends_on(pid, TASK_A, [TASK_B])
    info2 = await ts.remove_dependency_edge(pid, TASK_A, "cccccccc-dddd-4eee-8fff-eeeeeeee0003")
    assert info2["removed"] is False and info2["idempotent"] is True
    row = await ts.get_task(pid, TASK_A)
    assert _depends_on_ids(row) == [TASK_B]
    assert (row or {}).get("status") == "blocked"


@pytest.mark.asyncio
async def test_missing_task_raises(env):
    ts = TaskService()
    with pytest.raises(Exception):
        await ts.remove_dependency_edge(
            PROJECT_ID, "eeeeeeee-ffff-4aaa-8bbb-cccccccc0004", TASK_B
        )


# ── ④ 循环依赖：摘一边后本侧解锁，对侧不受影响 ───────────────────────


@pytest.mark.asyncio
async def test_cycle_removing_one_side_unblocks_that_side(env):
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, TASK_A, status="created")
    await _insert_task(ws, TASK_B, status="created")
    ts = TaskService()

    await ts.apply_depends_on(pid, TASK_A, [TASK_B])
    await ts.apply_depends_on(pid, TASK_B, [TASK_A])
    assert (await ts.get_task(pid, TASK_A)).get("status") == "blocked"
    assert (await ts.get_task(pid, TASK_B)).get("status") == "blocked"

    info = await ts.remove_dependency_edge(pid, TASK_A, TASK_B)
    assert info["removed"] is True and info["unblocked"] is True
    assert (await ts.get_task(pid, TASK_A)).get("status") == "running"
    # 对侧仍在等 A（合法依赖，reconcile 在管）—— 摘边不得越权放行
    row_b = await ts.get_task(pid, TASK_B)
    assert (row_b or {}).get("status") == "blocked"
    assert _depends_on_ids(row_b) == [TASK_A]


@pytest.mark.asyncio
async def test_timer_wait_not_unblocked_by_edge_removal(env):
    """wait_kind=timer 的 blocked 摘边**不**提前放行（timer 另有关口）。"""
    ws = env["workspace_path"]
    pid = env["project_id"]
    await _insert_task(ws, TASK_A, status="created")
    await _insert_task(ws, TASK_B, status="created")
    ts = TaskService()

    await ts.block_task(
        pid, TASK_A, "cooldown", depends_on_task_ids=[TASK_B],
        wait_kind="timer", wake_at=int(time.time() * 1000) + 3_600_000,
    )
    info = await ts.remove_dependency_edge(pid, TASK_A, TASK_B)
    assert info["removed"] is True
    assert info["unblocked"] is False
    assert (await ts.get_task(pid, TASK_A)).get("status") == "blocked"


# ── ② 工具层权限门 + 唤醒接线 ───────────────────────────────────────


class _FakeTaskService:
    def __init__(self, task_row, removal=None):
        self._task = task_row
        self._removal = removal or {
            "removed": True, "idempotent": False, "unblocked": True,
            "depends_on": [], "status": "running",
        }
        self.removed_with = None

    async def get_task(self, project_id, task_id):
        return self._task

    async def remove_dependency_edge(self, project_id, task_id, dep_ref, *, removed_by=None):
        self.removed_with = (project_id, task_id, dep_ref, removed_by)
        return self._removal


def _task_row(creator: str = CREATOR, assignee: str = ASSIGNEE) -> dict:
    return {
        "id": TASK_A,
        "title": "实现 M-A",
        "creator_id": creator,
        "assignee_id": assignee,
        "status": "blocked",
    }


async def _call_tool(monkeypatch, fake_ts, agent_id: str, *, me_row: dict):
    async def fake_project_id(a):
        return PROJECT_ID

    async def fake_resolve(self, agent):
        return me_row

    async def fake_send(self, from_agent_id, to_agent_id, message, **kw):
        return "inbox-id"

    async def fake_trigger(agent):
        return None

    monkeypatch.setattr("hiveweave.tools.helpers.get_project_id", fake_project_id)
    monkeypatch.setattr("hiveweave.services.task.TaskService", lambda: fake_ts)
    monkeypatch.setattr(
        "hiveweave.services.org.OrgService.resolve_agent", fake_resolve
    )
    monkeypatch.setattr(
        "hiveweave.services.inbox.InboxService.send_message", fake_send
    )
    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", fake_trigger
    )
    from hiveweave.tools.tasks.admin import (
        RemoveTaskDependencyParams,
        remove_task_dependency_tool,
    )

    return await remove_task_dependency_tool(
        RemoveTaskDependencyParams(taskId=TASK_A, depTaskId=TASK_B),
        agent_id,
        "",
    )


@pytest.mark.asyncio
async def test_tool_rejects_non_creator_non_ceo(monkeypatch):
    fake_ts = _FakeTaskService(_task_row())
    result = await _call_tool(
        monkeypatch, fake_ts, "some-random-agent",
        me_row={"role": "executor", "permission_type": "executor"},
    )
    assert not result.success
    assert "creator 或 CEO" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_allows_creator_and_wakes_assignee(monkeypatch):
    fake_ts = _FakeTaskService(_task_row())
    result = await _call_tool(
        monkeypatch, fake_ts, CREATOR, me_row={"role": "coordinator"}
    )
    assert result.success
    assert fake_ts.removed_with is not None
    assert fake_ts.removed_with[3] == CREATOR
    assert "auto-unblocked" in result.output
    assert "Assignee woken" in result.output


@pytest.mark.asyncio
async def test_tool_allows_ceo(monkeypatch):
    fake_ts = _FakeTaskService(_task_row(creator="someone-else"))
    result = await _call_tool(
        monkeypatch, fake_ts, "ceo-agent-9", me_row={"role": "ceo"}
    )
    assert result.success


@pytest.mark.asyncio
async def test_tool_idempotent_receipt(monkeypatch):
    fake_ts = _FakeTaskService(_task_row(), removal={
        "removed": False, "idempotent": True, "unblocked": False,
        "depends_on": [], "status": "created",
    })
    result = await _call_tool(
        monkeypatch, fake_ts, CREATOR, me_row={"role": "coordinator"}
    )
    assert result.success
    assert "idempotent no-op" in result.output
