"""Agent 实时活动状态推导（八轮 TEST_DSH_38 前端观测缺口）。

锁死核心推导规则（正交事实位 → 相位，优先级 tool > llm > subagent >
working > waiting > idle）：
1. 未收口 run_step（ended_at IS NULL）→ phase=tool，detail=工具名
2. is_streaming=1 且未超僵尸阈值 → phase=llm
3. agent_waits kind='subagent' 且 cleared_at IS NULL → phase=subagent
4. 开放 run（ended_at IS NULL）无未收口步 → phase=working
5. 其他开放等待 → phase=waiting
6. 以上皆无 → phase=idle
另：僵尸流式行（超过 12min）不得进入 live 状态。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db

PROJECT_ID = "activity-test"
WS = None  # 由 fixture 填充


def _ws() -> str:
    assert WS is not None
    return WS


@pytest.fixture
async def env():
    global WS
    with tempfile.TemporaryDirectory() as tmpdir:
        WS = str(Path(tmpdir).resolve())
        with patch(
            "hiveweave.services.tasks.db._resolve_workspace",
            side_effect=lambda pid: WS if pid == PROJECT_ID else None,
        ):
            yield {"ws": WS}
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(WS, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._write_locks.pop(WS, None)


async def _seed(conn, sql: str, params: list) -> None:
    cur = await conn.execute(sql, params)
    await cur.close()


def _phase_of(result: dict, agent_id: str) -> dict:
    for a in result["agents"]:
        if a["agent_id"] == agent_id:
            return a
    raise AssertionError(f"agent {agent_id} missing from result")


NOW = int(time.time() * 1000)


@pytest.mark.asyncio
async def test_priority_and_phases(env):
    """tool > llm > subagent > working > waiting > idle 全序。"""
    from hiveweave.services.agent_activity import live_status

    conn = await project_db.ensure_project_db(_ws())
    aids = {
        "a-tool": "agent-tool-1",
        "a-llm": "agent-llm-1",
        "a-sub": "agent-sub-1",
        "a-work": "agent-work-1",
        "a-wait": "agent-wait-1",
        "a-idle": "agent-idle-1",
    }
    for name, aid in aids.items():
        await _seed(
            conn,
            "INSERT INTO agents (id, short_id, project_id, name, role, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'executor', 'active', ?, ?)",
            [aid, name, PROJECT_ID, name, NOW, NOW],
        )
    # a-tool：开放 run + 未收口步骤
    await _seed(
        conn,
        "INSERT INTO agent_runs (id, agent_id, status, started_at) "
        "VALUES ('run-t', 'agent-tool-1', 'running', ?)",
        [NOW - 60_000],
    )
    await _seed(
        conn,
        "INSERT INTO run_steps (id, run_id, step_index, step_type, tool_name, status, started_at) "
        "VALUES ('step-t', 'run-t', 1, 'tool_call', 'pwsh', 'running', ?)",
        [NOW - 30_000],
    )
    # a-llm：流式消息（无开放 run/步）
    await _seed(
        conn,
        "INSERT INTO chat_messages (id, agent_id, role, content, is_streaming, created_at) "
        "VALUES ('msg-llm', 'agent-llm-1', 'assistant', '输出中…', 1, ?)",
        [NOW - 10_000],
    )
    # a-sub：subagent 等待
    await _seed(
        conn,
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "created_at) VALUES ('w-sub', 'agent-sub-1', ?, 'subagent', 'job-1', ?)",
        [PROJECT_ID, NOW - 20_000],
    )
    # a-work：开放 run 但无未收口步
    await _seed(
        conn,
        "INSERT INTO agent_runs (id, agent_id, status, started_at) "
        "VALUES ('run-w', 'agent-work-1', 'running', ?)",
        [NOW - 45_000],
    )
    # a-wait：task 等待
    await _seed(
        conn,
        "INSERT INTO agent_waits (id, agent_id, project_id, kind, ref, "
        "created_at) VALUES ('w-task', 'agent-wait-1', ?, 'task', 'task-9', ?)",
        [PROJECT_ID, NOW - 15_000],
    )
    # a-idle：什么都不插
    await conn.commit()

    result = await live_status(PROJECT_ID)
    assert _phase_of(result, "agent-tool-1")["phase"] == "tool"
    assert _phase_of(result, "agent-tool-1")["detail"] == "pwsh"
    assert _phase_of(result, "agent-llm-1")["phase"] == "llm"
    assert _phase_of(result, "agent-sub-1")["phase"] == "subagent"
    assert _phase_of(result, "agent-work-1")["phase"] == "working"
    assert _phase_of(result, "agent-wait-1")["phase"] == "waiting"
    assert _phase_of(result, "agent-idle-1")["phase"] == "idle"


@pytest.mark.asyncio
async def test_zombie_streaming_not_reported(env):
    """超龄流式行（崩溃残留 is_streaming=1）不得进入 live 状态。"""
    from hiveweave.services.agent_activity import live_status, _ZOMBIE_STREAMING_MS

    conn = await project_db.ensure_project_db(_ws())
    await _seed(
        conn,
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at, updated_at) VALUES ('agent-zombie', 'A-z', ?, 'z', "
        "'executor', 'active', ?, ?)",
        [PROJECT_ID, NOW, NOW],
    )
    await _seed(
        conn,
        "INSERT INTO chat_messages (id, agent_id, role, content, is_streaming, created_at) "
        "VALUES ('msg-z', 'agent-zombie', 'assistant', '残留', 1, ?)",
        [NOW - _ZOMBIE_STREAMING_MS - 60_000],
    )
    await conn.commit()
    result = await live_status(PROJECT_ID)
    assert _phase_of(result, "agent-zombie")["phase"] == "idle"


@pytest.mark.asyncio
async def test_best_effort_missing_table_degrades(env):
    """表缺失/查询失败降级为空，不炸端点（best-effort 只读）。"""
    from hiveweave.services import agent_activity

    # 未 ensure_project_db 的空白 workspace → 表不存在 → 仍返回结构
    result = await agent_activity.live_status(PROJECT_ID)
    assert "agents" in result and "generated_at" in result
