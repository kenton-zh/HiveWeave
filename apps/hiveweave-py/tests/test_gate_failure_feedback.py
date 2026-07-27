"""Gate failure reasons must reach the AI (tool result / conversation notice)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hiveweave.services.org_span import (
    validate_ceo_dispatch_target,
    validate_dispatch_span,
    validate_executor_assignee,
)
from hiveweave.services.permission import PermissionService
from hiveweave.tools.pipeline import build_deny_hint


def _agent(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "墨白",
        "role": "签到工程师",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "allowed_tools": "[]",
        "denied_tools": "[]",
        "ask_tools": "[]",
    }
    base.update(kwargs)
    return base


def _patch_agent(monkeypatch: pytest.MonkeyPatch, agent: dict) -> None:
    async def fake_get(_aid):
        return agent

    monkeypatch.setattr(
        "hiveweave.services.permission.meta_db.get_agent_by_id", fake_get
    )


# ── Permission deny reasons ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_denied_tools_reason_is_explicit(monkeypatch):
    svc = PermissionService()
    _patch_agent(
        monkeypatch,
        _agent(denied_tools='["bash"]'),
    )
    decision, reason = await svc.evaluate_detailed("a1", "bash", {})
    assert decision == "deny"
    assert reason is not None
    assert "denied_tools" in reason
    assert "operator configured" in reason
    hint = build_deny_hint("bash", "executor", reason)
    assert "denied_tools" in hint


@pytest.mark.asyncio
async def test_ceo_allowlist_deny_reason(monkeypatch):
    svc = PermissionService()
    _patch_agent(
        monkeypatch,
        _agent(
            role="ceo",
            name="归零",
            permission_type="coordinator",
            permission_mode="readonly",
        ),
    )
    # bash is capability-denied for CEO; reason comes from hard_check
    decision, reason = await svc.evaluate_detailed("a1", "bash", {})
    assert decision == "deny"
    assert reason
    # hard capability text OR allowlist — either is specific, not generic blocked
    assert "blocked for this agent" not in (reason or "")


@pytest.mark.asyncio
async def test_coordinator_allowlist_deny_reason(monkeypatch):
    """Tool not on coordinator allowlist → explicit allowlist reason (not hard)."""
    svc = PermissionService()
    # Patch hard_check to None so we hit allowlist path
    monkeypatch.setattr(
        "hiveweave.services.permission.policy_service.hard_check",
        lambda *_a, **_k: None,
    )
    _patch_agent(
        monkeypatch,
        _agent(
            role="前端架构师",
            name="云岫",
            permission_type="coordinator",
            permission_mode="readwrite",
        ),
    )
    # hire_agent is HR-only in allowlists; hard may also deny — we stubbed hard
    decision, reason = await svc.evaluate_detailed("a1", "hire_agent", {})
    assert decision == "deny"
    assert reason is not None
    assert "allowlist" in reason


# ── org_span fail-closed ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_span_lookup_fail_closed():
    org = MagicMock()
    org.get_agent = AsyncMock(side_effect=RuntimeError("db down"))
    err = await validate_dispatch_span("from1", "to1", org_service=org)
    assert err is not None
    assert "组织查询失败" in err


@pytest.mark.asyncio
async def test_executor_assignee_missing_agent():
    org = MagicMock()
    org.get_agent = AsyncMock(return_value=None)
    err = await validate_executor_assignee("missing-id", org_service=org)
    assert err is not None
    assert "不存在" in err


@pytest.mark.asyncio
async def test_executor_assignee_lookup_fail_closed():
    org = MagicMock()
    org.get_agent = AsyncMock(side_effect=RuntimeError("boom"))
    err = await validate_executor_assignee("x", org_service=org)
    assert err is not None
    assert "组织查询失败" in err


@pytest.mark.asyncio
async def test_ceo_dispatch_target_missing():
    org = MagicMock()

    async def get_agent(aid):
        if aid == "ceo1":
            return {"id": "ceo1", "role": "ceo", "permission_type": "coordinator"}
        return None

    org.get_agent = AsyncMock(side_effect=get_agent)
    err = await validate_ceo_dispatch_target("ceo1", "gone", org_service=org)
    assert err is not None
    assert "不存在" in err


# ── commit_turn labels include HIRE_UNREPORTED ───────────────────────────


def test_commit_turn_labels_include_hire_unreported():
    from hiveweave.tools import turn_tools as tt

    src = open(tt.__file__, encoding="utf-8").read()
    assert '"HIRE_UNREPORTED"' in src
    assert "本轮 hire_agent 后未通知请求方" in src


# ── gate notice helper ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_gate_notice_appends_user_message():
    from hiveweave.agents.agent import Agent

    appended: list = []

    class FakeConv:
        async def append_turn(self, agent_id, project_id, messages):
            appended.extend(messages)

    agent = object.__new__(Agent)
    agent.id = "ag1"
    agent.project_id = "p1"
    agent._conversation = FakeConv()
    agent._pending_resume_hint = None

    await Agent._persist_gate_notice(
        agent,
        "TURN EXIT PARKED",
        "GATE=OPEN_TASKS_UNDECLARED",
        footer="停泊说明",
    )
    assert len(appended) == 1
    assert appended[0]["role"] == "user"
    assert "[TURN EXIT PARKED]" in appended[0]["content"]
    assert "OPEN_TASKS_UNDECLARED" in appended[0]["content"]
    assert "停泊说明" in appended[0]["content"]
