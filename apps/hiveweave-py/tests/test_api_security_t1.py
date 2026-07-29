"""T1#6/#7/#8: REST org gate + filesystem .hiveweave + api_key mask."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from hiveweave.api.filesystem import _reject_protected_hiveweave
from hiveweave.api.models import _mask_api_key, _model_response
from hiveweave.api.org import _PRIVILEGE_UPDATE_KEYS, _require_org_actor


def test_mask_api_key_hides_secret():
    assert _mask_api_key(None) is None
    assert _mask_api_key("") == ""
    assert _mask_api_key("abcd") == "****"
    assert _mask_api_key("sk-secret-key-1234") == ("*" * 14) + "1234"
    assert "sk-secret" not in (_mask_api_key("sk-secret-key-1234") or "")


def test_model_response_always_masks_key():
    raw = {
        "id": "m1",
        "name": "demo",
        "model_id": "demo-model",
        "api_key": "super-secret-api-key-xyz9",
        "base_url": "https://example.com",
    }
    out = _model_response(raw)
    assert out["api_key"] != raw["api_key"]
    assert out["apiKey"] == out["api_key"]
    assert out["api_key"].endswith("xyz9")
    assert "super-secret" not in out["api_key"]


def test_reject_protected_hiveweave_blocks_data_db(tmp_path: Path):
    ws = tmp_path
    (ws / ".hiveweave").mkdir()
    target = ws / ".hiveweave" / "data.db"
    target.write_text("x")
    with pytest.raises(HTTPException) as ei:
        _reject_protected_hiveweave(str(ws), target)
    assert ei.value.status_code == 403


def test_reject_protected_hiveweave_allows_shared(tmp_path: Path):
    ws = tmp_path
    shared = ws / ".hiveweave" / "shared" / "note.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("ok")
    _reject_protected_hiveweave(str(ws), shared)  # no raise


def test_privilege_update_keys_cover_escalation_fields():
    assert "permission_type" in _PRIVILEGE_UPDATE_KEYS
    assert "permission_mode" in _PRIVILEGE_UPDATE_KEYS
    assert "parent_id" in _PRIVILEGE_UPDATE_KEYS
    assert "status" in _PRIVILEGE_UPDATE_KEYS


@pytest.mark.asyncio
async def test_require_org_actor_rejects_missing():
    with pytest.raises(HTTPException) as ei:
        await _require_org_actor(None, "dismiss_agent")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_require_org_actor_rejects_executor():
    executor = {
        "id": "exec-1",
        "role": "frontend engineer",
        "permission_type": "executor",
        "name": "工蜂",
    }
    with patch("hiveweave.api.org._org") as org:
        org.resolve_agent = AsyncMock(return_value=executor)
        with pytest.raises(HTTPException) as ei:
            await _require_org_actor("exec-1", "dismiss_agent")
        assert ei.value.status_code == 403
