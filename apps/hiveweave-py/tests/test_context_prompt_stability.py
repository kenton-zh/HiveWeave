"""T4.1 / T4.3 钉住测试（审计 P2-3：回归高风险点无直接测试）。

- T4.1：goals/org_directory **每轮无条件注入**（同 run 前缀字节稳定的
  前提），且 dirty 版本记录副作用保留（agents/trigger.py 的后台目标
  更新通知依赖；删掉 set_agent_*_version 调用会卡死该通知）。
- T4.3：``policy_required_kinds_label`` 与 verify_ids 的 kind 口径一致
  （soft → 空串；required → 排序逗号清单；未知 policy → fail-close 哨兵）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.attestation import policy_required_kinds_label


# ── T4.3 label 口径 ──────────────────────────────────────────────────────


def test_label_soft_policy_is_empty():
    assert policy_required_kinds_label("coordinator_review") == ""


def test_label_required_policy_lists_sorted_kinds():
    assert policy_required_kinds_label("generic_tests") == "test_run"
    assert (
        policy_required_kinds_label("code_audit_visual")
        == "browse_e2e,code_audit"
    )


def test_label_unknown_policy_fails_closed_with_sentinel():
    assert policy_required_kinds_label("no_such_policy") == "_unknown_policy"


def test_label_empty_policy_is_empty():
    assert policy_required_kinds_label("") == ""


# ── T4.1 每轮注入 + dirty 副作用 ─────────────────────────────────────────


def _bare_agent() -> object:
    """最小 Agent 替身（复用 test_slack_clone 的 __new__ 手法）。"""
    from hiveweave.agents.agent import Agent

    ag = Agent.__new__(Agent)
    ag.id = "agent-t41"
    ag.project_id = "proj-t41"
    ag.config = {"involvement_level": "medium", "bound_skills": "[]",
                 "role": "developer"}
    ag._memory = MagicMock()
    ag._memory.build_project_context = AsyncMock(return_value="")
    ag._org = MagicMock()
    ag._org.org_dirty = MagicMock(return_value=True)
    ag._org.build_org_directory = AsyncMock(return_value="[ORG DIRECTORY]")
    ag._org.get_org_version = MagicMock(return_value=7)
    ag._org.set_agent_org_version = AsyncMock()
    ag._get_workspace_path = AsyncMock(return_value="")
    return ag


@pytest.mark.asyncio
async def test_goals_injected_even_when_not_dirty():
    """核心断言：非 dirty 轮 goals 仍注入（每轮字节稳定的前提）。"""
    ag = _bare_agent()
    goals = {"objective": "做成", "keyResults": []}
    with patch("hiveweave.agents.agent.charter_service") as cs:
        cs.read_goals = AsyncMock(return_value=goals)
        cs.goals_dirty = MagicMock(return_value=False)  # 非 dirty！
        cs.read_charter = AsyncMock(return_value={})
        ctx = await ag._build_context_prompt(skip_handoffs=True)
    assert "做成" in ctx
    cs.read_goals.assert_awaited_once()


@pytest.mark.asyncio
async def test_dirty_side_effect_still_recorded():
    """dirty 时版本记录照常（trigger 的后台目标更新通知依赖）。"""
    ag = _bare_agent()
    with patch("hiveweave.agents.agent.charter_service") as cs:
        cs.read_goals = AsyncMock(return_value={"objective": "x"})
        cs.goals_dirty = MagicMock(return_value=True)
        cs.get_goals_version = MagicMock(return_value=9)
        cs.set_agent_goals_version = AsyncMock()
        cs.read_charter = AsyncMock(return_value={})
        await ag._build_context_prompt(skip_handoffs=True)
    cs.set_agent_goals_version.assert_awaited_once_with("agent-t41", 9)
    ag._org.set_agent_org_version.assert_awaited_once_with("agent-t41", 7)


@pytest.mark.asyncio
async def test_org_directory_injected_every_turn():
    ag = _bare_agent()
    with patch("hiveweave.agents.agent.charter_service") as cs:
        cs.read_goals = AsyncMock(return_value=None)
        cs.goals_dirty = MagicMock(return_value=False)
        cs.read_charter = AsyncMock(return_value={})
        ctx = await ag._build_context_prompt(skip_handoffs=True)
    assert "[ORG DIRECTORY]" in ctx
