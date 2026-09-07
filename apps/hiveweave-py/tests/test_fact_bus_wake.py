"""L3 事实总线完整形态 + 按事实唤醒回归（2026-09-08，repair-plan §L3）。

面：
1. publish → 内存环 + per-project ``facts`` 表持久化（平台级不落库）
2. ``wake_fact_waiters``：匹配 kind=fact 等待 → 清等待 + [FACT_OBSERVED] 信
3. ref 匹配语义（kind / subject 子串 / 大小写 / 裸通配拒绝）
4. FS 错误码分类学：read_file 缺席 → NOT_FOUND 码 + fs.absent 事实发布
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.services import fact_bus, fs_errors
from hiveweave.services.turn_result import WaitingOnItem
from hiveweave.services.wait_contract import (
    _fact_ref_matches,
    wait_contract_service,
    wake_fact_waiters,
)

PROJECT_ID = "fact-proj-0001"
AGENT_ID = "fact-ceo-0001"


@pytest.fixture(autouse=True)
async def _real_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        meta_db.app_settings,
        "meta_db_path",
        str(tmp_path / "meta" / "hiveweave.db"),
    )
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()
    yield
    await meta_db.close_meta_db()


@pytest.fixture(autouse=True)
def _clean_bus():
    fact_bus.reset_for_tests()
    yield
    fact_bus.reset_for_tests()


@pytest.fixture
async def seeded_project(tmp_path, _real_meta):
    """真实 Meta projects 行 + per-project DB + CEO agent 行 + 内存路由。"""
    ws = str(tmp_path / "ws")
    now = int(time.time() * 1000)
    await meta_db.execute(
        "INSERT INTO projects (id, name, workspace_path, created_at) "
        "VALUES (?, ?, ?, ?)",
        [PROJECT_ID, "fact-test", ws, now],
    )
    conn = await project_db.ensure_project_db(ws)
    cur = await conn.execute(
        "INSERT INTO agents (id, short_id, project_id, name, role, status, "
        "created_at) VALUES (?, 'FC', ?, 'FactCEO', 'ceo', 'active', ?)",
        [AGENT_ID, PROJECT_ID, now],
    )
    await cur.close()
    await conn.commit()
    # inbox 投递走 agent_id → 路由解析，测试内补注册（进程单例，先复位）
    from hiveweave.services.agent_router import AgentRoute, agent_router

    agent_router.reset_for_tests()
    agent_router.register(
        AgentRoute(
            agent_id=AGENT_ID,
            project_id=PROJECT_ID,
            workspace_path=ws,
            short_id="FC",
            name="FactCEO",
            role="ceo",
            status="active",
        )
    )
    project_db._agent_cache[AGENT_ID] = ws
    return ws


class TestFactBusPersistence:
    async def test_publish_persists_project_scoped_fact(self, seeded_project):
        fact_bus.publish(
            "fs.absent", "reports/T7.md", {"value": "absent"},
            project_id=PROJECT_ID,
        )
        await asyncio.sleep(0.05)  # 让落库 task 排空
        con = sqlite3.connect(str(Path(seeded_project) / ".hiveweave" / "data.db"))
        try:
            rows = con.execute(
                "SELECT kind, subject, project_id FROM facts"
            ).fetchall()
        finally:
            con.close()
        assert rows == [("fs.absent", "reports/T7.md", PROJECT_ID)]

    async def test_platform_scoped_fact_not_persisted(self, seeded_project):
        fact_bus.publish("note", "no-project", {})
        await asyncio.sleep(0.05)
        con = sqlite3.connect(str(Path(seeded_project) / ".hiveweave" / "data.db"))
        try:
            n = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            con.close()
        assert n == 0


class TestFactRefMatching:
    def test_kind_only_matches_any_subject(self):
        assert _fact_ref_matches("fs.absent", "fs.absent", "anything") is True

    def test_kind_mismatch_never_matches(self):
        assert _fact_ref_matches("merge_landed", "fs.absent", "x") is False

    def test_subject_substring_case_insensitive(self):
        assert (
            _fact_ref_matches("fs.absent:reports/T7", "fs.absent", "D:/x/REPORTS/t7.md")
            is True
        )

    def test_bare_wildcard_rejected(self):
        # 裸 '*' / 空 ref = 无界等待，按不匹配处理（防饿死隧道）
        assert _fact_ref_matches("*", "fs.absent", "x") is False
        assert _fact_ref_matches("", "fs.absent", "x") is False


class TestWakeFactWaiters:
    async def test_matching_fact_wakes_waiter(self, seeded_project):
        await wait_contract_service.replace_waits(
            PROJECT_ID,
            AGENT_ID,
            [WaitingOnItem(kind="fact", ref="fs.absent:reports/T7")],
            phase="waiting",
        )
        active = await wait_contract_service.list_all_active(PROJECT_ID)
        assert len(active) == 1

        woken = await wake_fact_waiters(
            PROJECT_ID, "fs.absent", "reports/T7.md", value="absent"
        )
        assert woken == [AGENT_ID]
        assert await wait_contract_service.list_all_active(PROJECT_ID) == []

        from hiveweave.services.inbox import InboxService

        msgs = await InboxService().get_pending_messages(AGENT_ID)
        assert any(
            "[FACT_OBSERVED]" in str(m.get("message") or m.get("content") or "")
            for m in msgs
        )

    async def test_non_matching_fact_leaves_wait_alone(self, seeded_project):
        await wait_contract_service.replace_waits(
            PROJECT_ID,
            AGENT_ID,
            [WaitingOnItem(kind="fact", ref="fs.absent:reports/T7")],
            phase="waiting",
        )
        woken = await wake_fact_waiters(PROJECT_ID, "merge_landed", "whatever")
        assert woken == []
        assert len(await wait_contract_service.list_all_active(PROJECT_ID)) == 1


class TestFsTaxonomy:
    async def test_read_missing_file_code_and_absent_fact(self, seeded_project):
        from hiveweave.tools.file import read_file

        result = await read_file("ghost.md", 0, 50, workspace_path=seeded_project)
        assert result["success"] is False
        assert result[fs_errors.ERROR_CODE_KEY] == fs_errors.NOT_FOUND
        assert any(
            f.kind == "fs.absent" and f.subject.endswith("ghost.md")
            for f in fact_bus.recent_facts()
        )

    async def test_classify_oserror_codes(self):
        assert fs_errors.classify_oserror(FileNotFoundError()) == fs_errors.NOT_FOUND
        assert (
            fs_errors.classify_oserror(PermissionError())
            == fs_errors.PERMISSION_DENIED
        )
        assert (
            fs_errors.classify_oserror(IsADirectoryError())
            == fs_errors.IS_A_DIRECTORY
        )
        assert (
            fs_errors.classify_oserror(OSError(20, "not a directory"))
            == fs_errors.NOT_A_DIRECTORY
        )
