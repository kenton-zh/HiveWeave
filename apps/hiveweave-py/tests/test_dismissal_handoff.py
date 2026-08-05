"""离职交接（dismissal handoff）测试 — 文档生成 / 记忆归档 / 幂等复用。

覆盖契约 06 的离职交接路径（E2E 审计 2026-08-05 发现本文件为 0 字节空文件，
按测试团队建议补写真实用例）：
- 有记忆离职：生成交接文档 + 私有记忆归档(scope agent→archive) + 建引用型 handoff
- 空记忆离职：无文档、不建 handoff（避免误导 "(none)" 记录）
- 幂等：同一 agent 二次离职复用已有交接文档（H8）
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import handoff as handoff_module
from hiveweave.services.handoff import HandoffService
from hiveweave.services.memory import MemoryService

PROJECT_ID = "test-dismissal-handoff"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

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


async def _scope_counts(pid: str, agent_id: str) -> dict:
    rows = await handoff_module._query(
        pid,
        "SELECT scope, COUNT(*) AS n FROM memories WHERE agent_id = ? "
        "GROUP BY scope",
        [agent_id],
    )
    return {r["scope"]: int(r["n"]) for r in rows}


@pytest.mark.asyncio
async def test_dismissal_with_memories_generates_doc_and_archives(env):
    pid = env["project_id"]
    hs = HandoffService()
    mem = MemoryService()

    await mem.add_entry(agent_id="a1", project_id=pid,
                        content="签到模块契约要点", category="tool_written")
    await mem.add_entry(agent_id="a1", project_id=pid,
                        content="签到边界：重复签到去重", category="tool_written")

    r = await hs.create_dismissal_handoff(
        pid, "a1", agent_name="阿签", short_id="A001", role="签到工程师",
        parent_id="boss-1",
    )

    # 文档落盘且可读
    assert r["document_path"], "有记忆离职应生成交接文档"
    doc = Path(r["document_path"])
    assert doc.exists(), "交接文档应真实落盘"
    text = doc.read_text("utf-8")
    assert "签到模块契约要点" in text
    assert r["memory_count"] == 2
    assert r["archived"] == 2
    assert r["handoff_id"], "应建引用型 handoff"

    # 记忆全部归档：agent scope 清空，archive scope 持有
    counts = await _scope_counts(pid, "a1")
    assert counts.get("agent", 0) == 0
    assert counts.get("archive", 0) == 2

    # handoff 行 artifact_path 指向真实文档
    rows = await handoff_module._query(
        pid,
        "SELECT artifact_path, to_agent_id, status FROM handoffs WHERE id = ?",
        [r["handoff_id"]],
    )
    assert rows and rows[0]["artifact_path"] == r["document_path"]
    assert rows[0]["to_agent_id"] == "boss-1"
    assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_dismissal_without_memories_creates_nothing(env):
    pid = env["project_id"]
    hs = HandoffService()

    r = await hs.create_dismissal_handoff(
        pid, "ghost-1", agent_name="无名", short_id="G001",
        role="空记忆工程师", parent_id="boss-1",
    )

    # 无记忆：不生成文档、不建 handoff（H6：引用必须指向真实文档）
    assert not r["document_path"]
    assert r["memory_count"] == 0
    assert not r["handoff_id"]

    rows = await handoff_module._query(
        pid, "SELECT COUNT(*) AS n FROM handoffs WHERE from_agent_id = ?",
        ["ghost-1"],
    )
    assert int(rows[0]["n"]) == 0


@pytest.mark.asyncio
async def test_dismissal_is_idempotent_and_reuses_document(env):
    pid = env["project_id"]
    hs = HandoffService()
    mem = MemoryService()

    await mem.add_entry(agent_id="a9", project_id=pid,
                        content="幂等探针记忆", category="tool_written")

    r1 = await hs.create_dismissal_handoff(
        pid, "a9", agent_name="阿九", short_id="A009", role="幂等工程师",
        parent_id="boss-1",
    )
    assert r1["document_path"]

    # 二次离职：复用已有文档，不重复生成
    r2 = await hs.create_dismissal_handoff(
        pid, "a9", agent_name="阿九", short_id="A009", role="幂等工程师",
        parent_id="boss-1",
    )
    assert r2.get("reused") is True
    assert r2["document_path"] == r1["document_path"]
    assert r2["memory_count"] == 1  # 复用分支补查真实归档数（P3-1）

    # 仍只有一条 handoff 记录
    rows = await handoff_module._query(
        pid, "SELECT COUNT(*) AS n FROM handoffs WHERE from_agent_id = ?",
        ["a9"],
    )
    assert int(rows[0]["n"]) == 1
