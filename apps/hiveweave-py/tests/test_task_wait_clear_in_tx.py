"""P2-5: 清等待必须与「触发方那次提交」同事务（消 lost-update 窗口）。

病灶：`UPDATE agent_waits SET cleared_at`（`close.py:_clear_task_wait_contracts`）
原本发生在 task 状态写 + `task_events` outbox 提交**之后**、独立一次写
⇒ 崩溃窗口可只落一边（等待已清、唤醒事件没落 ⇒ 唤醒永久丢失）。

本文件用**状态判据**验收，不用文案判据：

- 判据 ①②（同一观察点）：`agent_waits` 该 ref 的 `cleared_at IS NULL` 计数 = 0
  **且** `task_events` 该 task 的 `delivered = 0` 计数 = 1。
  两者必须在**同一次读取**里同时成立（证明同批可见）。
- ⭐ 判别性判据（阳性对照）：在「事务已提交、唤醒还没发」的崩溃窗口插探针
  （`publish_task_event` 处先读库再抛错）⇒ 崩溃后 ①② 已同时为真。
  改前此处读到的是「等待未清」⇒ 本判据转红。
- 判据 ③（回滚方向）：事务里任一语句失败 ⇒ 等待与 outbox **都没落**。

覆盖 4 条触发路径（少覆盖一条 = 「机制层备好、触发路径没接」复发）：
`_transition` else 支 / `_transition` blocked-exit 支 / `_transition_multi` /
`archive_task`。
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

PROJECT_ID = "test-p2-5-wait-in-tx"
COORD = "coord-p25"
EXEC = "exec-p25"


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


# ── 观测原语（状态判据：直接读库，不看日志文案）──────────────────


async def _counts(workspace: str, task_id: str) -> tuple[int, int]:
    """(未清的 kind='task' 等待数, 未投递的 task_events 数) —— 同一观察点。

    ⚠ 未投递事件数是**累计**值：`create/claim/start` 各自也写 outbox 行，
    测试环境没有 relay 消费 ⇒ 必须按「转换前后增量 = 1」判，不能写死 1
    （写死会退化成「只数到了最后那条」的假判据）。
    """
    conn = await project_db.ensure_project_db(workspace)
    cur = await conn.execute(
        "SELECT COUNT(*) FROM agent_waits "
        "WHERE kind = 'task' AND ref = ? AND cleared_at IS NULL",
        [task_id],
    )
    waits = (await cur.fetchone())[0]
    await cur.close()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND delivered = 0",
        [task_id],
    )
    events = (await cur.fetchone())[0]
    await cur.close()
    return int(waits), int(events)


async def _add_task_wait(workspace: str, agent_id: str, task_id: str) -> str:
    """插入一条活跃的 kind='task' 等待行（批量形态下的多行场景也走这里）。"""
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


def _probe(workspace: str, seen: list, *, raise_after: bool = True):
    """崩溃窗口探针：在「事务已提交、唤醒未发」处读库，然后抛错模拟崩溃。"""

    async def _probe(project_id, task_id, event_type, to_status, ts):
        seen.append(await _counts(workspace, task_id))
        if raise_after:
            raise RuntimeError("crash-window probe")

    return _probe


async def _running_task(ts: TaskService) -> str:
    tid = await ts.create_task(
        PROJECT_ID, "Work", "desc", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(PROJECT_ID, tid, EXEC)
    await ts.start_task(PROJECT_ID, tid)
    return tid


# ── 主路径：事务外的唤醒会观察到「等待已清 + outbox 已落」──────────


async def _outbox_base(workspace: str, task_id: str) -> int:
    """转换前该 task 的未投递 outbox 计数（判据 ② 取增量用）。"""
    return (await _counts(workspace, task_id))[1]


@pytest.mark.asyncio
async def test_transition_else_branch_clears_wait_in_tx(env):
    """`_transition`（非 blocked 支）：清等待与 outbox 同批提交，唤醒在其后。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    # 恰好一次：事务内已清 ⇒ 事务外兜底读不到行 ⇒ 不再二次唤醒
    calls: list[str] = []
    observed: list[tuple[int, int]] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)
        observed.append(await _counts(env["workspace"], tid))

    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        await ts._transition(PROJECT_ID, tid, "submitted")

    assert calls == [COORD]
    # 唤醒时（= 提交后）：等待已清且本次 outbox 行已在（同一次读取里同时成立）
    assert observed == [(0, base + 1)]
    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "submitted"
    assert await _counts(env["workspace"], tid) == (0, base + 1)


