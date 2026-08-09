"""收养已存在项目数据 — 跨机器搬迁/整目录拷贝场景回归测试.

背景: 从别的机器拷来的工作区自带 .hiveweave/data.db（完整的 agents/聊天/
记忆/任务）。在本机"创建项目"选中该目录时，正确行为是**收养**其原有状态
（沿用旧 project_id、保留全部数据、不删库），而不是清掉重建"新项目"。

测试面:
1. _probe_existing_project_id — "可收养"判定纯函数
2. create_project 收养路径 — 保留数据 + 沿用旧 project_id + adopted=true
3. 收养 id 与本机冲突时 — 迁移到新 id 的兜底路径
4. 无可收养数据时 — 维持"残留清理 + 全新初始化"旧行为
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.api.projects import ProjectCreate, _probe_existing_project_id, create_project
from hiveweave.db import meta as meta_module
from hiveweave.db import project as project_db

OLD_ID = "adopt-old-project-0001"
OLD_CEO = "adopt-old-ceo-0001"
OLD_HR = "adopt-old-hr-0001"


def _make_copied_workspace(ws: Path) -> None:
    """构造"从别的机器拷贝来的"工作区：.hiveweave/data.db 带完整旧数据。"""
    from hiveweave.db.schema import PROJECT_DB_TABLES

    hw = ws / ".hiveweave"
    hw.mkdir(parents=True, exist_ok=True)
    # 用真实 DDL 建表（含 short_id 等全部列，避免 ensure_project_db 的索引创建失败）
    ddl = ";\n".join(
        s
        for s in PROJECT_DB_TABLES
        if any(
            f"CREATE TABLE IF NOT EXISTS {t}" in s
            for t in ("agents", "project_meta", "chat_messages", "tasks")
        )
    )
    con = sqlite3.connect(hw / "data.db")
    con.executescript(ddl)
    con.execute(
        "INSERT INTO agents (id, project_id, name, role, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', 1)",
        [OLD_CEO, OLD_ID, "slack-ceo", "ceo"],
    )
    con.execute(
        "INSERT INTO agents (id, project_id, name, role, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', 1)",
        [OLD_HR, OLD_ID, "slack-hr", "hr"],
    )
    con.execute(
        "INSERT INTO project_meta (project_id, description, org_paradigm, "
        "charter_json, goals_json, language, game_time_accumulated_seconds, updated_at) "
        "VALUES (?, ?, 'solo', ?, '[]', 'zh', 3600, 1)",
        [OLD_ID, "老项目描述", '{"vision": "old-vision"}'],
    )
    con.execute(
        "INSERT INTO chat_messages (id, agent_id, role, content, created_at) "
        "VALUES (?, ?, 'user', ?, 1)",
        ["chat-1", OLD_CEO, "老机器上的聊天记录"],
    )
    con.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        ["task-1", OLD_ID, "老任务", OLD_CEO],
    )
    con.commit()
    con.close()


class _FakeMeta:
    """内存版 Meta DB — 只实现 create_project 用到的几个方法."""

    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}
        self.keys: dict[str, str] = {}  # meta_index (墓碑)
        self.executed: list[tuple[str, list | None]] = []
        # 收养冲突模拟: 命中该旧 id 时返回已有行
        self.conflicting_id: str | None = None

    async def query(self, sql: str, params: list | None = None) -> list:
        # 唯一性检查: 本机无任何现存项目
        return []

    async def query_one(self, sql: str, params: list | None = None):
        if "WHERE key = ?" in sql and params:
            val = self.keys.get(params[0])
            return {"value": val} if val is not None else None
        if "WHERE id = ?" in sql and params:
            if self.conflicting_id == params[0]:
                return {"id": params[0], "name": "conflict", "workspace_path": "/elsewhere"}
            return self.projects.get(params[0])
        return None

    async def execute(self, sql: str, params: list | None = None) -> None:
        self.executed.append((sql, params or []))
        stripped = sql.strip()
        if stripped.startswith("INSERT INTO projects"):
            self.projects[params[0]] = {
                "id": params[0],
                "name": params[1],
                "workspace_path": params[2],
                "is_started": 1,
                "created_at": params[3],
            }
        elif stripped.startswith("INSERT OR REPLACE INTO meta_index"):
            self.keys[params[0]] = str(params[1])
        elif stripped.startswith("DELETE FROM meta_index"):
            self.keys.pop(params[0], None)
        elif stripped.startswith(("DELETE", "UPDATE")):
            pass


@pytest.fixture
async def fake_meta():
    fake = _FakeMeta()
    with (
        patch.object(meta_module, "query", fake.query),
        patch.object(meta_module, "query_one", fake.query_one),
        patch.object(meta_module, "execute", fake.execute),
        patch("hiveweave.api.projects._seed_default_agents", new=AsyncMock(return_value=[OLD_CEO])),
        patch(
            "hiveweave.api.projects.GitWorktreeService",
            return_value=MagicMock(
                ensure_git_repo=AsyncMock(return_value={"success": True, "initialized": False})
            ),
        ),
        patch("hiveweave.api.projects.GameTimeService", return_value=MagicMock(start=AsyncMock())),
        patch(
            "hiveweave.agents.supervisor.agent_manager.start_project_agents",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield fake


# ── 1) _probe_existing_project_id 判定 ──────────────────────


class TestProbeExistingProjectId:
    async def test_no_hiveweave_dir_returns_none(self, tmp_path: Path):
        assert _probe_existing_project_id(tmp_path) is None

    async def test_empty_hiveweave_dir_returns_none(self, tmp_path: Path):
        (tmp_path / ".hiveweave").mkdir()
        assert _probe_existing_project_id(tmp_path) is None

    async def test_db_without_agents_table_returns_none(self, tmp_path: Path):
        hw = tmp_path / ".hiveweave"
        hw.mkdir()
        con = sqlite3.connect(hw / "data.db")
        con.execute("CREATE TABLE unrelated (id TEXT)")
        con.commit()
        con.close()
        assert _probe_existing_project_id(tmp_path) is None

    async def test_full_copied_workspace_returns_project_id(self, tmp_path: Path):
        ws = tmp_path / "copied"
        ws.mkdir()
        _make_copied_workspace(ws)
        assert _probe_existing_project_id(ws) == OLD_ID

    async def test_empty_project_id_is_not_adoptable(self, tmp_path: Path):
        """project_id 全为空的库不可收养（防误删边界: 返回 None 走清理）。"""
        ws = tmp_path / "empty"
        ws.mkdir()
        hw = ws / ".hiveweave"
        hw.mkdir()
        con = sqlite3.connect(hw / "data.db")
        con.execute(
            "CREATE TABLE agents (id TEXT, project_id TEXT NOT NULL, name TEXT)"
        )
        con.execute("INSERT INTO agents VALUES ('a1', '', 'empty-name')")
        con.commit()
        con.close()
        assert _probe_existing_project_id(ws) is None

    async def test_probe_read_failure_raises_instead_of_wipe(self, tmp_path: Path):
        """data.db 无法读取（损坏/非 sqlite）时抛异常——调用方中止而非删除。"""
        ws = tmp_path / "corrupt"
        ws.mkdir()
        hw = ws / ".hiveweave"
        hw.mkdir()
        (hw / "data.db").write_bytes(b"not a sqlite database at all")

        with pytest.raises(Exception):
            _probe_existing_project_id(ws)

        # 损坏文件未被删除
        assert (hw / "data.db").exists()


# ── 2) 收养路径 ───────────────────────────────────────────────


class TestAdoptPath:
    async def test_create_adopts_copied_workspace(self, tmp_path, fake_meta):
        ws = tmp_path / "copied"
        ws.mkdir()
        _make_copied_workspace(ws)

        resp = await create_project(
            ProjectCreate(name="slack-clone", workspacePath=str(ws))
        )

        assert resp["adopted"] is True
        assert resp["project"]["id"] == OLD_ID

        # .hiveweave 未被删除，data.db 仍在
        assert (ws / ".hiveweave" / "data.db").exists()

        # 原数据全部保留: agents / chat / project_meta
        con = sqlite3.connect(ws / ".hiveweave" / "data.db")
        try:
            agents = con.execute("SELECT id, role FROM agents ORDER BY id").fetchall()
            assert [a[0] for a in agents] == [OLD_CEO, OLD_HR]
            row = con.execute(
                "SELECT project_id, charter_json FROM project_meta LIMIT 1"
            ).fetchone()
            assert row == (OLD_ID, '{"vision": "old-vision"}')
            assert con.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 1
        finally:
            con.close()

        # Meta 登记用的就是旧 project_id（未生成新 UUID）
        assert fake_meta.projects.keys() == {OLD_ID}

    async def test_conflicting_old_id_forks_to_new_id(self, tmp_path, fake_meta):
        ws = tmp_path / "copied"
        ws.mkdir()
        _make_copied_workspace(ws)
        fake_meta.conflicting_id = OLD_ID

        resp = await create_project(
            ProjectCreate(name="slack-clone", workspacePath=str(ws))
        )

        assert resp["adopted"] is True
        new_id = resp["project"]["id"]
        assert new_id != OLD_ID
        assert new_id in fake_meta.projects

        # 工作区内所有表的 project_id 已迁移到新 id
        con = sqlite3.connect(ws / ".hiveweave" / "data.db")
        try:
            for table in ("agents", "project_meta", "tasks"):
                pid = con.execute(
                    f"SELECT project_id FROM {table} LIMIT 1"
                ).fetchone()[0]
                assert pid == new_id
        finally:
            con.close()

    async def test_purged_workspace_tombstone_blocks_adopt(self, tmp_path, fake_meta):
        """本机删除过该 workspace（墓碑）→ 同路径重建为全新项目，不收养。"""
        ws = tmp_path / "purged"
        ws.mkdir()
        _make_copied_workspace(ws)
        fake_meta.keys[f"purged_ws:{str(ws).replace(chr(92), '/').lower()}"] = "1"

        resp = await create_project(
            ProjectCreate(name="slack-clone", workspacePath=str(ws))
        )

        assert resp["adopted"] is False
        assert resp["project"]["id"] != OLD_ID
        # 墓碑被消费（新项目已是该路径的合法主人）
        assert fake_meta.keys == {}
        # 旧数据被清理（走"残留清理 + 全新初始化"旧语义）
        con = sqlite3.connect(ws / ".hiveweave" / "data.db")
        try:
            assert con.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
        finally:
            con.close()

    async def test_unreadable_data_db_aborts_create_without_wipe(
        self, tmp_path, fake_meta
    ):
        """data.db 损坏时创建被中止（HTTPException），原文件保留不受破坏。"""
        from fastapi import HTTPException

        ws = tmp_path / "corrupt"
        ws.mkdir()
        hw = ws / ".hiveweave"
        hw.mkdir()
        (hw / "data.db").write_bytes(b"not a sqlite database at all")

        with pytest.raises(HTTPException) as exc_info:
            await create_project(
                ProjectCreate(name="corrupt", workspacePath=str(ws))
            )
        assert exc_info.value.status_code == 500
        assert (hw / "data.db").exists()


# ── 3) 无可收养残留 → 维持旧行为 ──────────────────────────────


class TestNonAdoptableCleanup:
    async def test_residual_hwdir_is_cleaned_and_fresh_project_created(
        self, tmp_path, fake_meta
    ):
        ws = tmp_path / "fresh"
        ws.mkdir()
        # 残留 .hiveweave（无 data.db）— 旧"删除项目失败残留"场景
        hw = ws / ".hiveweave"
        hw.mkdir()
        (hw / "leftover.txt").write_text("old")

        resp = await create_project(
            ProjectCreate(name="fresh", workspacePath=str(ws))
        )

        assert resp["adopted"] is False
        assert resp["project"]["id"] != OLD_ID
        # 残留文件被清理，仅新系统文件
        remaining = {p.name for p in (ws / ".hiveweave").iterdir()}
        assert {"data.db", "shared"} <= remaining
        assert "leftover.txt" not in remaining