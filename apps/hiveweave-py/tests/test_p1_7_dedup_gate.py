"""P1-7 ②：查重门**下沉到唯一写口** `crud.create_task`（消三条旁路）。

病灶（§10.3 真根因 A）：门只在工具层（`tools/tasks/create.py` / `dispatch.py`），
而 `crud.py:87 create_task` 内**零查重** ⇒ HTTP `POST /tasks`、`services/dispatch.py`、
`tools/misc_tools.py` 三条旁路都能绕过。次生：查重窗口曾被 `LIMIT 40` 截断（③ 已修）。

判据（状态判据）：
- AC1 同标题再建 ⇒ **open 计数不增**（且返回既有 id —— 与工具层回执里既有的
  「请复用 dispatch_task(...)」同向：**复用而非拦截**）；
- 豁免必须成立：`source="system"`（平台内部 spawn，如 VERIFY）与显式 `kind` 都不受影响 ——
  否则 VERIFY 自动建单会被挡掉；
- `dedup_policy="allow"` ⇒ 完全跳过（给需要并行同名的场景留出口）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-p1-7-gate"
TITLE = "实现导出功能的接口与测试"


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


async def _open_count(env) -> int:
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE is_archived = 0 "
        "AND status NOT IN ('done','cancelled','archived','completed','closed')"
    )
    n = int((await cur.fetchone())[0])
    await cur.close()
    return n


async def _new(ts, env, **kw) -> str:
    return await ts.create_task(
        env["project_id"], TITLE, "d", creator_id="coord", assignee_id="exec-1", **kw
    )


@pytest.mark.asyncio
async def test_ac1_same_title_does_not_grow_open_count(env):
    ts = TaskService()
    first = await _new(ts, env)
    before = await _open_count(env)
    second = await _new(ts, env)
    assert second == first, "同标题再建应复用既有任务（返回既有 id）"
    assert await _open_count(env) == before, "open 计数不增（AC1）"


@pytest.mark.asyncio
async def test_system_source_is_exempt(env):
    ts = TaskService()
    first = await _new(ts, env)
    other = await _new(ts, env, source="system")
    assert other != first, "source=system（平台内部 spawn）必须豁免"


@pytest.mark.asyncio
async def test_explicit_kind_is_exempt(env):
    ts = TaskService()
    first = await _new(ts, env)
    other = await _new(ts, env, kind="verify")
    assert other != first, "显式 kind（VERIFY 等系统种类）必须豁免"


@pytest.mark.asyncio
async def test_allow_policy_creates_anyway(env):
    ts = TaskService()
    first = await _new(ts, env)
    other = await _new(ts, env, dedup_policy="allow")
    assert other != first, "dedup_policy=allow 应完全跳过检查"


@pytest.mark.asyncio
async def test_different_title_creates_new(env):
    ts = TaskService()
    await _new(ts, env)
    other = await ts.create_task(
        env["project_id"], "完全不同的另一件事", "d",
        creator_id="coord", assignee_id="exec-1",
    )
    assert other, "异标题必须能正常新建"
