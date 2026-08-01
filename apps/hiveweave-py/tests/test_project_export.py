"""project-export 调试端点回归测试。

行为审计依赖：run_steps 必须附 agent_id（join agent_runs）；
长文本按 truncate 截断；未知表返回 count=-1；项目不存在返回 404。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import HTTPException

from hiveweave.api.debug import project_export


async def _fake_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("CREATE TABLE agents (id TEXT, name TEXT)")
    await conn.execute("CREATE TABLE agent_runs (id TEXT, agent_id TEXT)")
    await conn.execute("CREATE TABLE run_steps (id TEXT, run_id TEXT, tool_name TEXT, error TEXT)")
    await conn.execute("CREATE TABLE tasks (id TEXT, title TEXT)")
    await conn.execute("INSERT INTO agents VALUES ('a1', '归零')")
    await conn.execute("INSERT INTO agent_runs VALUES ('r1', 'a1')")
    await conn.execute("INSERT INTO run_steps VALUES ('s1', 'r1', 'bash', ?)", ["x" * 5000])
    await conn.execute("INSERT INTO run_steps VALUES ('s2', 'r-missing', 'bash', 'orphan')")
    await conn.execute("INSERT INTO tasks VALUES ('t1', '验证任务')")
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_export_orphan_run_step_survives_left_join():
    """agent_runs 无对应行的 run_steps 必须保留（LEFT JOIN），agent_id 为 None。"""
    conn = await _fake_conn()
    try:
        with patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            new=AsyncMock(return_value=conn),
        ):
            out = await project_export(
                project_id="proj-1", tables="run_steps", truncate=0
            )
    finally:
        await conn.close()

    assert out["counts"]["run_steps"] == 2
    by_id = {r["id"]: r for r in out["tables"]["run_steps"]}
    assert by_id["s1"]["agent_id"] == "a1"
    assert by_id["s2"]["agent_id"] is None
    assert by_id["s2"]["error"] == "orphan"


@pytest.mark.asyncio
async def test_export_max_rows_limits_each_table():
    """max_rows 限制每表行数。"""
    conn = await _fake_conn()
    try:
        with patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            new=AsyncMock(return_value=conn),
        ):
            out = await project_export(
                project_id="proj-1", tables="run_steps", truncate=0, max_rows=1
            )
    finally:
        await conn.close()

    assert out["counts"]["run_steps"] == 1


@pytest.mark.asyncio
async def test_export_rows_with_agent_id_and_truncate():
    """run_steps 附 agent_id；长文本按 truncate 截断。"""
    conn = await _fake_conn()
    try:
        with patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            new=AsyncMock(return_value=conn),
        ):
            out = await project_export(
                project_id="proj-1",
                tables="agents,run_steps,tasks",
                truncate=100,
            )
    finally:
        await conn.close()

    assert out["counts"] == {"agents": 1, "run_steps": 2, "tasks": 1}
    step = out["tables"]["run_steps"][0]
    assert step["agent_id"] == "a1"
    assert step["error"].endswith("[truncated 4900 chars]")
    assert len(step["error"]) == 100 + 25


@pytest.mark.asyncio
async def test_export_unknown_table_count_minus_one():
    """不存在的表不报错，count 记为 -1。"""
    conn = await _fake_conn()
    try:
        with patch(
            "hiveweave.db.project.get_project_db_by_project_id",
            new=AsyncMock(return_value=conn),
        ):
            out = await project_export(
                project_id="proj-1", tables="agents,no_such_table"
            )
    finally:
        await conn.close()

    assert out["counts"]["agents"] == 1
    assert out["counts"]["no_such_table"] == -1
    assert "no_such_table" not in out["tables"]


@pytest.mark.asyncio
async def test_export_project_not_found_raises_404():
    """项目不存在时抛出 HTTPException 404。"""
    from hiveweave.db import project as project_db

    with patch(
        "hiveweave.db.project.get_project_db_by_project_id",
        new=AsyncMock(side_effect=project_db.ProjectDbError("nope")),
    ):
        with pytest.raises(HTTPException) as exc:
            await project_export(project_id="missing")
    assert exc.value.status_code == 404
