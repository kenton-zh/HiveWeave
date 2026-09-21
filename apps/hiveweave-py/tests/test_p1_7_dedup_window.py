"""P1-7 ③：查重的候选窗不得在**标题过滤之前**截断。

病灶（§10.3 次生）：`find_similar_open_task` 原实现
`ORDER BY created_at DESC LIMIT 40` ⇒ 先取最近 40 条 open 任务、再在 Python 里比标题
⇒ open 任务 > 40 时，命中项落窗外即**静默查重失效**（同一件事被派两次）。

判据（AC3，状态判据）：造 45 条 open 且**同名的那条是最旧的**（必然落窗外）
⇒ `find_similar_open_task` 必须仍返回它。改前此处返回 None。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService

PROJECT_ID = "test-p1-7-dedup-window"
DUP_TITLE = "实现导出功能的接口与测试"


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


@pytest.mark.asyncio
async def test_duplicate_beyond_40_open_tasks_is_still_found(env):
    ts = TaskService()
    pid = env["project_id"]
    # 先建「同名」那条（最旧 ⇒ 在 40 行窗口之外），再堆 44 条无关任务
    dup_id = await ts.create_task(pid, DUP_TITLE, "d", creator_id="coord")
    for i in range(44):
        await ts.create_task(pid, f"无关任务 {i:02d}", "d", creator_id="coord")

    hit = await ts.find_similar_open_task(pid, DUP_TITLE)
    assert hit is not None, (
        "同名任务落 40 行窗外后查重静默失效 —— 同一件事会被派两次"
    )
    assert hit["id"] == dup_id
