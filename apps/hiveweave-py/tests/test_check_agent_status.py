"""check_agent_status — restore from pre-migration Elixir/Node tool.

Regression: Python migration deleted Elixir/Node backends but never ported
``check_agent_status``. Prompts still told agents to call it, so CEO nagged
HR without being able to see busy / waiting_human.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.tools.base import list_tool_names
from hiveweave.tools.org_tools import (
    CheckAgentStatusParams,
    _format_live_badge,
    check_agent_status_tool,
)
from hiveweave.services.permission import PermissionService, _BASE_TOOLS
from hiveweave.llm.streamer import DOOM_LOOP_READONLY_TOOLS


class _FakeStatus:
    def __init__(self, value: str):
        self.value = value


def test_tool_is_registered():
    assert "check_agent_status" in list_tool_names()


def test_tool_in_base_permission_set():
    assert "check_agent_status" in _BASE_TOOLS


def test_tool_in_doom_readonly_set():
    assert "check_agent_status" in DOOM_LOOP_READONLY_TOOLS


def test_all_families_see_tool_in_listing():
    svc = PermissionService()
    for agent in (
        {"role": "ceo", "permission_type": "coordinator"},
        {"role": "hr", "permission_type": "coordinator"},
        {"role": "前端架构师", "permission_type": "coordinator"},
        {"role": "签到工程师", "permission_type": "executor", "permission_mode": "readwrite"},
    ):
        tools = svc.get_tools_for_agent(agent)
        assert "check_agent_status" in tools, f"missing for {agent}"


def test_format_badge_busy(monkeypatch):
    live = SimpleNamespace(
        status=_FakeStatus("processing"),
        disposition="runnable",
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: live,
    )
    badge = _format_live_badge("a1", "active")
    assert "working" in badge[1]


def test_format_badge_waiting_human(monkeypatch):
    live = SimpleNamespace(
        status=_FakeStatus("idle"),
        disposition="waiting_human",
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: live,
    )
    badge = _format_live_badge("a1", "active")
    assert badge[0] == "🟡"
    assert "waiting_human" in badge[1]
    assert "do NOT nag" in badge[1]


def test_format_badge_archived(monkeypatch):
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: None,
    )
    badge = _format_live_badge("a1", "archived")
    assert "archived" in badge[1]


@pytest.mark.asyncio
async def test_check_one_agent_by_name(monkeypatch):
    target = {
        "id": "uuid-hr",
        "name": "天线",
        "short_id": "A002",
        "role": "hr",
        "status": "active",
        "permission_type": "coordinator",
        "project_id": "proj-1",
    }
    org = MagicMock()
    org.get_agent = AsyncMock(return_value=target)
    ctx = SimpleNamespace(org=org)

    async def fake_project_id(_aid):
        return "proj-1"

    async def fake_resolve(project_id, name_or_id, org_service=None):
        assert name_or_id == "天线"
        return "uuid-hr"

    monkeypatch.setattr(
        "hiveweave.tools.org_tools.get_project_id", fake_project_id
    )
    monkeypatch.setattr(
        "hiveweave.tools.org_tools.resolve_agent_id", fake_resolve
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: SimpleNamespace(
            status=_FakeStatus("idle"),
            disposition="waiting_human",
            _message_queue=[],
        ),
    )
    monkeypatch.setattr(
        "hiveweave.tools.org_tools._unread_wake_count",
        AsyncMock(return_value=3),
    )

    result = await check_agent_status_tool(
        CheckAgentStatusParams(agentId="天线"),
        agent_id="ceo-1",
        workspace="",
        ctx=ctx,
    )
    assert result.success
    assert "天线" in result.output
    assert "waiting_human" in result.output
    assert "unread_wake=3" in result.output


@pytest.mark.asyncio
async def test_check_list_all(monkeypatch):
    agents = [
        {
            "id": "u1",
            "name": "归零",
            "short_id": "A001",
            "role": "ceo",
            "status": "active",
            "permission_type": "coordinator",
        },
        {
            "id": "u2",
            "name": "天线",
            "short_id": "A002",
            "role": "hr",
            "status": "active",
            "permission_type": "coordinator",
        },
    ]
    org = MagicMock()
    org.list_agents = AsyncMock(return_value=agents)
    ctx = SimpleNamespace(org=org)

    monkeypatch.setattr(
        "hiveweave.tools.org_tools.get_project_id",
        AsyncMock(return_value="proj-1"),
    )
    monkeypatch.setattr(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        lambda _aid: None,
    )
    monkeypatch.setattr(
        "hiveweave.tools.org_tools._unread_wake_count",
        AsyncMock(return_value=0),
    )

    result = await check_agent_status_tool(
        CheckAgentStatusParams(),
        agent_id="ceo-1",
        workspace="",
        ctx=ctx,
    )
    assert result.success
    assert "2 agents" in result.output
    assert "归零" in result.output
    assert "天线" in result.output