@pytest.mark.asyncio
async def test_transition_blocked_exit_clears_wait_in_tx(env):
    """blocked-exit 支同样把清等待并进那次提交。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await ts.block_task(PROJECT_ID, tid, "dependency:x")
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    seen: list[tuple[int, int]] = []
    with patch(
        "hiveweave.services.tasks.transitions.publish_task_event",
        _probe(env["workspace"], seen),
    ):
        with pytest.raises(RuntimeError):
            await ts._transition(PROJECT_ID, tid, "running")

    assert seen == [(0, base + 1)], "崩溃窗口内等待应已清且 outbox 已落"
    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_transition_multi_clears_wait_in_tx(env):
    """`_transition_multi`（rework 路径）同样覆盖。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await ts._transition(PROJECT_ID, tid, "submitted")
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    seen: list[tuple[int, int]] = []
    with patch(
        "hiveweave.services.tasks.transitions.publish_task_event",
        _probe(env["workspace"], seen),
    ):
        with pytest.raises(RuntimeError):
            await ts._transition_multi(
                PROJECT_ID, tid, "reviewing", "rework", "running"
            )

    assert seen == [(0, base + 1)]
    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "running"


@pytest.mark.asyncio
async def test_archive_task_clears_wait_in_tx(env):
    """`archive_task`（唯一另一处触发侧事务）同样覆盖。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    seen: list[tuple[int, int]] = []
    with patch(
        "hiveweave.services.tasks.close.publish_task_event",
        _probe(env["workspace"], seen),
    ):
        with pytest.raises(RuntimeError):
            await ts.archive_task(
                PROJECT_ID, tid, archived_by=COORD, reason="误绑，纠偏"
            )

    assert seen == [(0, base + 1)]
    task = await ts.get_task(PROJECT_ID, tid)
    assert task["is_archived"] == 1 and task["status"] == "cancelled"


# ── 判别性判据：崩溃窗口（提交后、唤醒前）──────────────────────


@pytest.mark.asyncio
async def test_crash_window_keeps_both_facts(env):
    """⭐⭐ 阳性对照：改前此处读到 `(1, base+1)`（等待未清）⇒ 本断言转红。

    崩溃在「提交后、唤醒前」⇒ 本改动保证的那条**状态一致性**成立：
    「等待已清」与「outbox 事件已落」要么都在、要么都不在（回滚方向见
    `test_tx_rollback_keeps_wait_and_no_outbox`）。

    ⚠⚠ **它不保证「唤醒不丢」**（2026-09-21 审计核实，勿再按旧说法理解）：
    `task_event_relay` 投递的 `task_event` 行是 FYI（`services/inbox.py`
    `is_fyi_task_event` + `agents/watcher.py` 只 ACK 不触发），只有
    `task.event_type == "task.blocked"` 带 wake=1（`task_event_relay.py`
    的 `wake_flag`）⇒ **本次投递不会把被唤醒方叫起来**。而 HEAD 原有的
    「30min TTL → `[WAIT_TIMEOUT]` 唤醒」兜底（`game_time._process_wait_contracts`
    → `wait_contract.clear_expired`，判据是 `cleared_at IS NULL`）在等待行
    被事务内清掉后**不再作用于该行**。⇒ 崩溃窗口内该次唤醒=丢失，剩下的网只有
    「其它 inbox 事件」与「silent watchdog 的 has_open_work 穿透（需该 agent
    名下有开放义务）」。**让唤醒本身 durable = 独立议题（P2-5b）**。
    """
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    calls: list[str] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)

    seen: list[tuple[int, int]] = []
    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        with patch(
            "hiveweave.services.tasks.transitions.publish_task_event",
            _probe(env["workspace"], seen),
        ):
            with pytest.raises(RuntimeError):
                await ts._transition(PROJECT_ID, tid, "submitted")

    assert seen == [(0, base + 1)]
    assert calls == [], "唤醒发生在提交之后，崩溃窗口内不应已唤醒"
    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "submitted"
    assert await _counts(env["workspace"], tid) == (0, base + 1)


# ── 降级路径（blocked-exit 主事务失败）不得双重唤醒 ──────────────


@pytest.mark.asyncio
async def test_blocked_exit_degraded_path_wakes_once(env):
    """主事务失败 ⇒ 退化为「事务外清等待」，唤醒**恰好一次**（回归 2 次实测）。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await ts.block_task(PROJECT_ID, tid, "dependency:x")
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    calls: list[str] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)

    def _broken(cleared_at_ms, wait_ids):
        return ("UPDATE agent_waits SET cleared_at = ? WHERE no_such_col = ?",
                [cleared_at_ms, "x"])

    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        with patch(
            "hiveweave.services.tasks.transitions.build_task_wait_clear_statement",
            _broken,
        ):
            await ts._transition(PROJECT_ID, tid, "running")

    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "running"
    assert calls == [COORD]
    # 状态转换保住了，等待也由兜底清掉，outbox 恰好 +1（主事务整体回滚）
    assert await _counts(env["workspace"], tid) == (0, base + 1)


