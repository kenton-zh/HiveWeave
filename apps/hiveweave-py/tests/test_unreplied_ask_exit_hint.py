"""UNREPLIED_ASKS hint includes ask body/sender; trigger digest keeps ghost asks."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents import trigger as trigger_module
from hiveweave.services.turn_exit import _build_gate_hint, build_exit_contract_hint


@pytest.mark.asyncio
async def test_exit_hint_includes_ask_body_snippet(monkeypatch):
    async def fake_asks(_agent_id):
        return [
            {
                "contract": "deadbeef0123",
                "from_agent_id": "sender-uuid-aaaa",
                "from_name": "柚子",
                "snippet": "Please confirm the milestone scope for login",
            }
        ]

    async def _none(*_a, **_k):
        return [] if _a else []

    async def _no_obligations(self, _project_id, _agent_id):
        return []

    async def _not_dirty(_agent_id, _project_id):
        return {"dirty": False, "files": [], "path": "", "git_error": None}

    monkeypatch.setattr(
        "hiveweave.services.turn_exit._unreplied_ask_contracts", fake_asks
    )
    monkeypatch.setattr(
        "hiveweave.services.task.TaskService.get_actionable_obligations",
        _no_obligations,
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit._worktree_dirty_flag", _not_dirty
    )
    monkeypatch.setattr(
        "hiveweave.services.turn_exit.ceo_project_pending_obligations",
        AsyncMock(return_value=[]),
    )

    hint = await build_exit_contract_hint("agent-1", "proj-1")
    assert "柚子" in hint
    assert "sender-u" in hint or "sender-uuid" in hint
    assert "Please confirm the milestone scope for login" in hint
    assert "deadbeef0123" in hint


@pytest.mark.asyncio
async def test_unreplied_ask_contracts_uses_query_snippet(monkeypatch):
    """Mock the outstanding-ask query: hint path sees body + sender."""
    rows = [
        {
            "id": "msg-1",
            "from_agent_id": "sender-uuid-aaaa",
            "to_agent_id": "agent-1",
            "message": "Please confirm the milestone scope for login now",
            "read": 1,
            "created_at": 1,
            "message_type": "ask",
            "expect_report": 1,
            "priority": "normal",
            "task_id": None,
            "wake": 1,
            "parked": 0,
            "wake_category": None,
            "triage_batch_id": None,
            "reply_contract_id": "deadbeef01234567",
            "reply_to": None,
            "snippet": "Please confirm the milestone scope for login now",
        }
    ]

    class _Row(dict):
        def keys(self):
            return dict.keys(self)

    async def fake_query(_agent_id, sql, params=None):
        if "FROM inbox" in sql or "from inbox" in sql.lower():
            return [_Row(rows[0])]
        if "FROM agents" in sql:
            return [_Row({"id": "sender-uuid-aaaa", "name": "柚子"})]
        return []

    monkeypatch.setattr(
        "hiveweave.services.inbox._ensure_schema", AsyncMock()
    )
    monkeypatch.setattr("hiveweave.db.project.query", fake_query)

    from hiveweave.services.inbox import InboxService
    from hiveweave.services.turn_exit import _unreplied_ask_contracts

    msgs = await InboxService().get_outstanding_ask_messages("agent-1")
    assert msgs
    assert "Please confirm" in (msgs[0].get("snippet") or msgs[0].get("message") or "")
    assert msgs[0].get("from_name") == "柚子"

    contracts = await _unreplied_ask_contracts("agent-1")
    assert contracts[0]["snippet"].startswith("Please confirm")
    assert contracts[0]["from_name"] == "柚子"
    assert contracts[0]["contract"] == "deadbeef0123"


def test_gate_empty_body_points_at_contract():
    hint = _build_gate_hint(
        ["UNREPLIED_ASKS"],
        [{
            "from_name": "知远",
            "message": "   ",
            "reply_contract_id": "contract-id-xx",
        }],
        None,
    )
    assert "body not in this turn" in hint
    assert "replyTo=contract-id" in hint


@pytest.mark.asyncio
async def test_trigger_digest_includes_read_outstanding_ask(monkeypatch):
    pending = [{
        "id": "unread-1",
        "from_agent_id": "peer-a",
        "message": "new ping",
        "expect_report": False,
        "created_at": 10,
        "reply_contract_id": None,
    }]
    ghost = [{
        "id": "ghost-1",
        "from_agent_id": "peer-b",
        "from_name": "柚子",
        "message": "still waiting on the login milestone",
        "expect_report": True,
        "read": True,
        "created_at": 5,
        "reply_contract_id": "ghost-contract-1",
    }]

    async def fake_pending(_aid):
        return list(pending)

    async def fake_outstanding(_aid, **_k):
        return list(ghost)

    async def empty(*_a, **_k):
        return []

    async def fake_name(aid):
        return {"peer-a": "A", "peer-b": "柚子"}.get(aid, aid)

    monkeypatch.setattr(trigger_module, "_get_agent_manager", lambda: None)
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_pending_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_accepted_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._handoff_service,
        "get_unreported_accepted_handoffs",
        empty,
    )
    monkeypatch.setattr(
        trigger_module._handoff_service, "mark_delivered", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_pending_messages", fake_pending
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_undelivered_background", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service,
        "get_outstanding_ask_messages",
        fake_outstanding,
    )
    monkeypatch.setattr(trigger_module, "_agent_name", fake_name)

    with patch(
        "hiveweave.services.charter.charter_service.goals_dirty",
        return_value=False,
    ):
        result = await trigger_module.build_trigger_context(
            {"id": "agent-1", "project_id": "p1", "name": "Me"},
            "subordinate",
        )
    assert result is not None
    context, inbox_msg_ids, _from, _cat = result
    assert "still waiting on the login milestone" in context
    assert "ghost-contract-1" in context
    assert "unread-1" in inbox_msg_ids
    assert "ghost-1" not in inbox_msg_ids


def test_format_unreplied_ask_reject_suffix_includes_body_and_contract():
    from hiveweave.services.turn_exit import format_unreplied_ask_reject_suffix

    text = format_unreplied_ask_reject_suffix([
        {
            "from_name": "柚子",
            "from_agent_id": "sender-uuid-aaaa",
            "contract": "deadbeef0123",
            "snippet": "Please confirm the milestone scope for login",
        }
    ])
    assert "柚子" in text
    assert "Please confirm the milestone scope for login" in text
    assert "deadbeef0123" in text
    assert format_unreplied_ask_reject_suffix([]) == ""


@pytest.mark.asyncio
async def test_trigger_complete_wakes_on_ghost_ask(monkeypatch):
    """complete + read=1 outstanding ask is still actionable (not background-only)."""
    from types import SimpleNamespace

    ghost = [{
        "id": "ghost-1",
        "from_agent_id": "peer-b",
        "from_name": "柚子",
        "message": "still waiting on the login milestone",
        "expect_report": True,
        "read": True,
        "created_at": 5,
        "reply_contract_id": "ghost-contract-1",
    }]

    async def empty(*_a, **_k):
        return []

    async def fake_outstanding(_aid, **_k):
        return list(ghost)

    async def fake_name(aid):
        return {"peer-b": "柚子"}.get(aid, aid)

    class _Mgr:
        def get_agent(self, _aid):
            return SimpleNamespace(disposition="complete")

    monkeypatch.setattr(trigger_module, "_get_agent_manager", lambda: _Mgr())
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_pending_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_accepted_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._handoff_service,
        "get_unreported_accepted_handoffs",
        empty,
    )
    monkeypatch.setattr(
        trigger_module._handoff_service, "mark_delivered", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_pending_messages", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_undelivered_background", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service,
        "get_outstanding_ask_messages",
        fake_outstanding,
    )
    monkeypatch.setattr(trigger_module, "_agent_name", fake_name)

    with patch(
        "hiveweave.services.charter.charter_service.goals_dirty",
        return_value=False,
    ):
        result = await trigger_module.build_trigger_context(
            {"id": "agent-1", "project_id": "p1", "name": "Me"},
            "subordinate",
        )
    assert result is not None
    context, inbox_msg_ids, _from, _cat = result
    assert "still waiting on the login milestone" in context
    assert "ghost-1" not in inbox_msg_ids


@pytest.mark.asyncio
async def test_trigger_complete_skips_without_ghost_ask(monkeypatch):
    from types import SimpleNamespace

    async def empty(*_a, **_k):
        return []

    class _Mgr:
        def get_agent(self, _aid):
            return SimpleNamespace(disposition="complete")

    monkeypatch.setattr(trigger_module, "_get_agent_manager", lambda: _Mgr())
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_pending_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._handoff_service, "get_accepted_handoffs", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_pending_messages", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service, "get_undelivered_background", empty
    )
    monkeypatch.setattr(
        trigger_module._inbox_service,
        "get_outstanding_ask_messages",
        empty,
    )

    result = await trigger_module.build_trigger_context(
        {"id": "agent-1", "project_id": "p1", "name": "Me"},
        "subordinate",
    )
    assert result is None
