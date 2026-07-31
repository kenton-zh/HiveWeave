"""Agent 显式 model_id 必须优先于 tier primary/backup。

回归：UI「使用模型」切换后 agent 仍用旧模型的根因——
1. resolve_model 忽略 preferred（显式 model_id），tier primary 永远赢
2. update_agent 只写 DB，不刷新运行中 Agent 的内存 config
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.agents.agent import Agent
from hiveweave.services.model import ModelService


def _mk(uid: str, model_id: str, active: bool = True) -> dict:
    return {
        "id": uid,
        "name": f"name-{uid}",
        "model_id": model_id,
        "base_url": "https://example.test/v3",
        "api_key": "key-" + uid,
        "provider_type": "openai-compatible",
        "is_active": 1 if active else 0,
        "tier": None,
        "context_window": 128000,
        "max_output_tokens": 8192,
    }


@pytest.mark.asyncio
async def test_resolve_model_preferred_beats_tier_primary():
    """显式指定的模型优先于 tier primary（用户切了模型必须生效）。"""
    svc = ModelService()
    explicit = _mk("uuid-EXP", "user-picked-model")
    primary = _mk("uuid-PRI", "deepseek-v4-flash")

    async def fake_get(pk: str):
        for m in (explicit, primary):
            if m["id"] == pk or m["model_id"] == pk:
                return m
        return None

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-PRI", "backup": None}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary, explicit]),
            ):
                resolved = await svc.resolve_model(
                    tier="management", preferred="user-picked-model"
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-EXP"


@pytest.mark.asyncio
async def test_resolve_model_preferred_inactive_falls_to_tier():
    """显式模型 inactive 时回退 tier primary，不能返回失效模型。"""
    svc = ModelService()
    explicit = _mk("uuid-EXP", "user-picked-model", active=False)
    primary = _mk("uuid-PRI", "deepseek-v4-flash")

    async def fake_get(pk: str):
        for m in (explicit, primary):
            if m["id"] == pk or m["model_id"] == pk:
                return m
        return None

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-PRI", "backup": None}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary]),
            ):
                resolved = await svc.resolve_model(
                    tier="management", preferred="user-picked-model"
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-PRI"


@pytest.mark.asyncio
async def test_resolve_model_preferred_missing_falls_to_tier():
    """显式模型不存在时回退 tier primary。"""
    svc = ModelService()
    primary = _mk("uuid-PRI", "deepseek-v4-flash")

    async def fake_get(pk: str):
        for m in (primary,):
            if m["id"] == pk or m["model_id"] == pk:
                return m
        return None

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-PRI", "backup": None}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary]),
            ):
                resolved = await svc.resolve_model(
                    tier="management", preferred="nonexistent-model"
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-PRI"


@pytest.mark.asyncio
async def test_resolve_model_preferred_uuid_form():
    """agents.model_id 存 UUID 形式时（UI 下拉传 m.id）同样优先。"""
    svc = ModelService()
    explicit = _mk("uuid-EXP", "user-picked-model")
    primary = _mk("uuid-PRI", "deepseek-v4-flash")

    async def fake_get(pk: str):
        for m in (explicit, primary):
            if m["id"] == pk or m["model_id"] == pk:
                return m
        return None

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-PRI", "backup": None}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary, explicit]),
            ):
                resolved = await svc.resolve_model(
                    tier="management", preferred="uuid-EXP"
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-EXP"


@pytest.mark.asyncio
async def test_resolve_model_preferred_skipped_falls_to_tier():
    """显式模型被 skip（failover 已失败）时回退 tier primary。"""
    svc = ModelService()
    explicit = _mk("uuid-EXP", "user-picked-model")
    primary = _mk("uuid-PRI", "deepseek-v4-flash")

    async def fake_get(pk: str):
        for m in (explicit, primary):
            if m["id"] == pk or m["model_id"] == pk:
                return m
        return None

    with patch.object(
        svc, "get_tier_config",
        new=AsyncMock(return_value={"primary": "uuid-PRI", "backup": None}),
    ):
        with patch.object(svc, "get", side_effect=fake_get):
            with patch.object(
                svc, "list_active_full",
                new=AsyncMock(return_value=[primary]),
            ):
                resolved = await svc.resolve_model(
                    tier="management",
                    preferred="user-picked-model",
                    skip_model_ids={"uuid-EXP"},
                )

    assert resolved is not None
    assert resolved["id"] == "uuid-PRI"


@pytest.mark.asyncio
async def test_update_agent_refreshes_live_config():
    """update_agent 后运行中 Agent 的内存 config 必须同步（不必重启后端）。"""
    from hiveweave.services.org import OrgService

    svc = OrgService()
    row = {
        "id": "agent-1",
        "project_id": "proj-1",
        "name": "A",
        "role": "executor",
        "model_id": "old-model",
        "bound_skills": "[]",
        "status": "active",
        "created_at": 1,
        "updated_at": 1,
    }
    live = Agent(
        agent_id="agent-1",
        project_id="proj-1",
        config=dict(row),
    )

    with patch.object(svc, "get_agent", new=AsyncMock(return_value=dict(row))), \
            patch(
                "hiveweave.services.org.project_db.execute",
                new=AsyncMock(),
            ), \
            patch(
                "hiveweave.agents.supervisor.agent_manager.get_agent",
                return_value=live,
            ):
        await svc.update_agent("agent-1", {"model_id": "user-picked-model"})

    assert live.config["model_id"] == "user-picked-model"
