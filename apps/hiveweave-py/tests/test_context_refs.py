"""产物引用链（context_refs）测试 — 交接文档 built-on 链 + 反向追溯。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import handoff as handoff_module
from hiveweave.services.handoff import HandoffService
from hiveweave.services.memory import MemoryService

PROJECT_ID = "test-context-refs"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        # 重置迁移缓存，确保本次测试真实走 ALTER 迁移
        handoff_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_memory(pid: str, agent_id: str, content: str) -> None:
    await MemoryService().add_entry(
        agent_id=agent_id, project_id=pid, content=content,
        category="tool_written",
    )


@pytest.mark.asyncio
async def test_dismissal_chain_builds_on_references(env):
    pid = env["project_id"]
    ws = env["workspace_path"]
    hs = HandoffService()

    # A 被解散，parent=B → 生成 doc1，交接 to_agent=B
    await _seed_memory(pid, "a1", "A 的私有记忆：签到模块契约")
    r1 = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到工程师",
        parent_id="b1",
    )
    assert r1["document_path"], "A 解散应生成交接文档"
    doc1 = r1["document_path"]

    # B（在职期间收到 doc1）被解散，parent=C → doc2 应引用 doc1
    await _seed_memory(pid, "b1", "B 的私有记忆：在签到之上扩展")
    r2 = await hs.create_dismissal_handoff(
        pid, "b1", agent_name="B", short_id="B001", role="扩展工程师",
        parent_id="c1",
    )
    assert r2["document_path"], "B 解散应生成交接文档"
    doc2 = r2["document_path"]

    # 1. doc2 的 handoff 行 context_refs 应包含 doc1
    rows = await handoff_module._query(
        pid,
        "SELECT context_refs FROM handoffs WHERE artifact_path = ?",
        [doc2],
    )
    assert rows, "doc2 应有关联 handoff 行"
    refs = json.loads(rows[0]["context_refs"] or "[]")
    assert any(r.get("path") == doc1 for r in refs), (
        f"doc2 应引用 doc1，实际 refs={refs}"
    )

    # 2. 反向追溯：doc1 被 doc2 引用
    reverse = await hs.get_reverse_references(pid, doc1)
    assert any(r.get("artifact_path") == doc2 for r in reverse), (
        f"doc1 应被 doc2 反向引用，实际={reverse}"
    )

    # 3. 文档正文含 References(built on) 小节，且每个引用路径可读
    doc2_text = Path(doc2).read_text(encoding="utf-8")
    assert "## References (built on)" in doc2_text
    assert doc1 in doc2_text
    assert Path(doc1).exists(), "引用路径必须真实存在（可被 read_file 读到）"


@pytest.mark.asyncio
async def test_get_incoming_references(env):
    pid = env["project_id"]
    hs = HandoffService()

    await _seed_memory(pid, "a1", "A 记忆")
    r1 = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到工程师",
        parent_id="b1",
    )
    assert r1["document_path"]

    # get_incoming_references 应返回 B 收到的交接文档
    incoming = await hs.get_incoming_references(pid, "b1")
    assert any(r.get("path") == r1["document_path"] for r in incoming), (
        f"b1 应收到 doc1，实际={incoming}"
    )


@pytest.mark.asyncio
async def test_get_incoming_references_empty(env):
    """空记忆解散：无文档 → 不建 handoff → 无 incoming 引用残留。"""
    pid = env["project_id"]
    hs = HandoffService()

    r = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到工程师",
        parent_id="b1",
    )
    assert r["document_path"] == "", "空记忆应无交接文档"
    assert r["handoff_id"] is None, "空记忆不应建 handoff"

    incoming = await hs.get_incoming_references(pid, "b1")
    assert incoming == [], "无文档则 b1 不应有 incoming 引用"


@pytest.mark.asyncio
async def test_reverse_references_survives_dirty_rows(env):
    """P1-1 回归：context_refs 为 NULL/空串/malformed 的行不炸掉反向查询。"""
    pid = env["project_id"]
    ws = env["workspace_path"]
    hs = HandoffService()

    # 造一个真实引用链：A→B 生成 doc1；B（收到 doc1）→C 生成 doc2 引用 doc1
    await _seed_memory(pid, "a1", "A 记忆")
    r1 = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到",
        parent_id="b1",
    )
    doc1 = r1["document_path"]
    await _seed_memory(pid, "b1", "B 记忆")
    r2 = await hs.create_dismissal_handoff(
        pid, "b1", agent_name="B", short_id="B001", role="扩展",
        parent_id="c1",
    )
    doc2 = r2["document_path"]

    # 手动插入脏行：空串、malformed、合法对象（非数组）、null
    import time as _t
    now = int(_t.time() * 1000)
    for i, bad in enumerate(["", "not-json", "{}", "null"]):
        await handoff_module._execute(
            pid,
            "INSERT INTO handoffs (id, from_agent_id, to_agent_id, summary, "
            "status, created_at, updated_at, context_refs) "
            "VALUES (?, 'x', 'y', 'dirty', 'pending', ?, ?, ?)",
            [f"dirty-{i}", now, now, bad],
        )

    # 反向追溯不应抛出异常，且不受脏行影响，仍命中 doc2→doc1 的真实引用
    reverse = await hs.get_reverse_references(pid, doc1)
    assert any(r.get("artifact_path") == doc2 for r in reverse), (
        f"脏行不应影响正常反向追溯，实际={reverse}"
    )


@pytest.mark.asyncio
async def test_ensure_schema_migrates_old_handoffs_table(env):
    """P2-2：老库缺 context_refs 列时，_ensure_schema 真实 ALTER ADD COLUMN。"""
    pid = env["project_id"]
    # 触发建库（含 context_refs），随后模拟"老库"：删表重建为无 context_refs 的旧 schema
    await handoff_module._ensure_schema(pid)
    conn = await handoff_module._conn(pid)
    await conn.execute("DROP TABLE handoffs")
    await conn.execute(
        "CREATE TABLE handoffs (id TEXT PRIMARY KEY, from_agent_id TEXT, "
        "to_agent_id TEXT, module_id TEXT, summary TEXT, status TEXT, "
        "created_at INTEGER, updated_at INTEGER)"
    )
    await conn.commit()
    # 清缓存强制重跑 _ensure_schema → 走真实 ADD COLUMN 路径
    handoff_module._migrated.discard(pid)
    await handoff_module._ensure_schema(pid)

    cols = await handoff_module._query(
        pid,
        "SELECT name FROM pragma_table_info('handoffs') WHERE name = 'context_refs'",
    )
    assert cols, "老库经 _ensure_schema 后应补上 context_refs 列"


@pytest.mark.asyncio
async def test_dismissal_handoff_reused_reports_count(env):
    """H8 幂等 / P3-1：重试解散复用已有交接，且 memory_count 为真实归档数。"""
    pid = env["project_id"]
    hs = HandoffService()

    await _seed_memory(pid, "a1", "A 记忆")
    r1 = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到",
        parent_id="b1",
    )
    assert r1["document_path"]
    assert r1["memory_count"] == 1

    # 再次解散同一 agent → 复用，不重复生成文档
    r2 = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="A", short_id="A001", role="签到",
        parent_id="b1",
    )
    assert r2["reused"] is True
    assert r2["document_path"] == r1["document_path"]
    assert r2["memory_count"] == 1, "复用分支应返回真实归档数，而非 None/0"