"""新-③：verify_spawn 的 rehang 必须让「状态写」与「事件写」**同一次提交**。

病灶（§12.9）：旧实现先 `_execute(UPDATE tasks SET status='created' …)`，再
`insert_task_event('task.verify_rehang')` **独立提交**（后者自带 try/except 吞错）
⇒ 两次提交之间的崩溃窗口只落一边。

**判别性判据（回滚方向）**：让事务里**第二条语句**失败 ⇒ 状态必须仍是 `blocked`
（没被单独提交）且**没有** `task.verify_rehang` 事件。改前状态已提交 ⇒ 必红。

本轮一并把该块从 `retry_qa_blocked_verify_tasks` 的循环里**抽成独立函数**
（`_rehang_blocked_verify_task`）—— 原形态要驱动它必须先造出候选 QA（roster），
验证成本全在 setup；抽出后可直接验收。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-new3-rehang-tx"


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


async def _blocked_task(env) -> str:
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"], "VERIFY 原子性", "desc",
        creator_id="coord", assignee_id="qa-1",
    )
    conn = await project_db.ensure_project_db(env["workspace"])
    await conn.execute(
        "UPDATE tasks SET status = 'blocked', blocked_reason = 'merge_failed', "
        "wait_kind = 'task', wake_at = 1 WHERE id = ?",
        [tid],
    )
    await conn.commit()
    return tid


async def _observe(env, tid: str) -> tuple[str, int]:
    """同一观察点：任务的 status + rehang 事件条数。"""
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute("SELECT status FROM tasks WHERE id = ?", [tid])
    status = str((await cur.fetchone())[0])
    await cur.close()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND event_type = 'task.verify_rehang'",
        [tid],
    )
    events = int((await cur.fetchone())[0])
    await cur.close()
    return status, events


@pytest.mark.asyncio
async def test_rehang_commits_status_and_event_together(env):
    from hiveweave.tools.tasks import verify_spawn as vs

    tid = await _blocked_task(env)
    await vs._rehang_blocked_verify_task(
        env["project_id"], tid, int(time.time() * 1000)
    )
    status, events = await _observe(env, tid)
    assert status == "created"
    assert events == 1, "rehang 事件必须落（与状态同一次提交）"


@pytest.mark.asyncio
async def test_event_failure_rolls_status_back(env):
    """⭐ 判别性判据：第二条语句失败 ⇒ 状态**没被单独提交**。"""
    from hiveweave.tools.tasks import verify_spawn as vs

    tid = await _blocked_task(env)

    def _boom(*_a, **_k):
        return (("INSERT INTO no_such_table_xyz (id) VALUES (?)", ["x"]), 0, "ev")

    with patch(
        "hiveweave.services.tasks.db.build_task_event_insert", _boom
    ):
        await vs._rehang_blocked_verify_task(
            env["project_id"], tid, int(time.time() * 1000)
        )

    status, events = await _observe(env, tid)
    assert status == "blocked", (
        f"状态被单独提交了（{status}）—— 说明状态写与事件写不在同一次提交里"
    )
    assert events == 0
