"""Hire market gate uses last list_available_skills, not accumulated #N cache.

Also: hire worktree relocation notifies the new agent via inbox.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.skill_registry import SkillRegistryService
from hiveweave.tools.org_tools import (
    HireAgentParams,
    _hire_market_skill_gate,
    hire_agent_tool,
)


def test_hire_gate_accumulated_cache_ignored_when_last_search_empty():
    """累计旧市场 slug + 最近一次搜索无市场命中 → 只绑内置放行。"""
    err = _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=[],  # last search
        builtin_lookup=SkillRegistryService._get_builtin_skill,
    )
    assert err is None


def test_hire_gate_rejects_when_last_search_has_market():
    """最近一次搜索有市场技能，hire 只绑内置 → 拒绝。"""
    err = _hire_market_skill_gate(
        skills=["self-review"],
        seen_slugs=["vendor/s3-campaign"],
        builtin_lookup=SkillRegistryService._get_builtin_skill,
    )
    assert err is not None
    assert "last list_available_skills" in err
    assert "tighter keyword" in err
    assert "skip marketplace by not searching" in err


@pytest.mark.asyncio
async def test_list_available_skills_last_search_is_this_call_only(monkeypatch):
    svc = SkillRegistryService()
    aid = "hr-last"
    svc._skill_search_cache[aid] = ["vendor/s3-campaign"]
    svc._skill_search_last[aid] = ["vendor/s3-campaign"]

    async def no_market(search=None):
        return []

    monkeypatch.setattr(svc, "_search_skills_sh", no_market)
    monkeypatch.setattr(svc, "_search_skillhub", no_market)

    await svc.list_available_skills(search="self-review", agent_id=aid)
    assert "vendor/s3-campaign" in svc._skill_search_cache[aid]
    assert svc._skill_search_last.get(aid) == []


def _org_for_hire():
    org = MagicMock()
    org.list_agents = AsyncMock(
        return_value=[
            {
                "id": "ceo-1",
                "name": "归零",
                "role": "ceo",
                "permission_type": "coordinator",
                "status": "active",
                "short_id": "A001",
                "parent_id": None,
                "model_id": "m1",
            },
            {
                "id": "coord-1",
                "name": "云岫",
                "role": "前端架构师",
                "permission_type": "coordinator",
                "status": "active",
                "short_id": "A002",
                "parent_id": "ceo-1",
                "model_id": "m1",
            },
        ]
    )
    org.get_agent_by_role = AsyncMock(return_value={"id": "ceo-1"})
    org.create_agent = AsyncMock(
        return_value={
            "id": "new-1",
            "short_id": "A010",
            "name": "墨白",
        }
    )
    org.update_agent = AsyncMock()
    return org


def _skills(last: list[str], cache: list[str] | None = None):
    skills = MagicMock()
    skills.resolve_skill_ref = lambda _aid, sk: sk
    skills._get_builtin_skill = SkillRegistryService._get_builtin_skill
    skills._skill_search_cache = {"hr-1": list(cache or last)}
    skills._skill_search_last = {"hr-1": list(last)}
    skills._resolve_marketplace_skill = AsyncMock(return_value=(None, None))
    return skills


def _hire_patches(gwt_create=None, inbox=None):
    gwt = MagicMock()
    gwt.create = gwt_create or AsyncMock(
        return_value={"success": True, "path": r"D:\proj\.hiveweave\worktrees\A010"}
    )
    inbox_svc = inbox or MagicMock()
    if not hasattr(inbox_svc, "send_message") or not isinstance(
        inbox_svc.send_message, AsyncMock
    ):
        inbox_svc.send_message = AsyncMock(return_value={"id": "m1"})
    ms = MagicMock()
    ms.resolve_model = AsyncMock(
        return_value={"id": "model-1", "model_id": "m", "name": "x"}
    )
    return (
        patch("hiveweave.tools.org_tools.get_project_id", AsyncMock(return_value="p1")),
        patch("hiveweave.services.model.ModelService", return_value=ms),
        patch("hiveweave.tools.org_tools.meta_db.get_project_workspace",
              AsyncMock(return_value=r"D:\proj")),
        patch("hiveweave.services.git_worktree.GitWorktreeService", return_value=gwt),
        patch("hiveweave.services.inbox.InboxService", return_value=inbox_svc),
        patch(
            "hiveweave.agents.supervisor.agent_manager.start_agent",
            AsyncMock(return_value=None),
        ),
    )


@pytest.mark.asyncio
async def test_hire_agent_gate_uses_last_search_not_cache():
    """累计缓存有市场 slug，最近一次搜索无市场 → 只绑内置仍通过门槛。"""
    ctx = MagicMock()
    ctx.org = _org_for_hire()
    ctx.skills = _skills(last=[], cache=["vendor/s3-campaign"])
    params = HireAgentParams(
        name="墨白",
        role="签到排行榜工程师",
        permission_type="executor",
        parent_agent_id="coord-1",
        skills=["self-review"],
    )
    patches = _hire_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await hire_agent_tool(params, "hr-1", "/tmp", ctx=ctx)
    assert result.success is True
    ctx.org.create_agent.assert_awaited()


@pytest.mark.asyncio
async def test_hire_agent_gate_rejects_builtin_only_after_market_search():
    ctx = MagicMock()
    ctx.org = _org_for_hire()
    ctx.skills = _skills(last=["vendor/s3-campaign"], cache=["vendor/s3-campaign"])
    params = HireAgentParams(
        name="墨白",
        role="签到排行榜工程师",
        permission_type="executor",
        parent_agent_id="coord-1",
        skills=["self-review"],
    )
    patches = _hire_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await hire_agent_tool(params, "hr-1", "/tmp", ctx=ctx)
    assert result.success is False
    assert "last list_available_skills" in (result.error or "")
    ctx.org.create_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_hire_relocated_worktree_sends_inbox():
    ctx = MagicMock()
    ctx.org = _org_for_hire()
    ctx.skills = _skills(last=[])
    inbox = MagicMock()
    inbox.send_message = AsyncMock(return_value={"id": "m1"})
    gwt_create = AsyncMock(
        return_value={
            "success": True,
            "path": r"D:\proj\.hiveweave\worktrees\A010-b",
        }
    )
    params = HireAgentParams(
        name="潮汐",
        role="背包系统工程师",
        permission_type="executor",
        parent_agent_id="coord-1",
        skills=None,
    )
    patches = _hire_patches(gwt_create=gwt_create, inbox=inbox)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await hire_agent_tool(params, "hr-1", "/tmp", ctx=ctx)
    assert result.success is True
    inbox.send_message.assert_awaited()
    sent = inbox.send_message.await_args.kwargs
    assert sent["from_agent_id"] == "system"
    assert sent["to_agent_id"] == "new-1"
    assert "[WORKTREE RELOCATED]" in sent["message"]
    assert "A010-b" in sent["message"]
    assert r"D:\proj\.hiveweave\worktrees\A010-b" in sent["message"]


@pytest.mark.asyncio
async def test_hire_canonical_worktree_skips_relocation_inbox():
    ctx = MagicMock()
    ctx.org = _org_for_hire()
    ctx.skills = _skills(last=[])
    inbox = MagicMock()
    inbox.send_message = AsyncMock(return_value={"id": "m1"})
    params = HireAgentParams(
        name="潮汐",
        role="背包系统工程师",
        permission_type="executor",
        parent_agent_id="coord-1",
        skills=None,
    )
    patches = _hire_patches(inbox=inbox)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = await hire_agent_tool(params, "hr-1", "/tmp", ctx=ctx)
    assert result.success is True
    inbox.send_message.assert_not_awaited()
