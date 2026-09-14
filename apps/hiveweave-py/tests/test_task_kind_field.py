"""`tasks.kind` 字段（#11 阶段 A）：闭合枚举语义 + 落库往返 + 「不读标题」。

## 背景

#11 的病灶：VERIFY 的判定用的是**任务标题**（`is_verify_title`，文本判据）——
改标题即可翻转全部验收门与串行锁：真验收写成「验收：xxx」⇒ 不被认；
普通任务加 `VERIFY:` 前缀 ⇒ 伪装成 VERIFY ⇒ 拿到 VERIFY 的隔离门/MAIN 证据闸/
串行锁。

阶段 A（本文件覆盖）只做**纯增量**：加列 + 创建点写值 + 闭合枚举访问器。
**此时还没有任何消费方读它** ⇒ 行为零变化（翻转是阶段 B）。

## 本文件钉三件事

1. **闭合枚举**：非成员/缺失 ⇒ None（「未知」**不猜**，对齐
   `services/delivery_plane.py::normalize_delivery_plane` 的范式）；
2. **落库往返**：`create_task(kind=…)` 写进去、`get_task`/`list_tasks` 读得回
   —— 后者靠 `crud._COLUMNS` 含 `kind`（漏了的话字段恒 None，是"单测绿、生产
   里字段恒空"那个坑）；
3. **判定与标题完全无关**：`kind='verify'` 配普通标题 ⇒ 认；标题像 VERIFY
   但没有 kind ⇒ **不认**（这正是验收②「普通任务加前缀不被认」）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.tasks.crud import CrudMixin
from hiveweave.services.tasks.verify import (
    VERIFY_KIND,
    is_verify_task,
    task_kind,
)

PROJECT_ID = "test-kind-project"
AGENT_ID = "test-kind-agent"


# ── 1. 闭合枚举（纯函数） ────────────────────────────────────────


def test_task_kind_is_a_closed_enum_unknown_is_none():
    """非成员/缺失/脏类型 ⇒ None（**不猜**）。"""
    assert task_kind({"kind": VERIFY_KIND}) == VERIFY_KIND
    assert task_kind({"kind": "VERIFY"}) == VERIFY_KIND, "大小写/空白应归一"
    assert task_kind({"kind": "  Verify  "}) == VERIFY_KIND
    for bad in (None, {}, {"kind": None}, {"kind": ""}, {"kind": "task"},
                {"kind": "verification"}, {"kind": 42}, "not-a-dict", []):
        assert task_kind(bad) is None, f"非法输入必须返回 None（不猜）：{bad!r}"


def test_verify_judgment_reads_kind_not_title():
    """判定只认 `kind`；标题**完全不参与**。

    这是 #11 的核心验收：① 真 VERIFY 换个标题照样被认；② 普通任务加
    `VERIFY:` 前缀**不被认**。
    """
    # ① kind 对 ⇒ 标题长什么样都被认（含「验收：xxx」这个原判据认不出的形态）
    for title in ("验收：M4 收口", "QA：收口", "随便什么标题", None, ""):
        assert is_verify_task({"kind": VERIFY_KIND, "title": title}) is True

    # ② 标题像 VERIFY 但没 kind ⇒ **不认**（旧的文本判据在这里会误认）
    for title in ("VERIFY: 伪造的", "【VERIFY: 伪造的】", "VERIFY：伪造的"):
        assert is_verify_task({"title": title}) is False, (
            f"普通任务加 VERIFY 前缀不得被认成 VERIFY（验收②）：{title!r}"
        )
        assert is_verify_task({"kind": None, "title": title}) is False
        assert is_verify_task({"kind": "task", "title": title}) is False

    assert is_verify_task(None) is False


def test_wide_select_columns_include_kind():
    """`crud._COLUMNS` 必须含 `kind`。

    漏了它的后果是"单测全绿、生产里字段恒 None"—— 宽查询
    （`SELECT {_COLUMNS} FROM tasks`）会把字段悄悄吞掉，而所有消费点读到的
    都是 None ⇒ 归因静默降级。（本仓踩过同款：`runner_failed`/`dialect_failed`
    都因漏登记字段白名单而恒 None。）
    """
    cols = [c.strip() for c in CrudMixin._COLUMNS.split(",")]
    assert "kind" in cols, "`_COLUMNS` 漏了 kind ⇒ 宽查询读不到它"


# ── 2. 落库往返（穿透调用链） ────────────────────────────────────


@pytest.fixture
async def env():
    """真实 per-project DB + mock meta 路由；**teardown 先关连接再删目录**
    （Windows 上打开的文件句柄会挡住临时目录删除 —— 照抄
    `tests/test_task_service.py` 的 Windows-safe 清理）。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid == AGENT_ID else None

        task_module._migrated.clear()
        project_db._agent_cache.pop(AGENT_ID, None)

        with patch("hiveweave.db.meta.get_project_workspace",
                   fake_get_project_workspace), \
             patch("hiveweave.db.meta.get_agent_project_id",
                   fake_get_agent_project_id):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path,
                   "agent_id": AGENT_ID}

        # Cleanup: 先关掉缓存连接，再让 tempdir 删目录。
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(AGENT_ID, None)


@pytest.mark.asyncio
async def test_kind_round_trips_through_create_and_read(env):
    """`create_task(kind=…)` 写进去、`get_task`/`list_tasks` 读得回。"""
    ts = TaskService()

    plain_id = await ts.create_task(
        env["project_id"], "普通任务", "desc", env["agent_id"]
    )
    verify_id = await ts.create_task(
        env["project_id"], "随便什么标题都行", "desc", env["agent_id"],
        source="system", kind=VERIFY_KIND,
    )

    plain = await ts.get_task(env["project_id"], plain_id)
    verify = await ts.get_task(env["project_id"], verify_id)

    assert plain is not None and verify is not None
    assert task_kind(plain) is None, "普通任务必须留 NULL（不是 DEFAULT 值）"
    assert is_verify_task(verify) is True, (
        "kind 没落库或宽查询没带上它 —— 这是阶段 B 翻转的前提"
    )

    # 列表路径同样要带上（消费点多半走 list_tasks）
    listed = {t["id"]: t for t in await ts.list_tasks(env["project_id"])}
    assert task_kind(listed[verify_id]) == VERIFY_KIND
    assert task_kind(listed[plain_id]) is None


@pytest.mark.asyncio
async def test_column_exists_in_a_freshly_created_db(env):
    """新库直接就有该列（正典 DDL 生效，不依赖 ALTER 路径）。"""
    conn = await project_db.ensure_project_db(env["workspace"])
    cur = await conn.execute("SELECT name FROM pragma_table_info('tasks')")
    cols = {r[0] for r in await cur.fetchall()}
    assert "kind" in cols
