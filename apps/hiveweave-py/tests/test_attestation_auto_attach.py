"""submit_task auto-attaches recent attestations (TEST4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_find_recent_prefers_task_match():
    from hiveweave.services.attestation import AttestationService

    svc = AttestationService()
    now = int(time.time() * 1000)
    rows = [
        {
            "id": "att-task",
            "task_id": "task-1",
            "kind": "bash_test",
            "created_at": now,
        },
        {
            "id": "att-fallback",
            "task_id": "",
            "kind": "bash_test",
            "created_at": now - 1000,
        },
    ]

    class _Cur:
        async def fetchall(self):
            return rows

        async def close(self):
            return None

    class _Conn:
        async def execute(self, *_a, **_k):
            return _Cur()

    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch(
            "hiveweave.services.attestation._conn",
            new_callable=AsyncMock,
            return_value=_Conn(),
        ),
    ):
        ids = await svc.find_recent_for_agent(
            "proj",
            agent_id="agent-1",
            task_id="task-1",
            kinds=["bash_test"],
        )
    assert ids == ["att-task"]


@pytest.mark.asyncio
async def test_submit_auto_attaches_when_ids_omitted():
    """When attestationIds omitted, recent ids are filled before verify."""
    from types import SimpleNamespace

    params = SimpleNamespace(
        task_id="task-1",
        summary="done",
        commit=None,
        files_changed=None,
        test_output=None,
        tests_passed=True,
        attestation_ids=None,
    )

    task = {
        "id": "task-1",
        "status": "running",
        "tags": [],
        "policy_id": "generic_tests",
        "title": "impl",
        "description": "",
        "creator_id": "agent-1",  # same agent → no inbox notify
    }

    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            new_callable=AsyncMock,
            return_value="proj",
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
        patch(
            "hiveweave.services.attestation.required_attestation_kinds",
            return_value=["bash_test"],
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.find_recent_for_agent",
            new_callable=AsyncMock,
            return_value=["auto-att-1"],
        ) as find_mock,
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ) as verify_mock,
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new_callable=AsyncMock,
        ),
        patch(
            "hiveweave.services.handoff.HandoffService.mark_reported",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts.submit_task = AsyncMock()

        from hiveweave.tools.task_tools import submit_task_tool

        result = await submit_task_tool(params, "agent-1", "/tmp/ws")

    assert result.success is True
    find_mock.assert_awaited()
    verify_mock.assert_awaited()
    # verify_ids(project_id, attest_ids, ...)
    assert verify_mock.await_args.args[1] == ["auto-att-1"]
