"""新-②：dismiss 无父任务时的归档必须**同一次提交**清等待（+ 提交后唤醒）。

病灶（§12.9）：`org.py` 的归档路径只写 `is_archived=1, status='cancelled'` + 批量
`task.archived` 事件（同事务），**不清 `agent_waits`** ⇒ 终态之后那条等待永远不会被
满足，等待方只能干等到 TTL。

判据（P2-5 同款状态判据）：同一观察点读库 —— 任务已归档为 cancelled **且**
该 ref 的 `cleared_at IS NULL` 计数 = 0（等待被清），`task.archived` 事件 = 1。
反向：归档**失败/回滚**时不得清等待（不得凭幻影事件叫醒）。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-new2-dismiss-archive"
AGENT = "agent-new2"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _open_task(env) -> str:
    ts = TaskService()
    return await ts.create_task(
        env["project_id"], "待办", "desc", creator_id="coord", assignee_id=AGENT
    )


async def _add_wait(env, task_id: str) -> str:
    conn = await project_db.ensure_project_db(env["workspace"])
    wid = str(uuid.uuid4())
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_waits "
        "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
        "created_at, cleared_at) VALUES (?, ?, ?, 'task', ?, '[]', ?, ?, NULL)",
        [wid, AGENT, PROJECT_ID, task_id, now + 3_600_000, now],
    )
    await conn.commit()
    return wid


async def _observe(env, task_id: str) -> tuple[int, str, int, int]:
    """(is_archived, status, 未清等待数, task.archived 事件数) —— 同一观察点。"""
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT is_archived, status FROM tasks WHERE id = ?", [task_id]
    )
    row = await cur.fetchone()
    await cur.close()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM agent_waits WHERE ref = ? AND cleared_at IS NULL",
        [task_id],
    )
    uncleared = int((await cur.fetchone())[0])
    await cur.close()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND event_type = 'task.archived'",
        [task_id],
    )
    events = int((await cur.fetchone())[0])
    await cur.close()
    return int(row[0]), str(row[1]), uncleared, events


@pytest.mark.asyncio
async def test_archive_clears_wait_in_same_commit(env):
    from hiveweave.services import org as org_module

    tid = await _open_task(env)
    await _add_wait(env, tid)
    open_tasks = [{"id": tid, "status": "running"}]

    events = await org_module._archive_open_tasks_on_dismiss(
        env["project_id"], AGENT, open_tasks, int(time.time() * 1000)
    )

    archived, status, uncleared, ev_count = await _observe(env, tid)
    assert archived == 1 and status == "cancelled"
    assert ev_count == 1
    assert uncleared == 0, (
        "等待没被清 —— 终态之后等待方只能等 TTL（新-② 的病灶）"
    )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_rollback_does_not_clear_wait(env):
    """归档事务整体失败 ⇒ 不得清等待、不得返回事件（不凭幻影事件叫醒）。"""
    from hiveweave.services import org as org_module

    tid = await _open_task(env)
    await _add_wait(env, tid)

    with patch(
        "hiveweave.services.tasks.db.build_task_event_insert",
        lambda *_a, **_k: (("INSERT INTO no_such_table_xyz (id) VALUES (?)", ["x"]), 0, "e"),
    ):
        events = await org_module._archive_open_tasks_on_dismiss(
            env["project_id"], AGENT, [{"id": tid, "status": "running"}],
            int(time.time() * 1000),
        )

    archived, status, uncleared, _ = await _observe(env, tid)
    assert events == []
    assert archived == 0, "事务失败却归档了 ⇒ 原子性被破坏"
    assert uncleared == 1, "事务失败却清了等待 ⇒ 唤醒方会凭幻影事件叫醒"
