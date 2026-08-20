"""模型身份回归：agents.model_id 存 UUID（而非可重复的名称）。

背景：global_settings 的 tier 配置存 UUID，但 _seed_default_agents / hire_agent
曾把 resolve_model 结果降级为 model_id 名称写入 agents.model_id。同名多渠道
（如 deepseek-v4-flash × 4 行）时 get() 无排序返回物理首行，CEO/HR 实际
用错渠道。修复：写路径存 UUID；读路径名称查询加确定性排序兜底存量。
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.api.projects import _seed_default_agents
from hiveweave.services.model import ModelService
from hiveweave.services.org import OrgService

_COLS = (
    "id, name, model_id, base_url, api_key, provider_type, context_window, "
    "max_output_tokens, supports_thinking, thinking_format, default_reasoning_effort, "
    "temperature, is_active, fallback, tier, created_at, updated_at"
)


def _mk_row(
    uid: str, model_id: str, active: int, updated_at: int, name: str | None = None
) -> tuple:
    name = name or f"name-{uid}"
    return (
        uid, name, model_id, "https://example.test/v3", f"key-{uid}",
        "openai-compatible", 1000, 500, 0, "", None, None, active, None, None,
        1, updated_at,
    )


@pytest.mark.asyncio
async def test_get_by_name_deterministic_latest_active():
    """同名多渠道：名称查询必须返回最近更新的活跃行，而非物理顺序第一行。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"CREATE TABLE llm_models ({_COLS})")
        conn.executemany(
            f"INSERT INTO llm_models ({_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                _mk_row("uuid-old-active", "deepseek-v4-flash", 1, 100),
                _mk_row("uuid-new-active", "deepseek-v4-flash", 1, 300),
                _mk_row("uuid-new-inactive", "deepseek-v4-flash", 0, 400),
            ],
        )
        conn.commit()

        def _run(sql: str, params: list) -> tuple | None:
            return conn.execute(sql, params).fetchone()

        ms = ModelService()
        with patch("hiveweave.services.model.meta_db") as meta:
            meta.query_one = AsyncMock(side_effect=_run)
            row = await ms.get("deepseek-v4-flash")
        assert row is not None
        assert row["id"] == "uuid-new-active"

        with patch("hiveweave.services.model.meta_db") as meta:
            meta.query_one = AsyncMock(side_effect=_run)
            exact = await ms.get("uuid-old-active")
        assert exact is not None
        assert exact["id"] == "uuid-old-active"

        with patch("hiveweave.services.model.meta_db") as meta:
            meta.query_one = AsyncMock(side_effect=_run)
            by_name = await ms.find_by_name("name-uuid-new-active")
        assert by_name is not None
        assert by_name["id"] == "uuid-new-active"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_get_by_name_tie_breaks_by_id():
    """is_active + updated_at 平局时按 id 倒序兜底，保证完全确定性。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"CREATE TABLE llm_models ({_COLS})")
        conn.executemany(
            f"INSERT INTO llm_models ({_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                _mk_row("uuid-a", "deepseek-v4-flash", 1, 100, name="平局名称"),
                _mk_row("uuid-b", "deepseek-v4-flash", 1, 100, name="平局名称"),
            ],
        )
        conn.commit()

        ms = ModelService()
        with patch("hiveweave.services.model.meta_db") as meta:
            meta.query_one = AsyncMock(
                side_effect=lambda sql, params: conn.execute(sql, params).fetchone()
            )
            row = await ms.get("deepseek-v4-flash")
        assert row is not None
        assert row["id"] == "uuid-b"

        with patch("hiveweave.services.model.meta_db") as meta:
            meta.query_one = AsyncMock(
                side_effect=lambda sql, params: conn.execute(sql, params).fetchone()
            )
            by_name = await ms.find_by_name("平局名称")
        assert by_name is not None
        assert by_name["id"] == "uuid-b"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_seed_default_agents_stores_uuid_not_name():
    """seed 必须存 tier 模型的 UUID；CEO/HR 不能绑定到可重复的名称。"""
    mgmt_row = {
        "id": "uuid-mgmt", "name": "官方DS", "model_id": "deepseek-v4-flash",
        "api_key": "sk-mgmt",
    }
    exec_row = {
        "id": "uuid-exec", "name": "ARK Coding", "model_id": "deepseek-v4-flash",
        "api_key": "sk-exec",
    }

    async def fake_resolve(*, tier, skip_model_ids=None, prefer_latest=False):
        return mgmt_row if tier == "management" else exec_row

    created_models: list[str] = []

    async def fake_create(attrs, bootstrap=False):
        created_models.append(attrs["model_id"])
        return {"id": f"id-{attrs['role']}", "model_id": attrs["model_id"]}

    fake_cursor = AsyncMock()
    fake_cursor.fetchall = AsyncMock(return_value=[])
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=fake_cursor)
    fake_conn.commit = AsyncMock()

    with (
        patch(
            "hiveweave.api.projects.project_db.get_project_db_by_project_id",
            new=AsyncMock(return_value=fake_conn),
        ),
        patch.object(
            OrgService, "list_agents", new=AsyncMock(return_value=[])
        ),
        patch.object(ModelService, "resolve_model", side_effect=fake_resolve),
        patch.object(ModelService, "list_active", new=AsyncMock(return_value=[])),
        patch.object(OrgService, "create_agent", side_effect=fake_create),
    ):
        await _seed_default_agents("proj-1")

    assert created_models == ["uuid-mgmt", "uuid-exec"], created_models
