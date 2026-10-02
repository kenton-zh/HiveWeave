"""T22-②：「[等待解除]」解除声明必须以事实为条件（S16 回执说谎回归）.

S16 事故锚点（2026-09）: merge 实际未发生时，平台对等待者发
「[等待解除] 任务已进入终态或有新进展…不必再等」—— 等待者被无事实的
解除声明放走（CEO/牧云 45 分钟双等死锁的同族成因）。

修复面（本文件钉死）:
  - close.py::_durable_wake_stmts —— 解除声明按
    ``wait_contract.TASK_WAIT_SATISFIED_STATUSES`` 条件化：
      * 终态（approved/closed/cancelled）→「[等待解除] …status=X」，事实在场；
      * 非终态转换 →「[任务状态变更] from→to（非终态——本次没有合并/关闭
        事实）」+ 重新挂 waiting_on 的处方；禁说「解除/不必再等」。
  - 调用方（_transition / _transition_multi / archive_task /
    _clear_task_wait_contracts 兜底）透传 from→to。
  - 唤醒机制本身不动（TEST17/P2-5：任意转移清等待 + durable 唤醒行 +
    恰好一次 trigger）。
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

PROJECT_ID = "test-t22-wait-facts"
COORD = "t22-coord"
EXEC = "t22-exec"


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


async def _add_task_wait(workspace: str, agent_id: str, task_id: str) -> str:
    conn = await project_db.ensure_project_db(workspace)
    wid = str(uuid.uuid4())
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_waits "
        "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
        "created_at, cleared_at) VALUES (?, ?, ?, 'task', ?, '[]', ?, ?, NULL)",
        [wid, agent_id, PROJECT_ID, task_id, now + 3_600_000, now],
    )
    await conn.commit()
    return wid


async def _wake_messages(workspace: str, agent_id: str) -> list[str]:
    conn = await project_db.ensure_project_db(workspace)
    cur = await conn.execute(
        "SELECT message FROM inbox WHERE to_agent_id = ? "
        "AND wake_category = 'task_wait_cleared' ORDER BY created_at ASC",
        [agent_id],
    )
    rows = await cur.fetchall()
    await cur.close()
    return [str(r["message"]) for r in rows]


async def _uncleared_wait_count(workspace: str, task_id: str) -> int:
    conn = await project_db.ensure_project_db(workspace)
    cur = await conn.execute(
        "SELECT COUNT(*) FROM agent_waits "
        "WHERE kind = 'task' AND ref = ? AND cleared_at IS NULL",
        [task_id],
    )
    n = (await cur.fetchone())[0]
    await cur.close()
    return int(n)


async def _running_task(ts: TaskService) -> str:
    tid = await ts.create_task(
        PROJECT_ID, "Work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(PROJECT_ID, tid, EXEC)
    await ts.start_task(PROJECT_ID, tid)
    return tid


@pytest.mark.asyncio
async def test_non_terminal_transition_says_state_change_not_release(env):
    """running→submitted（非终态）：唤醒行说「任务状态变更」，禁称
    「等待解除/不必再等」，必须给重新挂等待的处方。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)

    with patch("hiveweave.agents.trigger.trigger_subordinate",
               async_noop):
        await ts._transition(PROJECT_ID, tid, "submitted")

    msgs = await _wake_messages(env["workspace"], COORD)
    assert len(msgs) == 1, msgs
    msg = msgs[0]
    assert "[任务状态变更]" in msg, msg
    assert "running" in msg and "submitted" in msg, msg
    assert "非终态" in msg, msg
    # 谎报禁令：非终态转换不得出现解除声明
    assert "[等待解除]" not in msg, msg
    assert "不必再等" not in msg, msg
    # 处方：怎么继续等（T16 三期：auto ⇒ wait_on(refs=…)；legacy ⇒
    # waiting_on 列表——机制不变，只是话术分流）
    from hiveweave.config import turn_exit_mode

    if turn_exit_mode() == "auto":
        assert "wait_on(refs=" in msg, msg
    else:
        assert "waiting_on" in msg, msg
    # 等待行仍被清（TEST17/P2-5 机制不变），只是文案诚实了
    assert await _uncleared_wait_count(env["workspace"], tid) == 0


@pytest.mark.asyncio
async def test_terminal_transition_says_release_with_status(env):
    """→closed（终态）：唤醒行才是「[等待解除]」，且状态事实在场。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await ts._transition(PROJECT_ID, tid, "submitted")
    await ts._transition(PROJECT_ID, tid, "reviewing")
    await ts._transition(PROJECT_ID, tid, "approved")
    await _add_task_wait(env["workspace"], COORD, tid)

    with patch("hiveweave.agents.trigger.trigger_subordinate",
               async_noop):
        await ts._transition(PROJECT_ID, tid, "closed")

    msgs = await _wake_messages(env["workspace"], COORD)
    assert len(msgs) == 1, msgs
    msg = msgs[0]
    assert "[等待解除]" in msg, msg
    assert "status=closed" in msg, msg
    assert "不必再等" in msg  # 终态下「不必再等」是有事实支撑的


@pytest.mark.asyncio
async def test_archive_transition_says_release_cancelled(env):
    """archive（→cancelled 终态）：解除声明 + 状态事实。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)

    with patch("hiveweave.agents.trigger.trigger_subordinate",
               async_noop):
        await ts.archive_task(
            PROJECT_ID, tid, archived_by=COORD, reason="误绑，纠偏"
        )

    msgs = await _wake_messages(env["workspace"], COORD)
    assert len(msgs) == 1, msgs
    assert "[等待解除]" in msgs[0], msgs[0]
    assert "status=cancelled" in msgs[0], msgs[0]


@pytest.mark.asyncio
async def test_multi_transition_uses_final_status(env):
    """_transition_multi：文案按**最终**状态判定（reviewing→rework→running
    全程非终态 ⇒ 状态变更文案）。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await ts._transition(PROJECT_ID, tid, "submitted")
    await _add_task_wait(env["workspace"], COORD, tid)

    with patch("hiveweave.agents.trigger.trigger_subordinate",
               async_noop):
        await ts._transition_multi(
            PROJECT_ID, tid, "reviewing", "rework", "running"
        )

    msgs = await _wake_messages(env["workspace"], COORD)
    assert len(msgs) == 1, msgs
    msg = msgs[0]
    assert "[任务状态变更]" in msg, msg
    assert "running" in msg, msg
    assert "[等待解除]" not in msg, msg


async def async_noop(agent_id: str, **kwargs) -> None:
    return None
