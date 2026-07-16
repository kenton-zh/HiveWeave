"""P0 Hard Gates — Wait TTL, SCC break, attestation gates."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.attestation import (
    AttestationService,
    check_task_attestations,
    is_test_command,
    required_attestation_kinds,
    resolve_task_policy,
)
from hiveweave.services.wait_contract import (
    WaitContractService,
    _scc,
    default_ttl_ms,
)


def test_default_ttl_by_kind():
    assert default_ttl_ms("agent") == 15 * 60 * 1000
    assert default_ttl_ms("user") == 60 * 60 * 1000
    assert default_ttl_ms("task") == 30 * 60 * 1000


def test_scc_detects_cycle():
    graph = {"a": {"b"}, "b": {"a"}, "c": set()}
    comps = [sorted(c) for c in _scc(graph) if len(c) >= 2]
    assert ["a", "b"] in comps


def test_is_test_command():
    assert is_test_command("npm test")
    assert is_test_command("cd apps && pytest -q")
    assert is_test_command("pnpm run test")
    assert not is_test_command("npm run build")
    assert not is_test_command("ls -la")


def test_resolve_task_policy():
    assert resolve_task_policy("Fix login UI", ["frontend"]) == "ui_browser_e2e"
    assert resolve_task_policy("Write README", ["docs"]) == "docs_only"
    assert resolve_task_policy("Implement API", []) == "generic_tests"
    # "suite" must not false-positive match "ui"
    assert resolve_task_policy("Run test suite", []) == "generic_tests"
    assert (
        resolve_task_policy("VERIFY: Feature X", ["verify", "post-merge"])
        == "generic_tests"
    )
    assert (
        resolve_task_policy("VERIFY: Fix login UI", ["verify", "ui"])
        == "ui_browser_e2e"
    )


def test_required_kinds():
    assert required_attestation_kinds("ui_browser_e2e") == frozenset({"browse_e2e"})
    assert required_attestation_kinds("generic_tests") == frozenset({"test_run"})
    assert required_attestation_kinds("docs_only") is None


@pytest.mark.asyncio
async def test_check_task_rejects_bare_tests_passed():
    task = {
        "id": "t1",
        "title": "UI button",
        "tags": ["ui"],
        "policy_id": "ui_browser_e2e",
        "evidence": {"tests_passed": True},
    }
    err = await check_task_attestations("p1", task, None)
    assert err is not None
    assert "attestation" in err.lower() or "REJECT" in err


@pytest.mark.asyncio
async def test_replace_waits_sets_default_ttl(tmp_path, monkeypatch):
    """replace_waits applies default expires_at when none given."""
    svc = WaitContractService()
    now = int(time.time() * 1000)
    created_rows: list = []

    class FakeConn:
        async def execute(self, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                created_rows.append(list(params))
            return MagicMock(rowcount=0)

        async def commit(self):
            pass

    async def fake_conn(_pid):
        return FakeConn()

    async def fake_ensure(_pid):
        return None

    monkeypatch.setattr(
        "hiveweave.services.wait_contract._ensure_schema", fake_ensure
    )
    monkeypatch.setattr(
        "hiveweave.services.wait_contract._conn", fake_conn
    )

    out = await svc.replace_waits(
        "proj",
        "agent-a",
        [{"kind": "agent", "ref": "墨白"}],
        phase="waiting_agent",
    )
    assert len(out) == 1
    assert out[0]["expiresAt"] is not None
    assert out[0]["expiresAt"] >= now + 14 * 60 * 1000


@pytest.mark.asyncio
async def test_clear_expired_returns_rows(monkeypatch):
    svc = WaitContractService()
    past = int(time.time() * 1000) - 1000
    row = {
        "id": "w1",
        "agent_id": "a1",
        "project_id": "p1",
        "kind": "agent",
        "ref": "b",
        "wake_on": '["timeout"]',
        "expires_at": past,
        "obligation_version": "x",
        "phase": "waiting_agent",
        "note": None,
        "created_at": past - 10000,
        "cleared_at": None,
    }

    class FakeCursor:
        async def fetchall(self):
            return [row]

        async def close(self):
            pass

    class FakeConn:
        def __init__(self):
            self.updated = False

        async def execute(self, sql, params=None):
            if sql.strip().upper().startswith("SELECT"):
                return FakeCursor()
            self.updated = True
            return MagicMock(rowcount=1)

        async def commit(self):
            pass

    conn = FakeConn()

    async def fake_conn(_pid):
        return conn

    async def fake_ensure(_pid):
        return None

    monkeypatch.setattr(
        "hiveweave.services.wait_contract._ensure_schema", fake_ensure
    )
    monkeypatch.setattr(
        "hiveweave.services.wait_contract._conn", fake_conn
    )

    cleared = await svc.clear_expired("p1")
    assert len(cleared) == 1
    assert cleared[0]["agentId"] == "a1"
    assert conn.updated


@pytest.mark.asyncio
async def test_break_wait_cycles_clears_min_id(monkeypatch):
    svc = WaitContractService()
    waits = [
        {
            "id": "w1",
            "agentId": "agent-b",
            "projectId": "p",
            "kind": "agent",
            "ref": "agent-a",
            "wakeOn": ["timeout"],
            "expiresAt": None,
            "obligationVersion": "",
            "phase": "x",
            "note": None,
            "createdAt": 1,
            "clearedAt": None,
        },
        {
            "id": "w2",
            "agentId": "agent-a",
            "projectId": "p",
            "kind": "agent",
            "ref": "agent-b",
            "wakeOn": ["timeout"],
            "expiresAt": None,
            "obligationVersion": "",
            "phase": "x",
            "note": None,
            "createdAt": 1,
            "clearedAt": None,
        },
    ]

    async def fake_list(_pid):
        return waits

    cleared_for: list[str] = []

    class FakeConn:
        async def execute(self, sql, params=None):
            if "UPDATE" in sql.upper():
                cleared_for.append(params[1])
            return MagicMock(rowcount=1)

        async def commit(self):
            pass

    async def fake_conn(_pid):
        return FakeConn()

    monkeypatch.setattr(svc, "list_all_active", fake_list)
    monkeypatch.setattr(
        "hiveweave.services.wait_contract._conn", fake_conn
    )

    breaks = await svc.break_wait_cycles(
        "p", lambda ref: ref if ref.startswith("agent-") else None
    )
    assert breaks
    # min(agent-a, agent-b) == agent-a
    assert breaks[0]["breakerId"] == "agent-a"
    assert "agent-a" in cleared_for


@pytest.mark.asyncio
async def test_verify_ids_rejects_missing(monkeypatch):
    svc = AttestationService()

    async def fake_get(_pid, _aid):
        return None

    monkeypatch.setattr(svc, "get", fake_get)
    monkeypatch.setattr(svc, "ensure_schema", AsyncMock())
    ok, err = await svc.verify_ids("p", ["missing"], expected_kinds=["test_run"])
    assert not ok
    assert "not found" in err.lower()


@pytest.mark.asyncio
async def test_independent_qa_finder():
    from hiveweave.tools.task_tools import _find_independent_qa

    agents = [
        {
            "id": "dev-1",
            "role": "签到工程师",
            "permission_type": "executor",
            "parent_id": "arch-1",
            "status": "active",
        },
        {
            "id": "qa-1",
            "role": "测试工程师",
            "permission_type": "executor",
            "parent_id": "arch-1",
            "status": "active",
        },
    ]

    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=agents),
    ):
        qa = await _find_independent_qa("p", original_assignee="dev-1")
        assert qa == "qa-1"

    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=agents),
    ):
        # Must not pick self
        qa2 = await _find_independent_qa("p", original_assignee="qa-1")
        assert qa2 is None or qa2 != "qa-1"
