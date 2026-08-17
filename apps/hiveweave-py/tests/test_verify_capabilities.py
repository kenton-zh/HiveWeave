"""TEST21 M12 — VERIFY QA capability filtering."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.tools.task_tools import (
    _find_independent_qa,
    _verify_required_capabilities,
)


def test_verify_required_capabilities_ui():
    caps = _verify_required_capabilities("ui_browser_e2e")
    assert caps == ["browse", "browser_acceptance"]


def test_verify_required_capabilities_docs():
    assert _verify_required_capabilities("docs_only") == ["source_read"]


def test_verify_required_capabilities_default():
    assert _verify_required_capabilities("generic_tests") == [
        "test_run",
        "source_read",
    ]


@pytest.mark.asyncio
async def test_find_independent_qa_filters_by_caps():
    qa_agent = {
        "id": "qa-1",
        "status": "active",
        "parent_id": "mgr-1",
        "permission_type": "executor",
        "role": "测试工程师",
        "skills": ["browse", "qa"],
    }
    dev_agent = {
        "id": "dev-1",
        "status": "active",
        "parent_id": "mgr-1",
        "permission_type": "executor",
        "role": "前端工程师",
        "skills": [],
    }

    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[qa_agent, dev_agent]),
    ):
        picked = await _find_independent_qa(
            "proj",
            original_assignee="dev-1",
            required_capabilities=["browse", "browser_acceptance"],
        )
    assert picked == "qa-1"


@pytest.mark.asyncio
async def test_find_independent_qa_none_when_caps_missing():
    dev_agent = {
        "id": "dev-1",
        "status": "active",
        "parent_id": "mgr-1",
        "permission_type": "executor",
        "role": "前端工程师",
        "skills": [],
    }
    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[dev_agent]),
    ):
        picked = await _find_independent_qa(
            "proj",
            original_assignee=None,
            required_capabilities=["browse", "browser_acceptance"],
        )
    assert picked is None


@pytest.mark.asyncio
async def test_find_independent_qa_skips_ceo_even_with_browse():
    ceo = {
        "id": "ceo-1",
        "status": "active",
        "parent_id": "",
        "permission_type": "coordinator",
        "role": "ceo",
        "name": "归零",
        "skills": [],
    }
    qa_agent = {
        "id": "qa-1",
        "status": "active",
        "parent_id": "mgr-1",
        "permission_type": "executor",
        "role": "测试工程师",
        "skills": ["browse", "qa"],
    }
    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[ceo, qa_agent]),
    ):
        picked = await _find_independent_qa(
            "proj",
            original_assignee="dev-1",
            required_capabilities=["browse", "browser_acceptance"],
        )
    assert picked == "qa-1"
    with patch(
        "hiveweave.services.org.OrgService.list_agents",
        new=AsyncMock(return_value=[ceo]),
    ):
        none = await _find_independent_qa(
            "proj",
            original_assignee=None,
            required_capabilities=["browse"],
        )
    assert none is None