# ── 回滚方向：不得只落一边 ──────────────────────────────────


@pytest.mark.asyncio
async def test_tx_rollback_keeps_wait_and_no_outbox(env):
    """清等待语句失败 ⇒ 整批回滚：等待仍活跃、outbox 无新行、状态未变。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)
    base = await _outbox_base(env["workspace"], tid)

    def _broken(cleared_at_ms, wait_ids):
        return ("UPDATE agent_waits SET cleared_at = ? WHERE no_such_col = ?",
                [cleared_at_ms, "x"])

    with patch(
        "hiveweave.services.tasks.transitions.build_task_wait_clear_statement",
        _broken,
    ):
        with pytest.raises(Exception):
            await ts._transition(PROJECT_ID, tid, "submitted")

    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "running"
    assert await _counts(env["workspace"], tid) == (1, base)


# ── 判据域：ref 的两种书写形态都要清到（取证见 close.py docstring）──────


@pytest.mark.asyncio
async def test_prefix_form_ref_is_also_cleared(env):
    """ref 写成 8 位短号（实测 214/2135 行如此）也必须被清 —— 裸 UUID 匹配是假阴性。"""
    ts = TaskService()
    tid = await _running_task(ts)
    conn = await project_db.ensure_project_db(env["workspace"])
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO agent_waits "
        "(id, agent_id, project_id, kind, ref, wake_on, expires_at, "
        "created_at, cleared_at) VALUES (?, ?, ?, 'task', ?, '[]', ?, ?, NULL)",
        [str(uuid.uuid4()), COORD, PROJECT_ID, tid[:8], now + 3_600_000, now],
    )
    await conn.commit()
    base = await _outbox_base(env["workspace"], tid)

    calls: list[str] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)

    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        await ts._transition(PROJECT_ID, tid, "submitted")

    assert calls == [COORD]
    assert await _counts(env["workspace"], tid) == (0, base + 1)


@pytest.mark.asyncio
async def test_same_agent_multiple_wait_rows_wakes_once(env):
    """同一 agent 对同一 task 多条等待行 ⇒ 只唤醒一次（批量形态 id IN(…)）。"""
    ts = TaskService()
    tid = await _running_task(ts)
    await _add_task_wait(env["workspace"], COORD, tid)
    await _add_task_wait(env["workspace"], COORD, tid)
    await _add_task_wait(env["workspace"], EXEC, tid)

    calls: list[str] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)

    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        await ts._transition(PROJECT_ID, tid, "submitted")

    assert sorted(calls) == sorted([COORD, EXEC])


# ── 空态：无等待行时不得拼出空 `IN ()`（语法错）或空跑事务 ────────


@pytest.mark.asyncio
async def test_transition_without_waiters_is_unchanged(env):
    """无 waiter 的常见路径：转换照常、outbox 恰好 +1、无唤醒调用。"""
    ts = TaskService()
    tid = await _running_task(ts)
    base = await _outbox_base(env["workspace"], tid)

    calls: list[str] = []

    async def _fake_trigger(agent_id: str, **kwargs):
        calls.append(agent_id)

    with patch("hiveweave.agents.trigger.trigger_subordinate", _fake_trigger):
        await ts._transition(PROJECT_ID, tid, "submitted")

    assert calls == []
    assert (await ts.get_task(PROJECT_ID, tid))["status"] == "submitted"
    assert await _counts(env["workspace"], tid) == (0, base + 1)
