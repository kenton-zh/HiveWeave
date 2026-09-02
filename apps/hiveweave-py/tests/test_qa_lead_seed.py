"""QA 主管（qa_lead）种子与剧本测试——用户钦定的验收权独立设计。

MVP 语义：
- 项目创建 seed 三个初始角色：CEO 归零、HR 天线、QA 主管 验真（coordinator
  权 → staffing/dispatch 可招叶子 QA）
- qa_lead 的身份提示词 = coordinator 剧本 + QA_LEAD_BLOCK（契约驱动验收纪律）
- 白名单：coordinator 写前缀含 tests/（QA 探针写入位）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.api.projects import _seed_default_agents
from hiveweave.services.model import ModelService
from hiveweave.services.org import OrgService


@pytest.mark.asyncio
async def test_seed_creates_qa_lead_with_contract_driven_mission():
    mgmt_row = {"id": "uuid-mgmt", "name": "官方DS", "model_id": "m"}
    exec_row = {"id": "uuid-exec", "name": "ARK Coding", "model_id": "e"}

    async def fake_resolve(*, tier, skip_model_ids=None, prefer_latest=False):
        return mgmt_row if tier == "management" else exec_row

    created: list[dict] = []

    async def fake_create(attrs, bootstrap=False):
        created.append(attrs)
        assert bootstrap is True, "种子创建必须跳过 hire invariants"
        return {"id": f"id-{attrs['role']}", "model_id": attrs.get("model_id")}

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
        patch.object(OrgService, "list_agents", new=AsyncMock(return_value=[])),
        patch.object(ModelService, "resolve_model", side_effect=fake_resolve),
        patch.object(ModelService, "list_active", new=AsyncMock(return_value=[])),
        patch.object(OrgService, "create_agent", side_effect=fake_create),
    ):
        ids = await _seed_default_agents("proj-qa")

    by_role = {a["role"]: a for a in created}
    for role in ("ceo", "hr", "qa_lead"):
        assert role in by_role, f"缺 {role} 种子: {by_role}"

    qa = by_role["qa_lead"]
    assert qa["name"] == "验真"
    assert qa["permission_type"] == "coordinator", "QA 主管需要 staffing/dispatch 权招叶子 QA"
    assert "task-advance" in qa["skills"]
    # 契约驱动使命写在 goal 里（prompt 经由 goal 注入 "## Your Role"）
    assert "装配级" in qa["goal"]
    assert "禁止以直调内部函数" in qa["goal"]


def test_qa_lead_prompt_block_injected_for_qa_role_only():
    from hiveweave.prompts.identity import build_identity_prompt
    from hiveweave.prompts.qa_lead import QA_LEAD_BLOCK

    qa_prompt = build_identity_prompt(
        role="qa_lead", role_type="coordinator",
        permission_type="coordinator", name="验真",
        goal="契约驱动验收", backstory="",
    )
    assert "契约驱动验收" in qa_prompt
    assert QA_LEAD_BLOCK in qa_prompt
    assert "禁止直调内部函数" in qa_prompt

    # 非 qa_lead 的 coordinator 不带该块
    other = build_identity_prompt(
        role="后端架构师", role_type="coordinator",
        permission_type="coordinator", name="某架构", goal="", backstory="",
    )
    assert QA_LEAD_BLOCK not in other
