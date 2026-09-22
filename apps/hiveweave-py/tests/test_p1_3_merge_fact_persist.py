"""P1-3 ② 的**落库面**：`merge_landed` 必须真的进 `facts` 表。

病灶（2026-09-22 由 TEST_DSH_66 数据暴露）：`fact_bus._persist` 第一行是
``if not fact.project_id: return`` ⇒ 我的 `_publish_merge_landed` 当初**没传
project_id**，于是事实只活在内存环里（进程内唤醒有效），**永不落库**。
铁证：该轮 `git_worktree_merge` **47 步全 completed**、`task.merged` **9 次**，
而项目库 `facts` 表 **0 行**。

判据：
- 两格：`_persist` 带 project_id ⇒ 落行；**不带 ⇒ 不落行**（把机制钉住）；
- AST：`_publish_merge_landed` 里的 `fact_bus.publish(...)` **必须带 `project_id=`**；
- AST：三个成功点的调用都是 `await`（改异步后的契约）。
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import fact_bus, task as task_module
from hiveweave.services.git_worktree import service_merge as sm

PROJECT_ID = "test-p1-3-merge-fact-persist"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return ws if pid == PROJECT_ID else None

        task_module._migrated.clear()
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": ws}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _fact_rows(ws: str, kind: str) -> int:
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "SELECT COUNT(*) FROM facts WHERE kind = ?", [kind]
    )
    n = int((await cur.fetchone())[0])
    await cur.close()
    return n


@pytest.mark.asyncio
async def test_persist_requires_project_id(env):
    """两格：不带 project_id ⇒ **不落库**（这就是 TEST_DSH_66 里 facts 为空的机制）。"""
    fact_bus.reset_for_tests()
    f = fact_bus.publish("merge_landed", "A1", {"branch": "b"}, source="platform")
    await fact_bus._persist(f)
    assert await _fact_rows(env["workspace"], "merge_landed") == 0, (
        "没有 project_id 却落库了 —— 说明落库条件变了，本判据需同步"
    )

    f2 = fact_bus.publish(
        "merge_landed", "A2", {"branch": "b"}, source="platform",
        project_id=env["project_id"],
    )
    await fact_bus._persist(f2)
    assert await _fact_rows(env["workspace"], "merge_landed") == 1, (
        "带 project_id 应落库（否则「事实」只剩内存环，重启即丢）"
    )


def test_merge_publisher_passes_project_id():
    """AST 守卫：`_publish_merge_landed` 内的 `publish(...)` 必须带 `project_id=`。"""
    tree = ast.parse(inspect.getsource(sm))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_publish_merge_landed"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "publish"
    ]
    assert calls, "没找到 fact_bus.publish 调用"
    for c in calls:
        assert any(k.arg == "project_id" for k in c.keywords), (
            "publish 没带 project_id ⇒ 事实永不落库（TEST_DSH_66 的 facts=0 就是这么来的）"
        )


def test_all_merge_success_sites_await_the_publisher():
    """三个成功点都必须是 `await _publish_merge_landed(...)`（改异步后的契约）。"""
    tree = ast.parse(inspect.getsource(sm))
    awaited = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_publish_merge_landed"
    ]
    assert len(awaited) == 3, f"await 调用点数应为 3，实际 {len(awaited)}"
    for n in awaited:
        assert len(n.value.args) >= 2, "第一个参数应是 workspace_path（用于反查 project_id）"
