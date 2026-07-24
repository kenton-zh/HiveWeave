"""TEST12 dogfood follow-ups: worktree_error heal, VERIFY waive, cases visibility."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.tools.task_tools import (
    WaiveAttestationParams,
    waive_attestation_tool,
)


@pytest.mark.asyncio
async def test_waive_rejects_docs_only():
    task = {
        "id": "t-docs",
        "title": "Write spec",
        "tags": ["docs_only"],
        "policy_id": "docs_only",
        "assignee_id": "exec1",
    }
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-docs",
                reason="Short waive that should still be blocked for docs",
            ),
            agent_id="coord1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "attest_doc_review" in (result.output or result.error or "")


@pytest.mark.asyncio
async def test_waive_verify_rejects_non_ceo():
    task = {
        "id": "t-verify",
        "title": "VERIFY: feature",
        "tags": ["verify", "mandatory"],
        "parent_task_id": "t-parent",
        "assignee_id": "qa1",
    }
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="coordinator",
        ),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=True)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "coord1", "role": "技术负责人"}
        )
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-verify",
                reason="极小项目不扩招 QA，技术负责人已审测通过十项",
            ),
            agent_id="coord1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "only CEO" in (result.output or result.error or "")


@pytest.mark.asyncio
async def test_waive_reason_min_length():
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
    ):
        result = await waive_attestation_tool(
            WaiveAttestationParams(taskId="t1", reason="too short"),
            agent_id="c1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "20" in (result.output or result.error or "")


@pytest.mark.asyncio
async def test_waive_verify_ceo_ok_echoes_reason():
    task = {
        "id": "t-verify",
        "title": "VERIFY: feature",
        "tags": ["verify"],
        "parent_task_id": "t-parent",
        "assignee_id": "qa1",
    }
    reason = "极小项目不扩招 QA。技术负责人已审查并通过 10 条测试。"
    with (
        patch(
            "hiveweave.tools.task_tools.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.tools.task_tools.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.create_waiver",
            AsyncMock(return_value="waiver-uuid-1"),
        ),
        patch(
            "hiveweave.services.task.VerificationCaseService"
        ) as VCS,
        patch("hiveweave.services.inbox.InboxService") as Inbox,
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=True)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "ceo"}
        )
        vcs = VCS.return_value
        vcs.ensure_case = AsyncMock(return_value="case1")
        vcs.mark_waived = AsyncMock()
        Inbox.return_value.send_message = AsyncMock()
        result = await waive_attestation_tool(
            WaiveAttestationParams(taskId="t-verify", reason=reason),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True
    out = result.output or ""
    assert reason in out
    assert "Stored reason" in out
    vcs.mark_waived.assert_awaited()


def test_create_error_already_exists_heuristic():
    """Sanity: error text used by attach-after-exists path."""
    err = "fatal: a branch named 'hw/A004/work' already exists"
    assert "already exists" in err.lower()
    assert "hw/A004/work" in err
