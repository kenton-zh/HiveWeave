"""新-①（P2-5b）：唤醒本身必须 **durable**，且**不能被终态降级误杀**。

病灶一：`_wake_task_waiters` 只在提交后调 `trigger_subordinate()`（**进程内调用，
不是写库**）⇒ 崩在 COMMIT 与它之间 = 等待已清（durable）而**唤醒永不发生**
（比 TTL 更糟：等待行已清，没有任何东西会再叫醒它）。

病灶二（本轮实测踩到）：`archive_task` 末尾的 `demote_wake_for_task`
（`UPDATE inbox SET wake=0 WHERE task_id=? AND read=0 AND COALESCE(wake,1)=1`）
会把刚落库的**唤醒行**一起降级 —— 它的语义是「义务消失、别再唤醒」，与
「等待已解除、请转向别处」**相反** ⇒ 必须按 `wake_category` 排除。

判据（同一观察点，两格 + 崩溃场景）：
- ① 我的 durable 唤醒行：`wake=1`（活下来）；
- ② 同 task 的**旧式提示行**（别的 category）：`wake=0`（降级仍生效，没被我一并放过）；
- ③ 让 `trigger_subordinate` 抛错（模拟崩溃点）⇒ ①② 仍成立（不依赖进程内 trigger）。
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

PROJECT_ID = "test-new1-durable-wake"
WAITER = "waiter-new1"
LEGACY = "legacy-prompt"


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


async def _setup(env) -> str:
    """一条等待行（→ waiter）+ 一条**旧式**提示行（→ legacy）挂在同一 task 上。"""
    ts = TaskService()
    tid = await ts.create_task(
        env["project_id"], "待归档任务", "d", creator_id="coord", assignee_id="exec-1"
    )
    conn = await project_db.ensure_project_db(env["workspace"])
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_waits "
        "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
        "created_at, cleared_at) VALUES (?, ?, ?, 'task', ?, '[]', ?, ?, NULL)",
        [str(uuid.uuid4()), WAITER, PROJECT_ID, tid, now + 3_600_000, now],
    )
    # 旧式提示行：wake=1 + read=0，category 不是 task_wait_cleared ⇒ 应被降级
    await conn.execute(
        "INSERT INTO inbox (id, from_agent_id, to_agent_id, message, read, "
        "created_at, message_type, expect_report, priority, task_id, wake, "
        "delivered, wake_category) "
        "VALUES (?, 'system', ?, '[REWORK REQUESTED] 旧提示', 0, ?, 'normal', "
        "0, 'normal', ?, 1, 0, 'review')",
        [str(uuid.uuid4()), LEGACY, now, tid],
    )
    await conn.commit()
    return tid


async def _wake_flags(env, tid: str) -> dict[str, int]:
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT to_agent_id, wake FROM inbox WHERE task_id = ?", [tid]
    )
    rows = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
    await cur.close()
    return rows


@pytest.mark.asyncio
async def test_durable_wake_survives_demote_even_if_trigger_crashes(env):
    tid = await _setup(env)
    ts = TaskService()

    async def _boom(*_a, **_k):
        raise RuntimeError("模拟：COMMIT 之后、trigger 之前进程崩掉")

    with patch("hiveweave.agents.trigger.trigger_subordinate", _boom):
        await ts.archive_task(
            env["project_id"], tid, archived_by="coord", reason="test"
        )

    flags = await _wake_flags(env, tid)
    assert flags.get(WAITER) == 1, (
        f"durable 唤醒行被降级/丢失（wake={flags.get(WAITER)}）—— trigger 没跑就"
        f"等于唤醒永久丢失；全部行：{flags}"
    )
    assert flags.get(LEGACY) == 0, (
        f"旧式提示行没被降级 → 降级逻辑被我一并放过（{flags}）"
    )
