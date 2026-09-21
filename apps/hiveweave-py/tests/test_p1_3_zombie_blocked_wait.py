"""P1-3 ①：**僵尸 blocked 等待**要接到 wait 侧（消「只能等 TTL」）。

病灶（§10.1 真病灶 = 缺链接）：僵尸检测**早已存在且是纯状态判据** ——
`lifecycle.blocked_task_has_wake_path()`（`depends_on` 非空 或 `timer`+`wake_at`）；
但它**只被义务面消费**（`services/obligation.py`），wait 侧不读
⇒ 「blocked 且无自动解封路径」的任务上的 `kind='task'` 等待永远等不到
`task_transition`，只能等 TTL。

⚠ 规格明令：**不得**把 `blocked` 塞进 `_TASK_WAIT_SATISFIED_STATUSES` ——
`_TRANSITIONS` 允许 blocked → running/closed 且 `reconcile_blocked_tasks` 会解封续走
⇒ 塞进去会让等待/唤醒**空转**。

判据（两格，状态判据）：① blocked + `depends_on` 空 + `wait_kind` 空 ⇒ 等待当场清除
（`cleared_at` 非 NULL）+ 唤醒；② **对照**：blocked + `wait_kind='timer'` + `wake_at` 非空
（有解封路径）⇒ 等待**不得**被清。
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
from hiveweave.services.wait_contract import WaitContractService

PROJECT_ID = "p1-3-zombie-wait"
COORD = "coord-p13"
EXEC = "exec-p13"


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


async def _blocked_task(env, *, zombie: bool) -> str:
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "被阻塞的任务", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.start_task(pid, tid)
    conn = await project_db.ensure_project_db(env["workspace"])
    now = int(time.time() * 1000)
    if zombie:
        # 无自动解封路径：deps 空 + 非 timer
        await conn.execute(
            "UPDATE tasks SET status = 'blocked', depends_on = NULL, "
            "wait_kind = NULL, wake_at = NULL WHERE id = ?",
            [tid],
        )
    else:
        # 有自动解封路径：timer + wake_at（不得被清）
        await conn.execute(
            "UPDATE tasks SET status = 'blocked', depends_on = NULL, "
            "wait_kind = 'timer', wake_at = ? WHERE id = ?",
            [now + 3_600_000, tid],
        )
    await conn.commit()
    return tid


async def _wait_cleared(env, ref: str) -> bool:
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT cleared_at FROM agent_waits WHERE ref = ? AND kind = 'task'", [ref]
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, "等待行不存在"
    return row[0] is not None


@pytest.mark.asyncio
async def test_zombie_blocked_wait_is_short_circuited(env, monkeypatch):
    tid = await _blocked_task(env, zombie=True)
    triggered: list[str] = []

    async def fake_trigger(agent_id: str) -> None:
        triggered.append(agent_id)

    monkeypatch.setattr("hiveweave.agents.trigger.trigger_subordinate", fake_trigger)

    svc = WaitContractService()
    await svc.replace_waits(
        PROJECT_ID, EXEC, [{"kind": "task", "ref": tid}], phase="waiting"
    )
    assert await _wait_cleared(env, tid) is True, (
        "无自动解封路径的 blocked 任务上的等待没被清 —— 只能等 TTL（僵尸等待）"
    )
    assert triggered, "清等待后必须唤醒（否则 agent 干等）"


@pytest.mark.asyncio
async def test_blocked_with_wake_path_is_not_cleared(env, monkeypatch):
    """对照：blocked 但有解封路径（timer）⇒ 等待**不得**被清。"""
    tid = await _blocked_task(env, zombie=False)

    async def fake_trigger(_agent_id: str) -> None:
        return None

    monkeypatch.setattr("hiveweave.agents.trigger.trigger_subordinate", fake_trigger)

    svc = WaitContractService()
    await svc.replace_waits(
        PROJECT_ID, EXEC, [{"kind": "task", "ref": tid}], phase="waiting"
    )
    assert await _wait_cleared(env, tid) is False, (
        "有解封路径的 blocked 等待被误清 —— 会与 reconcile 解封打架（空转）"
    )
