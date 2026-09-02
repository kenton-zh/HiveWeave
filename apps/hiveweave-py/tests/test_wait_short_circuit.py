"""事实短路 #5：kind=task 等待创建时目标已终态 → 当场清等待并唤醒。

07 实测：M3/M4/M5 早已 approved，凛川 20:34 挂 task_transition 等待——
事件在等待创建前就已发生，唤醒永远不会触发，只能等 TTL 超时（僵尸 4.5h）。
修复：replace_waits 事务提交后立即核对任务状态，已终态则清除等待行 +
trigger_subordinate 唤醒（与任务转换唤醒同路径）；未终态的等待不受影响。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.wait_contract import WaitContractService

PROJECT_ID = "short-circuit-proj"
COORD = "coord-sc"
EXEC = "exec-sc"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _make_approved_task(env) -> tuple[str, str]:
    """走完整状态机造一个 approved 任务（create→running→submitted→approved）。"""
    ts = TaskService()
    pid, ws = env["project_id"], env["workspace"]
    tid = await ts.create_task(
        pid, "Approved milestone", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.start_task(pid, tid)
    await ts.submit_task(pid, tid, {"summary": "done", "tests_passed": True})
    await ts.start_review(pid, tid, reviewer_id=COORD)
    await ts.review_task(pid, tid, "approve", reviewer_id=COORD)
    task = await ts.get_task(pid, tid)
    assert task["status"] == "approved"
    return tid, ws


async def _make_running_task(env) -> str:
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid, "Running milestone", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.start_task(pid, tid)
    return tid


@pytest.mark.asyncio
async def test_wait_on_approved_task_short_circuits(env, monkeypatch):
    """等一个已 approved 的任务：等待被当场清除 + 唤醒，不留僵尸。"""
    tid, ws = await _make_approved_task(env)

    triggered: list[str] = []

    async def fake_trigger(agent_id: str) -> None:
        triggered.append(agent_id)

    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", fake_trigger
    )

    svc = WaitContractService()
    created = await svc.replace_waits(
        PROJECT_ID,
        EXEC,
        [{"kind": "task", "ref": tid}],
        phase="waiting",
    )
    assert len(created) == 1

    active = await svc.list_all_active(PROJECT_ID)
    mine = [w for w in active if w["agentId"] == EXEC]
    assert mine == [], "已终态任务的等待必须被当场清除，不能留僵尸"

    assert triggered == [EXEC], "必须立即唤醒等待者（而不是等 TTL 超时）"


@pytest.mark.asyncio
async def test_wait_on_running_task_stays_active(env, monkeypatch):
    """未终态任务的等待是合法的——不能被误清。"""
    tid = await _make_running_task(env)

    triggered: list[str] = []

    async def fake_trigger(agent_id: str) -> None:
        triggered.append(agent_id)

    monkeypatch.setattr(
        "hiveweave.agents.trigger.trigger_subordinate", fake_trigger
    )

    svc = WaitContractService()
    created = await svc.replace_waits(
        PROJECT_ID,
        EXEC,
        [{"kind": "task", "ref": tid}],
        phase="waiting",
    )
    assert len(created) == 1

    active = await svc.list_all_active(PROJECT_ID)
    mine = [w for w in active if w["agentId"] == EXEC]
    assert len(mine) == 1 and mine[0]["ref"] == tid
    assert triggered == []
