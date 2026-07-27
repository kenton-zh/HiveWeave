"""Tier failover — resolve_model skip semantics.

回归测试：主用与备用模型允许共用同一 model_id（靠 DB 编号/记录与 API Key 区分）。
failover 跳过失败主用时必须只按 DB 主键(UUID)匹配，绝不能按 model_id 匹配，
否则会误伤同 model_id 的备用模型，导致 failover 失效、agent 直接 park。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.model import ModelService


def _mk(uid: str, model_id: str, key: str) -> dict:
    return {
        "id": uid,
        "name": f"name-{uid}",
        "model_id": model_id,
        "base_url": "https://example.test/v3",
        "api_key": key,
        "provider_type": "openai-compatible",
        "is_active": True,
        "tier": "management",
    }


@pytest.mark.asyncio
async def test_failover_skip_by_uuid_returns_same_model_id_backup():
    """核心回归：skip 失败主用(UUID)后，能返回同 model_id 的备用记录。"""
    svc = ModelService()
    primary = _mk("uuid-A", "deepseek-v4-flash", "key-A")
    backup = _mk("uuid-B", "deepseek-v4-flash", "key-B")  # 同 model_id，不同记录

    async def fake_get(pk: str):
        return {"uuid-A": primary, "uuid-B": backup}.get(pk)

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-A", "backup": "uuid-B"}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary, backup]),
            ):
                resolved = await svc.resolve_model(
                    tier="management", skip_model_ids={"uuid-A"}
                )

    assert resolved is not None, "failover 必须能找到备用模型"
    assert resolved["id"] == "uuid-B"
    assert resolved["api_key"] == "key-B"


@pytest.mark.asyncio
async def test_skipped_matches_id_not_model_id():
    """_skipped 只按主键匹配：传入 model_id 字符串不应跳过任何模型。"""
    svc = ModelService()
    primary = _mk("uuid-A", "deepseek-v4-flash", "key-A")

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-A", "backup": None}),
    ):
        with patch.object(svc, "get", new=AsyncMock(return_value=primary)):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary]),
            ):
                # skip 集合里放的是 model_id 字符串——新语义下不应命中主键
                resolved = await svc.resolve_model(
                    tier="management", skip_model_ids={"deepseek-v4-flash"}
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-A"


@pytest.mark.asyncio
async def test_no_skip_returns_primary():
    """无 skip 时正常返回主用。"""
    svc = ModelService()
    primary = _mk("uuid-A", "deepseek-v4-flash", "key-A")

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-A", "backup": None}),
    ):
        with patch.object(svc, "get", new=AsyncMock(return_value=primary)):
            resolved = await svc.resolve_model(tier="management")

    assert resolved is not None
    assert resolved["id"] == "uuid-A"
