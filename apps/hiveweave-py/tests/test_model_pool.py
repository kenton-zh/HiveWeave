"""Model pool — pick_from_pool round-robin + dual-channel ensure.

Direct tests for the Ark dual-channel pool: rotation across active models,
single/empty pool fallback, and dual-key channel upsert behavior.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import model as model_module
from hiveweave.services.model import ModelService


def _mk_model(name: str, key: str = "k") -> dict:
    return {
        "id": f"id-{name}",
        "name": name,
        "model_id": "m",
        "base_url": "https://example.test/v3",
        "api_key": key,
        "provider_type": "openai-compatible",
        "is_active": True,
    }


@pytest.fixture(autouse=True)
def reset_pool_counter():
    model_module._pool_counter = itertools.count()
    yield
    model_module._pool_counter = itertools.count()


@pytest.mark.asyncio
async def test_pick_round_robin_rotates_across_pool():
    svc = ModelService()
    pool = [_mk_model("A"), _mk_model("B")]
    with patch.object(
        svc, "list_active_full", new=AsyncMock(return_value=pool)
    ):
        picks = [await svc.pick_from_pool() for _ in range(4)]
    assert [p["name"] for p in picks] == ["A", "B", "A", "B"]


@pytest.mark.asyncio
async def test_pick_single_active_returns_it():
    svc = ModelService()
    only = _mk_model("ONLY")
    with patch.object(
        svc, "list_active_full", new=AsyncMock(return_value=[only])
    ):
        assert (await svc.pick_from_pool())["name"] == "ONLY"
        assert (await svc.pick_from_pool())["name"] == "ONLY"


@pytest.mark.asyncio
async def test_pick_empty_pool_falls_back_to_preferred():
    svc = ModelService()
    preferred = _mk_model("PREF")
    with patch.object(svc, "list_active_full", new=AsyncMock(return_value=[])):
        with patch.object(svc, "get", new=AsyncMock(return_value=preferred)):
            assert (await svc.pick_from_pool("id-PREF"))["name"] == "PREF"
        with patch.object(svc, "get", new=AsyncMock(return_value=None)):
            assert await svc.pick_from_pool(None) is None


@pytest.mark.asyncio
async def test_pick_rotation_survives_pool_resize():
    """Counter-based rotation keeps working when pool grows (no crash, valid pick)."""
    svc = ModelService()
    pool = [_mk_model("A"), _mk_model("B")]
    with patch.object(
        svc, "list_active_full", new=AsyncMock(return_value=pool)
    ):
        await svc.pick_from_pool()  # idx 0
        await svc.pick_from_pool()  # idx 1
    pool3 = pool + [_mk_model("C")]
    with patch.object(
        svc, "list_active_full", new=AsyncMock(return_value=pool3)
    ):
        pick = await svc.pick_from_pool()  # idx 2 % 3
    assert pick["name"] == "C"


@pytest.mark.asyncio
async def test_ensure_channel_models_dual_key_upserts_both():
    svc = ModelService()
    upserts: list[dict] = []

    async def fake_upsert(attrs: dict) -> dict:
        upserts.append(attrs)
        return {"id": f"id-{len(upserts)}", **attrs}

    class FakeSettings:
        ark_api_key = "plan-key"
        ark_base_url = "https://ark.example/api/plan/v3"
        ark_model_id = "model-x"
        ark_coding_api_key = "coding-key"
        ark_coding_base_url = "https://ark.example/api/coding/v3"
        ark_coding_model_id = "model-y"

    with patch.object(svc, "upsert_by_name", side_effect=fake_upsert):
        with patch.object(
            svc, "list_active", new=AsyncMock(return_value=[])
        ):
            with patch("hiveweave.config.settings", FakeSettings()):
                out = await svc.ensure_channel_models()

    assert len(upserts) == 2
    plan, coding = upserts
    assert plan["api_key"] == "plan-key"
    assert plan["base_url"] == "https://ark.example/api/plan/v3"
    assert plan["model_id"] == "model-x"
    assert coding["api_key"] == "coding-key"
    assert coding["base_url"] == "https://ark.example/api/coding/v3"
    assert coding["model_id"] == "model-y"
    assert len(out["ensured"]) == 2


@pytest.mark.asyncio
async def test_ensure_channel_models_same_key_skips_coding():
    svc = ModelService()
    upserts: list[dict] = []

    async def fake_upsert(attrs: dict) -> dict:
        upserts.append(attrs)
        return {"id": "id-1", **attrs}

    class FakeSettings:
        ark_api_key = "same-key"
        ark_base_url = "https://ark.example/api/plan/v3"
        ark_model_id = "model-x"
        ark_coding_api_key = "same-key"
        ark_coding_base_url = "https://ark.example/api/coding/v3"
        ark_coding_model_id = "model-x"

    with patch.object(svc, "upsert_by_name", side_effect=fake_upsert):
        with patch.object(
            svc, "list_active", new=AsyncMock(return_value=[])
        ):
            with patch("hiveweave.config.settings", FakeSettings()):
                out = await svc.ensure_channel_models()

    assert len(upserts) == 1
    assert upserts[0]["api_key"] == "same-key"
    assert len(out["ensured"]) == 1


@pytest.mark.asyncio
async def test_ensure_channel_models_no_plan_key_noop():
    svc = ModelService()

    class FakeSettings:
        ark_api_key = ""
        ark_base_url = ""
        ark_model_id = ""
        ark_coding_api_key = ""
        ark_coding_base_url = ""
        ark_coding_model_id = ""

    with patch.object(
        svc, "upsert_by_name", new=AsyncMock()
    ) as up:
        with patch.object(
            svc, "find_by_name", new=AsyncMock(return_value=None)
        ):
            with patch.object(
                svc, "list_active", new=AsyncMock(return_value=[])
            ):
                with patch("hiveweave.config.settings", FakeSettings()):
                    out = await svc.ensure_channel_models()

    up.assert_not_called()
    assert out["ensured"] == []
