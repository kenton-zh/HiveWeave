"""BUG-ORG-01 回归：create_agent 入职即预置人事档案（personnel_records）。

E2E 审计（2026-08-05，org 功能测试）发现 read_roster 对全员回落
"(no roster record yet — basic info from agents table)"（P1）——roster 表
从未被写入，只有 HR 手工 update_roster 才落记录。修复后 create_agent 成功
即预置一行（position=role / responsibilities=goal / hire_date=今天），
HR 后续 update_roster 仍按 upsert 覆盖。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.agent_router import agent_router
from hiveweave.services.org import OrgService
from hiveweave.services.roster import RosterService


@pytest.fixture
async def org_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())
        pid = "roster-seed-proj"

        async def fake_ws(p: str):
            return ws if p == pid else None

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": pid, "workspace": ws}

        agent_router.clear_project(pid)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_create_agent_seeds_roster_record(org_env):
    pid = org_env["project_id"]
    org = OrgService()

    agent = await org.create_agent(
        {
            "project_id": pid,
            "name": "归零",
            "role": "ceo",
            "goal": "掌控项目全局，交付项目目标。",
            "permission_type": "coordinator",
            "status": "active",
        },
        bootstrap=True,
    )

    rec = await RosterService().get(pid, agent["id"])
    assert rec is not None, "入职应自动预置 personnel_records 行"
    assert rec["position"] == "ceo"
    assert rec["responsibilities"] == "掌控项目全局，交付项目目标。"
    assert rec["status"] == "active"
    assert rec["hire_date"], "hire_date 应预置为当天"
    assert rec["updated_by"] == "system"


@pytest.mark.asyncio
async def test_hr_update_roster_still_overwrites_seed(org_env):
    """HR 手工 update_roster（DELETE+INSERT upsert）覆盖预置行，语义不变。"""
    pid = org_env["project_id"]
    org = OrgService()
    rs = RosterService()

    agent = await org.create_agent(
        {
            "project_id": pid,
            "name": "阿灿",
            "role": "backend-engineer",
            "goal": "交付 API。",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )
    assert await rs.get(pid, agent["id"]) is not None

    await rs.update_roster(pid, agent["id"], {
        "position": "后端工程师",
        "department": "工程部",
        "responsibilities": "API 与数据库",
        "updated_by": "hr-test",
    })

    rec = await rs.get(pid, agent["id"])
    assert rec["position"] == "后端工程师"
    assert rec["department"] == "工程部"
    assert rec["responsibilities"] == "API 与数据库"
    assert rec["updated_by"] == "hr-test"

    # upsert 语义：仍只有一条记录
    rows = await rs.list_by_project(pid)
    assert len([r for r in rows if r["agent_id"] == agent["id"]]) == 1


@pytest.mark.asyncio
async def test_seeded_roster_shows_in_get_roster_without_fallback(org_env):
    """read_roster 不再出现 '(no roster record yet)' 回落标注。"""
    pid = org_env["project_id"]
    org = OrgService()

    await org.create_agent(
        {
            "project_id": pid,
            "name": "小查",
            "role": "qa-engineer",
            "goal": "守住质量底线。",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )

    roster_text = await RosterService().get_roster(pid)
    assert "小查" in roster_text
    assert "qa-engineer" in roster_text
    assert "(no roster record yet" not in roster_text
