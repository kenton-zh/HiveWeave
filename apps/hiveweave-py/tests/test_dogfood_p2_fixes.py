"""TEST12 dogfood follow-ups: worktree_error heal, VERIFY waive, cases visibility."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.tools.task_tools import (
    WaiveAttestationParams,
    waive_attestation_tool,
)

# Default evidence id for coordinator-path tests.
_EV_ID = "att-evidence-1"


def _waive_params(**kwargs) -> WaiveAttestationParams:
    kwargs.setdefault("evidenceAttestationId", _EV_ID)
    return WaiveAttestationParams(**kwargs)


def _patch_evidence(kind: str = "test_run", task_id: str = "t-docs", exit_code: int = 0):
    """Mock attestation get used by waive_attestation evidence gate."""
    svc = MagicMock()
    svc.ensure_schema = AsyncMock()
    svc.get = AsyncMock(
        return_value={
            "id": _EV_ID,
            "kind": kind,
            "task_id": task_id,
            "exit_code": exit_code,
        }
    )
    return patch(
        "hiveweave.services.attestation.attestation_service",
        svc,
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
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="coordinator",
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        _patch_evidence(kind="doc_review", task_id="t-docs"),
    ):
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "coord1", "role": "架构师"}
        )
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        result = await waive_attestation_tool(
            _waive_params(
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
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="coordinator",
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        _patch_evidence(kind="test_run", task_id="t-verify"),
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=True)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "coord1", "role": "架构师"}
        )
        result = await waive_attestation_tool(
            _waive_params(
                taskId="t-verify",
                reason="本小项目没有独立 QA，请允许协调员豁免并通过审查",
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
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
    ):
        result = await waive_attestation_tool(
            _waive_params(taskId="t1", reason="too short"),
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
    reason = "本小项目没有独立 QA，CEO 已人工抽查并通过 10 条用例。"
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
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
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.task.VerificationCaseService"
        ) as VCS,
        patch("hiveweave.services.inbox.InboxService") as Inbox,
        _patch_evidence(kind="test_run", task_id="t-verify"),
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
            _waive_params(taskId="t-verify", reason=reason),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True
    out = result.output or ""
    assert reason in out
    assert "Stored reason" in out
    vcs.mark_waived.assert_awaited()


def test_deny_if_not_single_task_id():
    from hiveweave.tools.tasks.waive import deny_if_not_single_task_id

    assert deny_if_not_single_task_id("t-verify") is None
    assert deny_if_not_single_task_id("all")
    assert deny_if_not_single_task_id("*")
    assert deny_if_not_single_task_id("全部")
    assert deny_if_not_single_task_id("a,b")
    assert deny_if_not_single_task_id("id1 id2")


@pytest.mark.asyncio
async def test_waive_rejects_bulk_all_token():
    with patch(
        "hiveweave.tools.helpers.get_project_id",
        AsyncMock(return_value="p1"),
    ):
        result = await waive_attestation_tool(
            _waive_params(
                taskId="all",
                reason="I looked at the whole product so skip every gate now.",
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is False
    err = result.output or result.error or ""
    assert "one" in err.lower() or "all" in err.lower()


@pytest.mark.asyncio
async def test_coord_waive_without_evidence_denied():
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="coordinator",
        ),
    ):
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "coord1", "role": "架构师"}
        )
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t1",
                reason="Looked at the UI myself so skip the gate for this.",
                evidenceAttestationId="",
            ),
            agent_id="coord1",
            workspace="/tmp",
        )
    assert result.success is False
    assert "evidenceAttestationId" in (result.output or result.error or "")


@pytest.mark.asyncio
async def test_ceo_waive_without_evidence_one_task():
    task = {
        "id": "t-ui",
        "title": "Landing page",
        "tags": [],
        "assignee_id": "exec1",
    }
    reason = "CEO browsed this landing page on MAIN; skip QA hire for this task."
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.create_waiver",
            AsyncMock(return_value="waiver-uuid-ceo"),
        ) as create,
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch("hiveweave.services.inbox.InboxService") as Inbox,
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "ceo"}
        )
        Inbox.return_value.send_message = AsyncMock()
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-ui",
                reason=reason,
                evidenceAttestationId="",
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True
    out = (result.output or "").lower()
    assert "this task only" in out
    create.assert_awaited()
    assert "[ceo_look]" in str(create.await_args)


@pytest.mark.asyncio
async def test_ceo_can_waive_docs_only_one_task():
    task = {
        "id": "t-docs",
        "title": "Write spec",
        "tags": ["docs_only"],
        "policy_id": "docs_only",
        "assignee_id": "exec1",
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value="p1"),
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch("hiveweave.services.org.OrgService") as Org,
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value="ceo",
        ),
        patch(
            "hiveweave.services.attestation.create_waiver",
            AsyncMock(return_value="waiver-docs"),
        ),
        patch(
            "hiveweave.services.attestation.count_waivers",
            AsyncMock(return_value=0),
        ),
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            AsyncMock(return_value=False),
        ),
        patch("hiveweave.services.inbox.InboxService") as Inbox,
    ):
        TS.return_value.get_task = AsyncMock(return_value=task)
        TS.return_value._is_verify_task = MagicMock(return_value=False)
        Org.return_value.get_agent = AsyncMock(
            return_value={"id": "ceo1", "role": "ceo"}
        )
        Inbox.return_value.send_message = AsyncMock()
        result = await waive_attestation_tool(
            WaiveAttestationParams(
                taskId="t-docs",
                reason="CEO read this spec task; waive doc_review gate here.",
                evidenceAttestationId="",
            ),
            agent_id="ceo1",
            workspace="/tmp",
        )
    assert result.success is True


def test_create_error_already_exists_heuristic():
    """Sanity: error text used by attach-after-exists path."""
    err = "fatal: a branch named 'hw/A004/work' already exists"
    assert "already exists" in err.lower()
    assert "hw/A004/work" in err
