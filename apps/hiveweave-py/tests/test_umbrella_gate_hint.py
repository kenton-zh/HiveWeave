"""Umbrella/parent attestation gate receipt — docs/milestone, not leaf waive."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.attestation import (
    check_task_attestations,
    format_umbrella_gate_hint,
    should_hint_umbrella_gate,
    task_is_umbrella,
)

TASK_U = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def test_format_umbrella_hint_docs_or_milestone_not_leaf_waive():
    text = format_umbrella_gate_hint(
        "code_audit_unit", "Missing required attestation kind(s) ['code_audit']"
    )
    assert len(text.splitlines()) <= 10
    lower = text.lower()
    assert "docs" in lower or "milestone" in lower
    assert "waive_attestation" not in lower
    assert "do not copy a leaf code_audit_unit" in lower
    assert "do not ask ceo for a leaf ticket" in lower


def test_should_hint_on_code_audit_unit():
    assert should_hint_umbrella_gate(
        "code_audit_unit", frozenset({"code_audit", "test_run"}), "missing"
    )
    assert not should_hint_umbrella_gate(
        "docs_only", frozenset({"doc_review"}), "No attestation_ids provided"
    )


@pytest.mark.asyncio
async def test_task_is_umbrella_from_structured_flag():
    task = {
        "id": TASK_U,
        "parent_task_id": None,
        "milestoneVerify": True,
        "title": "BUILD slice",
    }
    assert await task_is_umbrella("proj", task) is True


@pytest.mark.asyncio
async def test_task_is_umbrella_root_with_children():
    task = {"id": TASK_U, "parent_task_id": None, "title": "BUILD"}
    with patch(
        "hiveweave.services.attestation._task_has_children",
        new_callable=AsyncMock,
        return_value=True,
    ):
        assert await task_is_umbrella("proj", task) is True
    child = {"id": TASK_U, "parent_task_id": "parent-1"}
    assert await task_is_umbrella("proj", child) is False


@pytest.mark.asyncio
async def test_check_task_umbrella_code_audit_unit_hint():
    task = {
        "id": TASK_U,
        "title": "BUILD",
        "tags": [],
        "policy_id": "code_audit_unit",
        "evidence": {},
        "parent_task_id": None,
        "milestone_verify": True,
    }
    with (
        patch(
            "hiveweave.services.attestation.has_valid_waiver",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.verify_ids",
            new_callable=AsyncMock,
            return_value=(False, "Missing required attestation kind(s) ['code_audit']"),
        ),
    ):
        err = await check_task_attestations("proj", task, None)
    assert err is not None
    lower = err.lower()
    assert "docs" in lower or "milestone" in lower
    assert "waive_attestation" not in lower
    assert "do not ask ceo for a leaf ticket" in lower
