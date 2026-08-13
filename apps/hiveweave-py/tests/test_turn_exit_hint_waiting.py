"""H2 regression: exit-contract hint must not mislead agents into
WAIT_WITHOUT_ASK traps.

slack-clone_03 实证：hint 在无待办时只写「仅需提交 commit_turn」，而出口门
（TEST11 #1a）要求 waiting_on 里的 agent 必须有 30 分钟内送达消息/未完结
ask 证据 → agent 按提示 commit(waiting) 撞门，_TURN_GATE_MAX=1 一次修复
机会耗尽即 blocked。修复：空分支补 WAIT_WITHOUT_ASK 规则说明 + phase 示例。
"""

from __future__ import annotations

import pytest

from hiveweave.services.turn_exit import build_exit_contract_hint


@pytest.mark.asyncio
async def test_hint_empty_branch_documents_wait_rule(monkeypatch):
    """无待办时 hint 必须包含 WAIT_WITHOUT_ASK 规则 + phase 用法。"""
    async def _no_asks(_agent_id):
        return []

    async def _no_obligations(self, _project_id, _agent_id):
        return []

    async def _not_dirty(_agent_id, _project_id):
        return False

    async def _no_ceo(_project_id, _agent_id):
        return []

    monkeypatch.setattr(
        "hiveweave.services.turn_exit._unreplied_ask_contracts", _no_asks
    )
    monkeypatch.setattr(
        "hiveweave.services.task.TaskService.get_actionable_obligations",
        _no_obligations,
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit._worktree_dirty_flag", _not_dirty
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit.ceo_project_pending_obligations", _no_ceo
    )

    hint = await build_exit_contract_hint("agent-1", "proj-1")
    assert "commit_turn" in hint
    # 修复点 1：不再只说「仅需提交 commit_turn」——必须带 waiting 规则
    assert "WAIT_WITHOUT_ASK" in hint
    assert "waiting_on" in hint
    # 修复点 2：带 phase 用法示例
    assert "done_slice" in hint
    assert "assistant 纯文本不是返回值" in hint


@pytest.mark.asyncio
async def test_hint_busy_branch_keeps_obligations(monkeypatch):
    """有义务时 hint 仍列出义务（原有行为不回归）。"""
    async def _no_asks(_agent_id):
        return []

    async def _some_obligations(self, project_id, agent_id):
        return [{"id": "abc12345-def0-0000-0000-000000000000", "title": "t"}]

    async def _not_dirty(_agent_id, _project_id):
        return False

    async def _no_ceo(_project_id, _agent_id):
        return []

    monkeypatch.setattr(
        "hiveweave.services.turn_exit._unreplied_ask_contracts", _no_asks
    )
    monkeypatch.setattr(
        "hiveweave.services.task.TaskService.get_actionable_obligations",
        _some_obligations,
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit._worktree_dirty_flag", _not_dirty
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit.ceo_project_pending_obligations", _no_ceo
    )

    hint = await build_exit_contract_hint("agent-1", "proj-1")
    assert "未完成义务" in hint
    assert "abc12345" in hint

