"""Org span hard-gate unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.org import OrgService
from hiveweave.services.org_span import (
    validate_dispatch_span,
    validate_executor_assignee,
    validate_message_span,
)


@pytest.mark.asyncio
async def test_dispatch_allows_direct_report():
    async def ga(aid):
        if aid == "boss":
            return {"id": "boss", "parent_id": None, "name": "Boss"}
        return {
            "id": "kid",
            "parent_id": "boss",
            "name": "Kid",
            "permission_type": "executor",
        }

    with patch.object(OrgService, "get_agent", AsyncMock(side_effect=ga)):
        assert await validate_dispatch_span("boss", "kid") is None


@pytest.mark.asyncio
async def test_dispatch_blocks_cross_level():
    async def ga(aid):
        mapping = {
            "ceo": {"id": "ceo", "parent_id": None, "name": "CEO"},
            "tl": {"id": "tl", "parent_id": "ceo", "name": "TL"},
            "eng": {
                "id": "eng",
                "parent_id": "tl",
                "name": "Eng",
                "permission_type": "executor",
            },
        }
        return mapping.get(aid)

    with patch.object(OrgService, "get_agent", AsyncMock(side_effect=ga)):
        err = await validate_dispatch_span("ceo", "eng")
    assert err is not None
    assert "跨级" in err


@pytest.mark.asyncio
async def test_message_allows_peer():
    async def ga(aid):
        return {
            "id": aid,
            "parent_id": "ceo",
            "name": aid,
            "status": "active",
        }

    with patch.object(OrgService, "get_agent", AsyncMock(side_effect=ga)):
        assert await validate_message_span("hr", "tl") is None


@pytest.mark.asyncio
async def test_executor_assignee_blocks_coordinator():
    with patch.object(
        OrgService,
        "get_agent",
        AsyncMock(
            return_value={
                "id": "x",
                "name": "Lead",
                "permission_type": "coordinator",
            }
        ),
    ):
        err = await validate_executor_assignee("x")
    assert err is not None
    assert "coordinator" in err.lower() or "拒绝派活" in err
