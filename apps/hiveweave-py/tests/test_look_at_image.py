"""look_at_image (帮你看图片) — unit tests."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import hiveweave.tools.vision_tools  # noqa: F401 — register tool
import pytest

from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
)
from hiveweave.services.policy import TOOL_CAPABILITY, policy_service
from hiveweave.services.vision import extract_nonstream_text
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.vision_tools import LookAtImageParams, look_at_image_tool


def _tiny_png(path: Path) -> None:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    path.write_bytes(raw)


def test_look_at_image_registered() -> None:
    td = get_tool_def("look_at_image")
    assert td is not None
    assert "帮你看图片" in (td.description or "")


def test_look_at_image_in_all_role_presets() -> None:
    assert "look_at_image" in CEO_TOOLS
    assert "look_at_image" in COORDINATOR_BUILDER_TOOLS
    assert "look_at_image" in HR_TOOLS
    assert "look_at_image" in READONLY_TOOLS


def test_look_at_image_not_bound_to_browse_capability() -> None:
    """Must stay out of TOOL_CAPABILITY so HR (no BROWSE) can still call it."""
    assert "look_at_image" not in TOOL_CAPABILITY
    # Contrast: assert_visual IS gated — regression lock for the design choice.
    assert "assert_visual" in TOOL_CAPABILITY


def test_look_at_image_hard_check_allows_ceo_and_hr() -> None:
    ceo = {"role": "ceo", "permission_type": "readonly", "name": "归零"}
    hr = {"role": "hr", "permission_type": "readonly", "name": "知远"}
    assert policy_service.hard_check(ceo, "look_at_image") is None
    assert policy_service.hard_check(hr, "look_at_image") is None
    # CEO can browse to look; HR still cannot.
    assert policy_service.hard_check(ceo, "browse") is None
    assert policy_service.hard_check(hr, "browse") is not None


def test_extract_nonstream_text_openai() -> None:
    assert (
        extract_nonstream_text(
            {"choices": [{"message": {"content": "a cat"}}]}
        )
        == "a cat"
    )


def test_extract_nonstream_text_anthropic() -> None:
    assert (
        extract_nonstream_text(
            {"content": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]}
        )
        == "hello world"
    )


def test_extract_nonstream_text_reasoning_fallback() -> None:
    assert (
        extract_nonstream_text(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "I see a red button.",
                        }
                    }
                ]
            }
        )
        == "I see a red button."
    )


@pytest.mark.asyncio
async def test_look_at_image_rejects_empty_prompt(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    _tiny_png(img)
    result = await look_at_image_tool(
        LookAtImageParams(image_path=str(img.name), prompt="  "),
        agent_id="a1",
        workspace=str(tmp_path),
    )
    assert result.success is False
    assert "prompt" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_look_at_image_path_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    img = outside / "secret.png"
    _tiny_png(img)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = await look_at_image_tool(
        LookAtImageParams(image_path=str(img), prompt="describe"),
        agent_id="a1",
        workspace=str(ws),
    )
    assert result.success is False
    assert "workspace" in (result.error or "").lower() or "Invalid" in (result.error or "")


@pytest.mark.asyncio
async def test_look_at_image_missing_vision_model(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    _tiny_png(img)
    with patch(
        "hiveweave.tools.vision_tools.ModelService"
    ) as MockSvc:
        MockSvc.return_value.resolve_vision_model = AsyncMock(return_value=None)
        result = await look_at_image_tool(
            LookAtImageParams(image_path="shot.png", prompt="what do you see?"),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    assert "no model" in (result.error or "").lower() or "optional" in (
        result.error or ""
    ).lower()


@pytest.mark.asyncio
async def test_look_at_image_success(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    _tiny_png(img)
    fake_model = {
        "id": "m1",
        "name": "Vision-X",
        "model_id": "vision-x",
        "base_url": "https://example.com/v1",
        "api_key": "sk-test",
        "is_active": True,
    }
    with (
        patch(
            "hiveweave.tools.vision_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.vision_tools.analyze_image",
            new_callable=AsyncMock,
            return_value="I see a 1x1 pixel image.",
        ) as mock_analyze,
    ):
        MockSvc.return_value.resolve_vision_model = AsyncMock(return_value=fake_model)
        result = await look_at_image_tool(
            LookAtImageParams(image_path="shot.png", prompt="Describe briefly."),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is True
    assert "1x1" in result.output
    assert result.extra.get("model_name") == "Vision-X"
    mock_analyze.assert_awaited_once()
    call_kw = mock_analyze.await_args.kwargs
    assert call_kw["prompt"] == "Describe briefly."
    assert call_kw["image"]["media_type"] == "image/png"
    assert call_kw["model_config"]["id"] == "m1"


@pytest.mark.asyncio
async def test_look_at_image_reports_failure_without_switching(tmp_path: Path) -> None:
    """主模型失败即如实上报 —— 自动切换备用视觉模型已移除（对标 DSH）。

    静默换模型会掩盖真实故障并把结果归因到错误的模型上；且换 model =
    换缓存域。即便存在可用的、不同 api_key 的备用模型也不再调用。
    """
    img = tmp_path / "shot.png"
    _tiny_png(img)
    primary = {
        "id": "p1",
        "name": "Vision-Primary",
        "model_id": "v-p",
        "api_key": "sk-a",
        "is_active": True,
    }
    backup = {
        "id": "b1",
        "name": "Vision-Backup",
        "model_id": "v-b",
        "api_key": "sk-b",
        "is_active": True,
    }

    async def _resolve(skip_model_ids=None):
        skip = skip_model_ids or set()
        if "p1" not in skip:
            return primary
        if "b1" not in skip:
            return backup
        return None

    with (
        patch(
            "hiveweave.tools.vision_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.vision_tools.analyze_image",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("429 rate limit"), "backup saw a cat"],
        ) as mock_analyze,
    ):
        MockSvc.return_value.resolve_vision_model = AsyncMock(side_effect=_resolve)
        result = await look_at_image_tool(
            LookAtImageParams(image_path="shot.png", prompt="what?"),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    # 失败归因到实际调用的模型，并带上原始错误
    assert "Vision-Primary" in (result.error or "")
    assert "429 rate limit" in (result.error or "")
    # 关键断言：备用模型从未被调用（旧 failover 行为会是 2）
    mock_analyze.assert_awaited_once()


@pytest.mark.asyncio
async def test_look_at_image_same_key_backup_also_unused(tmp_path: Path) -> None:
    """备用与主模型同 api_key 时同样不被调用（切换整体已移除）。"""
    img = tmp_path / "shot.png"
    _tiny_png(img)
    primary = {
        "id": "p1",
        "name": "Vision-Primary",
        "model_id": "v-p",
        "api_key": "sk-same",
        "is_active": True,
    }
    backup = {
        "id": "b1",
        "name": "Vision-Backup",
        "model_id": "v-b",
        "api_key": "sk-same",
        "is_active": True,
    }

    async def _resolve(skip_model_ids=None):
        skip = skip_model_ids or set()
        if "p1" not in skip:
            return primary
        return backup

    with (
        patch(
            "hiveweave.tools.vision_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.vision_tools.analyze_image",
            new_callable=AsyncMock,
            side_effect=RuntimeError("429"),
        ) as mock_analyze,
    ):
        MockSvc.return_value.resolve_vision_model = AsyncMock(side_effect=_resolve)
        result = await look_at_image_tool(
            LookAtImageParams(image_path="shot.png", prompt="what?"),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    # same-api-key 守卫随切换一并移除：现在根本不解析备用模型
    assert "Vision-Primary" in (result.error or "")
    assert "429" in (result.error or "")
    mock_analyze.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_vision_model_primary_then_backup() -> None:
    from hiveweave.services.model import ModelService

    primary = {"id": "p1", "name": "P", "is_active": True}
    backup = {"id": "b1", "name": "B", "is_active": True}

    svc = ModelService()
    with (
        patch("hiveweave.services.settings.SettingsService") as MockSettings,
        patch.object(svc, "get", new_callable=AsyncMock) as mock_get,
    ):
        settings = MagicMock()
        settings.get = AsyncMock(
            side_effect=lambda k: {
                "vision_model_primary": "p1",
                "vision_model_backup": "b1",
            }.get(k)
        )
        MockSettings.return_value = settings
        mock_get.side_effect = lambda mid: {"p1": primary, "b1": backup}.get(mid)

        got = await svc.resolve_vision_model()
        assert got == primary

        got_backup = await svc.resolve_vision_model(skip_model_ids={"p1"})
        assert got_backup == backup


@pytest.mark.asyncio
async def test_analyze_image_disables_thinking() -> None:
    from hiveweave.services.vision import analyze_image

    fake_provider = MagicMock()
    fake_provider.build_body.return_value = {"model": "x", "stream": False}
    fake_provider.build_headers.return_value = {}
    fake_provider.build_url.return_value = "https://example.com/v1/chat/completions"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok pixels"}}]
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with (
        patch("hiveweave.llm.provider.provider_factory") as mock_factory,
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        mock_factory.create.return_value = fake_provider
        text = await analyze_image(
            image={"media_type": "image/png", "data": "QQ=="},
            prompt="see?",
            model_config={
                "id": "m1",
                "model_id": "vision",
                "base_url": "https://example.com/v1",
                "api_key": "sk",
                "supports_thinking": True,
                "default_reasoning_effort": "high",
            },
        )

    assert text == "ok pixels"
    created_cfg = mock_factory.create.call_args[0][0]
    assert created_cfg["supports_thinking"] is False
    assert created_cfg.get("default_reasoning_effort") is None
    body_kw = fake_provider.build_body.call_args.kwargs
    assert body_kw["stream"] is False
    assert body_kw["messages"][0]["images"]


@pytest.mark.asyncio
async def test_analyze_image_retries_on_429() -> None:
    """视觉一次性调用遇到 429 必须指数退避重试（与流式路径同口径），
    不能直接抛掉把视觉门禁废掉 → 团队只能 waive visual/module_visual。"""
    from hiveweave.services.vision import analyze_image

    fake_provider = MagicMock()
    fake_provider.build_body.return_value = {"model": "x", "stream": False}
    fake_provider.build_headers.return_value = {}
    fake_provider.build_url.return_value = "https://example.com/v1/chat/completions"

    def make_resp(status: int, payload: dict):
        r = MagicMock()
        r.status_code = status
        r.headers = {"retry-after-ms": "0"} if status == 429 else {}
        r.raise_for_status = MagicMock()
        r.json.return_value = payload
        r.text = "rate limit" if status == 429 else ""
        return r

    resp_429 = make_resp(429, {})
    resp_ok = make_resp(
        200, {"choices": [{"message": {"content": "ok after retry"}}]}
    )

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=[resp_429, resp_ok])

    with (
        patch("hiveweave.llm.provider.provider_factory") as mock_factory,
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        mock_factory.create.return_value = fake_provider
        text = await analyze_image(
            image={"media_type": "image/png", "data": "QQ=="},
            prompt="see?",
            model_config={
                "id": "m1",
                "model_id": "vision",
                "base_url": "https://example.com/v1",
                "api_key": "sk",
            },
        )

    assert text == "ok after retry"
    assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_resolve_vision_model_falls_back_to_management() -> None:
    from hiveweave.services.model import ModelService

    svc = ModelService()
    mgmt = {"id": "mgmt-1", "name": "muse-spark", "is_active": True}

    async def _settings_get(self, key: str, *a, **k):
        return ""

    mock_resolve = AsyncMock(return_value=mgmt)
    with (
        patch(
            "hiveweave.services.settings.SettingsService.get",
            new=_settings_get,
        ),
        patch.object(svc, "resolve_model", new=mock_resolve),
    ):
        got = await svc.resolve_vision_model()
    assert got is mgmt
    # strict=True：management 档解析不出就返回 None，不落 emergency pool
    # 把 executor 档模型冒充 management（审计 M2）。
    assert mock_resolve.await_args.kwargs.get("strict") is True


@pytest.mark.asyncio
async def test_resolve_vision_model_management_none_beats_pool() -> None:
    """专用槽空 + management 严格解析为 None → 返回 None（不用任意池模型）。"""
    from hiveweave.services.model import ModelService

    svc = ModelService()

    async def _settings_get(self, key: str, *a, **k):
        return ""

    mock_resolve = AsyncMock(return_value=None)
    with (
        patch(
            "hiveweave.services.settings.SettingsService.get",
            new=_settings_get,
        ),
        patch.object(svc, "resolve_model", new=mock_resolve),
    ):
        got = await svc.resolve_vision_model()
    assert got is None
    assert mock_resolve.await_args.kwargs.get("strict") is True


@pytest.mark.asyncio
async def test_resolve_vision_model_dedicated_slot_wins() -> None:
    from hiveweave.services.model import ModelService

    svc = ModelService()
    vision = {"id": "v1", "name": "vision-box", "is_active": True}
    mgmt = {"id": "mgmt-1", "name": "muse-spark", "is_active": True}

    async def _settings_get(self, key: str, *a, **k):
        if key == "vision_model_primary":
            return "v1"
        return ""

    async def _get(model_pk: str):
        if model_pk == "v1":
            return vision
        return None

    mock_resolve = AsyncMock(return_value=mgmt)
    with (
        patch(
            "hiveweave.services.settings.SettingsService.get",
            new=_settings_get,
        ),
        patch.object(svc, "get", new=_get),
        patch.object(svc, "resolve_model", new=mock_resolve),
    ):
        got = await svc.resolve_vision_model()
    assert got is vision
    mock_resolve.assert_not_awaited()


def test_ui_submit_gate_does_not_require_assert_visual_ritual() -> None:
    from hiveweave.services.attestation import (
        BROWSE_E2E_KIND,
        CODE_AUDIT_KIND,
        POLICY_REQUIRED_KINDS,
        VISUAL_CHECK_KIND,
    )

    ui = POLICY_REQUIRED_KINDS["ui_browser_e2e"]
    vis = POLICY_REQUIRED_KINDS["code_audit_visual"]
    assert ui is not None and vis is not None
    assert VISUAL_CHECK_KIND not in ui
    assert BROWSE_E2E_KIND in ui
    assert VISUAL_CHECK_KIND not in vis
    assert CODE_AUDIT_KIND in vis
    assert BROWSE_E2E_KIND in vis
