"""inbox._ensure_schema 补列标记回归（09-08 office-godot 事故）。

事故：ALTER 撞瞬态锁被 `except: pass` 吞掉后无条件标 `_migrated`，
收养项目整个进程生命周期 wake 列缺失 → send_message/inbox_watcher
全断（CEO 招聘指令发不出，日志 'no such column: wake'）。

回归点：
1. 旧 schema 库（无 wake 等 _MISSING_COLUMNS 列）→ send_message 补列
   成功且消息落库；
2. ALTER 瞬态失败 → 不标记 `_migrated`（PRAGMA 验列不过），下次调用
   重试并补齐；
3. 列齐后标记 `_migrated`，重复调用不再打 ALTER。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services.inbox import InboxService

PROJECT_ID = "test-inbox-schema"
CEO_ID = "schema-ceo"
DEV_ID = "schema-dev"

# 旧 schema：schema.py 的 inbox 正典 CREATE 本就无 wake 等新增列
_OLD_INBOX_DDL = """
CREATE TABLE inbox (
    id TEXT PRIMARY KEY,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    message TEXT,
    read INTEGER DEFAULT 0,
    created_at INTEGER,
    message_type TEXT,
    expect_report INTEGER DEFAULT 0,
    priority TEXT DEFAULT 'normal',
    task_id TEXT
)
"""


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (CEO_ID, DEV_ID) else None

        async def fake_get_agent_by_id(aid: str):
            return {"id": aid, "name": "x", "status": "active"}

        async def fake_publish(*args, **kwargs):
            return None

        # 标记键自 2026-09-11 起是 (workspace, 连接世代) 元组 —— 逐键 discard 已不适用，
        # 本文件只测这一个 memo，直接清空即可（保证用例间隔离）。
        inbox_module._migrated.clear()
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch(
                "hiveweave.realtime.event_bus.status_event_bus"
                ".publish_chat_message",
                fake_publish,
            ),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)
        inbox_module._migrated.clear()


async def _inbox_columns(workspace_path: str) -> set[str]:
    conn = await project_db.ensure_project_db(workspace_path)
    cur = await conn.execute("PRAGMA table_info(inbox)")
    rows = await cur.fetchall()
    await cur.close()
    return {r[1] for r in rows}


@pytest.mark.asyncio
async def test_old_schema_db_gets_columns_on_send(env):
    """事故主回归：旧 schema 库首条 send_message 补齐 wake 等列并落库。"""
    ws = env["workspace_path"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute("DROP TABLE IF EXISTS inbox")
    await conn.execute(_OLD_INBOX_DDL)
    await conn.commit()

    assert "wake" not in await _inbox_columns(ws)

    result = await InboxService().send_message(CEO_ID, DEV_ID, "你好")
    assert result.get("ok") is not False

    cols = await _inbox_columns(ws)
    assert "wake" in cols
    assert "delivered" in cols
    # 消息真实落库（INSERT 带 wake 不再炸）
    check = await project_db.query(
        CEO_ID, "SELECT message FROM inbox WHERE to_agent_id=?", [DEV_ID]
    )
    assert check and check[0]["message"] == "你好"


@pytest.mark.asyncio
async def test_transient_alter_failure_does_not_mark_migrated(env):
    """核心回归：ALTER 瞬态失败（锁）不得标记 _migrated，下次调用重试。"""
    ws = env["workspace_path"]
    conn = await project_db.ensure_project_db(ws)
    await conn.execute("DROP TABLE IF EXISTS inbox")
    await conn.execute(_OLD_INBOX_DDL)
    await conn.commit()

    original_execute = project_db.execute
    attempted = {"failed_once": False}

    async def flaky_execute(agent_id: str, sql: str, *args, **kwargs):
        if "ALTER TABLE inbox ADD COLUMN wake" in sql and not attempted[
            "failed_once"
        ]:
            attempted["failed_once"] = True
            raise RuntimeError("database is locked")
        return await original_execute(agent_id, sql, *args, **kwargs)

    with patch.object(project_db, "execute", flaky_execute):
        await inbox_module._ensure_schema(CEO_ID)

    # 第一次：wake 补列失败 → 未标记，可重试
    assert "wake" not in await _inbox_columns(ws)

    await inbox_module._ensure_schema(CEO_ID)
    assert "wake" in await _inbox_columns(ws)

    # 列齐后已标记：第三次调用不再发 ALTER（记录调用而非抛哨兵——
    # AssertionError 会被 _ensure_schema 的 except Exception 吞掉，审计 M1）
    async def recording_execute(agent_id: str, sql: str, *args, **kwargs):
        if "ALTER TABLE inbox" in sql:
            alter_calls.append(sql)
        return await original_execute(agent_id, sql, *args, **kwargs)

    alter_calls: list[str] = []
    with patch.object(project_db, "execute", recording_execute):
        await inbox_module._ensure_schema(CEO_ID)
    assert alter_calls == [], (
        "already-migrated workspace must not re-ALTER"
    )


@pytest.mark.asyncio
async def test_duplicate_column_is_benign(env):
    """「duplicate column」仍视为良性：列已在库时静默、正常标记。"""
    ws = env["workspace_path"]
    await inbox_module._ensure_schema(CEO_ID)
    assert "wake" in await _inbox_columns(ws)
    # 再次调用（标记短路）不抛错
    await inbox_module._ensure_schema(CEO_ID)


@pytest.mark.asyncio
async def test_recreated_db_at_same_path_gets_columns_again(env):
    """09-11 TEST_DSH_52_A 事故回归：**同一路径上的库被整代重建后必须能重新补列**。

    事故形态（实测：`TEST_DSH_52_A` 的 inbox 只有正典 10 列，缺 9 个补列）：
    库曾补齐过 → 标记**按 workspace 路径**留存 → 库被整代重建（项目目录被删/重建）
    回到正典基础列 → `_ensure_schema` 命中旧标记**静默早退** → 永不补列。
    三条失败分支都会打 warning，而全日志 `inbox_schema_*` 0 条 → 只能是从早退走的；
    失效只在**下游**以 `no such column: wake` 炸开，归因完全错位。

    世代维度（`db/project.py::_ws_generation`）让「新建连接 = 新一代」使旧标记失效。
    """
    ws = env["workspace_path"]

    # ── 第一代：旧 schema → 补列成功 → 打上标记 ──
    conn = await project_db.ensure_project_db(ws)
    await conn.execute("DROP TABLE IF EXISTS inbox")
    await conn.execute(_OLD_INBOX_DDL)
    await conn.commit()
    assert "wake" not in await _inbox_columns(ws)

    first = await InboxService().send_message(CEO_ID, DEV_ID, "第一代")
    assert first.get("ok") is not False
    assert "wake" in await _inbox_columns(ws), "第一代应已补列并打标"
    gen1 = project_db.workspace_generation(ws)
    # 守住「标记机制」本身：若有人删掉 _ensure_schema 里的 `_migrated.add(key)`，
    # 第一代就不落键、第二代也不存在旧键 → ALTER 照跑 → 本用例仍会绿
    # （审计指出：「对『标记被悄悄移除』无鉴别力」）。故显式断言键已落。
    assert (ws, gen1) in inbox_module._migrated, (
        "第一代补列成功后必须落 (workspace, 世代) 标记"
    )

    # ── 模拟「同一路径上的库被整代重建」──
    # 丢连接 + 丢 agent 缓存（等价于项目目录被删后重建 → 新库、新连接）。
    async with project_db._ensure_lock:
        old = project_db._cache.pop(ws, None)
    if old is not None:
        await old.close()
    project_db._agent_cache.pop(CEO_ID, None)

    conn2 = await project_db.ensure_project_db(ws)
    await conn2.execute("DROP TABLE IF EXISTS inbox")
    await conn2.execute(_OLD_INBOX_DDL)
    await conn2.commit()
    assert "wake" not in await _inbox_columns(ws), "重建后的库应回到正典基础列"
    assert project_db.workspace_generation(ws) > gen1, "新建连接必须让世代递增"

    # ── 第二代：**不做任何人工清标记**，纯靠世代失效自愈 ──
    second = await InboxService().send_message(CEO_ID, DEV_ID, "第二代")
    assert second.get("ok") is not False
    assert "wake" in await _inbox_columns(ws), (
        "同一路径上的新一代库必须能重新补列 —— 否则就是 TEST_DSH_52_A 事故复发"
    )
    check = await project_db.query(
        CEO_ID, "SELECT message FROM inbox WHERE to_agent_id=?", [DEV_ID]
    )
    assert check and check[-1]["message"] == "第二代"
