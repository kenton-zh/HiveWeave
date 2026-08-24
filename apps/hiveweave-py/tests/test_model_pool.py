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
            with patch.object(
                svc, "_is_tombstoned", new=AsyncMock(return_value=False)
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
            with patch.object(
                svc, "_is_tombstoned", new=AsyncMock(return_value=False)
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
                with patch.object(
                    svc, "_is_tombstoned", new=AsyncMock(return_value=False)
                ):
                    with patch("hiveweave.config.settings", FakeSettings()):
                        out = await svc.ensure_channel_models()

    up.assert_not_called()
    assert out["ensured"] == []


# ── Tombstone：删除即永久（2026-08-24）────────────────────────
# UI 删除渠道模型后，启动 ensure_channel_models 不得再按 .env 配置重建。
# delete 写 tombstone → _is_tombstoned 返回 True → ensure 跳过；create 撤销。


@pytest.mark.asyncio
async def test_delete_writes_tombstone():
    """delete 硬删行后把渠道名写入 global_settings tombstone。"""
    svc = ModelService()
    plan_name = "DeepSeek V4 Flash (ARK Plan)"
    model = {"id": "m1", "name": plan_name, "model_id": "deepseek-v4-flash"}
    set_calls: list[tuple[str, str]] = []

    class FakeSettingsSvc:
        async def set(self, key, value):
            set_calls.append((key, str(value)))
            return str(value)

    with patch.object(svc, "get", new=AsyncMock(return_value=model)):
        with patch(
            "hiveweave.services.settings.SettingsService", FakeSettingsSvc
        ):
            with patch(
                "hiveweave.services.model.meta_db.execute", new=AsyncMock()
            ):
                await svc.delete("m1")

    assert set_calls[0][0] == f"model_tombstone:{plan_name}"


@pytest.mark.asyncio
async def test_ensure_skips_tombstoned_channel():
    """tombstone 渠道在 ensure_channel_models 里被跳过（不再重建）。"""
    svc = ModelService()
    upserts: list[dict] = []

    async def fake_upsert(attrs: dict) -> dict:
        upserts.append(attrs)
        return {"id": "id-1", **attrs}

    class FakeSettings:
        ark_api_key = "plan-key"
        ark_base_url = "https://ark.example/api/plan/v3"
        ark_model_id = "model-x"
        ark_coding_api_key = ""
        ark_coding_base_url = ""
        ark_coding_model_id = ""

    async def fake_is_tombstoned(name: str) -> bool:
        return name == "DeepSeek V4 Flash (ARK Plan)"

    with patch.object(svc, "upsert_by_name", side_effect=fake_upsert):
        with patch.object(svc, "list_active", new=AsyncMock(return_value=[])):
            with patch.object(
                svc, "_is_tombstoned", side_effect=fake_is_tombstoned
            ):
                with patch("hiveweave.config.settings", FakeSettings()):
                    out = await svc.ensure_channel_models()

    assert upserts == []
    assert out["ensured"] == []


@pytest.mark.asyncio
async def test_create_keeps_tombstone():
    """服务层 create 不清 tombstone——防止 seed 删光后复活并永久清标记（S1）。

    清除 tombstone 只发生在 API 层用户手动创建入口（撤销删除）。
    """
    svc = ModelService()
    name = "DeepSeek V4 Flash (ARK Plan)"
    delete_keys: list[str] = []

    class FakeSettingsSvc:
        async def delete(self, key):
            delete_keys.append(key)

    async def fake_execute(*_args, **_kwargs):
        return None

    with patch(
        "hiveweave.services.settings.SettingsService", FakeSettingsSvc
    ):
        with patch(
            "hiveweave.services.model.meta_db.execute", side_effect=fake_execute
        ):
            await svc.create(
                {
                    "name": name,
                    "model_id": "deepseek-v4-flash",
                    "base_url": "https://ark.example/api/plan/v3",
                    "api_key": "k",
                    "provider_type": "openai-compatible",
                    "is_active": True,
                }
            )

    assert delete_keys == []


@pytest.mark.asyncio
async def test_delete_renamed_channel_not_tombstoned():
    """改名后的模型不再命中渠道名集合 → 普通删除，不落 tombstone（B1）。"""
    svc = ModelService()
    # 用户把渠道模型改名后再删除 —— DB name 已不等于渠道常量名
    model = {"id": "m1", "name": "我的ARK", "model_id": "deepseek-v4-flash"}
    set_calls: list[tuple[str, str]] = []

    class FakeSettingsSvc:
        async def set(self, key, value):
            set_calls.append((key, str(value)))
            return str(value)

    with patch.object(svc, "get", new=AsyncMock(return_value=model)):
        with patch(
            "hiveweave.services.settings.SettingsService", FakeSettingsSvc
        ):
            with patch(
                "hiveweave.services.model.meta_db.execute", new=AsyncMock()
            ):
                await svc.delete("m1")

    assert set_calls == []


@pytest.mark.asyncio
async def test_seed_step_skips_tombstoned():
    """Step 渠道同样受 tombstone 保护，删除后 seed 不再重建（B2）。"""
    svc = ModelService()

    class FakeSettingsSvc:
        async def get(self, key):
            return None

    async def fake_is_tombstoned(name: str) -> bool:
        return name == svc._STEP_NAME

    with patch("hiveweave.services.settings.SettingsService", FakeSettingsSvc):
        with patch.object(svc, "_is_tombstoned", side_effect=fake_is_tombstoned):
            with patch.object(svc, "list_active", new=AsyncMock(return_value=[])):
                with patch(
                    "hiveweave.services.model.meta_db.query_one",
                    new=AsyncMock(return_value={"cnt": 0}),
                ):
                    with patch.dict(
                        "os.environ", {"STEP_API_KEY": "step-key"}, clear=False
                    ):
                        with patch.object(
                            svc, "create", new=AsyncMock()
                        ) as create:
                            out = await svc.seed_default_model()

    create.assert_not_called()
    assert out == {"error": "no_api_key"}
