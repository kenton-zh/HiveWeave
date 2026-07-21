"""Org span hard-gate unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.org import OrgService
from hiveweave.services.org_span import (
    validate_ceo_dispatch_target,
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
async def test_executor_assignee_allows_builder_coordinator():
    """中层 builder coordinator（family=coordinator，有 SOURCE_WRITE）可接代码任务。"""
    with patch.object(
        OrgService,
        "get_agent",
        AsyncMock(
            return_value={
                "id": "x",
                "name": "Lead",
                "role": "前端架构师",
                "permission_type": "coordinator",
            }
        ),
    ):
        assert await validate_executor_assignee("x") is None


@pytest.mark.asyncio
async def test_executor_assignee_blocks_ceo():
    """family=ceo 一律拒绝承接改代码任务。"""
    with patch.object(
        OrgService,
        "get_agent",
        AsyncMock(
            return_value={
                "id": "x",
                "name": "归零",
                "role": "ceo",
                "permission_type": "coordinator",
            }
        ),
    ):
        err = await validate_executor_assignee("x")
    assert err is not None
    assert "CEO" in err or "拒绝派活" in err


@pytest.mark.asyncio
async def test_executor_assignee_blocks_hr():
    """HR 无 SOURCE_WRITE —— 仍拒绝。"""
    with patch.object(
        OrgService,
        "get_agent",
        AsyncMock(
            return_value={
                "id": "x",
                "name": "天线",
                "role": "hr",
                "permission_type": "coordinator",
            }
        ),
    ):
        err = await validate_executor_assignee("x")
    assert err is not None
    assert "拒绝派活" in err


@pytest.mark.asyncio
async def test_ceo_dispatch_only_to_mid_coordinator():
    """CEO 派工硬门：只能派直属中层 coordinator（修复语义倒挂）。"""
    agents = {
        "ceo": {"id": "ceo", "role": "ceo", "permission_type": "coordinator",
                "parent_id": None, "name": "归零"},
        "mid": {"id": "mid", "role": "前端架构师",
                "permission_type": "coordinator", "parent_id": "ceo",
                "name": "云岫"},
        "eng": {"id": "eng", "role": "签到工程师",
                "permission_type": "executor", "parent_id": "mid",
                "name": "墨白"},
        "hr": {"id": "hr", "role": "hr", "permission_type": "coordinator",
               "parent_id": "ceo", "name": "天线"},
    }

    async def ga(aid):
        return agents.get(aid)

    with patch.object(OrgService, "get_agent", AsyncMock(side_effect=ga)):
        # CEO → 中层 coordinator：放行
        assert await validate_ceo_dispatch_target("ceo", "mid") is None
        # CEO → executor：硬拒（不再日常直派叶子）
        err = await validate_ceo_dispatch_target("ceo", "eng")
        assert err is not None and "中层" in err
        # CEO → HR：硬拒
        err = await validate_ceo_dispatch_target("ceo", "hr")
        assert err is not None
        # 中层 → executor：不受此门约束
        assert await validate_ceo_dispatch_target("mid", "eng") is None
