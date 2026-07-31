"""generate_image (Seedream) — unit tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import hiveweave.tools.image_gen_tools  # noqa: F401 — register tool
import pytest

from hiveweave.services.ark_media import (
    images_generations_url,
    is_agent_plan_root,
    normalize_plan_root,
)
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
)
from hiveweave.services.policy import TOOL_CAPABILITY, policy_service
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.image_gen_tools import GenerateImageParams, generate_image_tool


PLAN_ROOT = "https://ark.cn-beijing.volces.com/api/plan/v3"


def test_normalize_plan_root_strips_images_suffix() -> None:
    assert (
        normalize_plan_root(f"{PLAN_ROOT}/images/generations") == PLAN_ROOT
    )
    assert normalize_plan_root(PLAN_ROOT) == PLAN_ROOT
    assert images_generations_url(PLAN_ROOT) == f"{PLAN_ROOT}/images/generations"
    assert (
        images_generations_url(f"{PLAN_ROOT}/images/generations")
        == f"{PLAN_ROOT}/images/generations"
    )


def test_reject_non_plan_roots() -> None:
    assert not is_agent_plan_root(
        "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert not is_agent_plan_root(
        "https://ark.cn-beijing.volces.com/api/coding/v3"
    )
    assert images_generations_url(
        "https://ark.cn-beijing.volces.com/api/v3"
    ) is None
    assert images_generations_url(
        "https://ark.cn-beijing.volces.com/api/coding/v3"
    ) is None
    assert is_agent_plan_root(PLAN_ROOT)


def test_generate_image_registered() -> None:
    td = get_tool_def("generate_image")
    assert td is not None
    assert "Seedream" in (td.description or "")


def test_generate_image_in_source_write_presets_only() -> None:
    assert "generate_image" in COORDINATOR_BUILDER_TOOLS
    assert "generate_image" in READONLY_TOOLS
    assert "generate_image" not in CEO_TOOLS
    assert "generate_image" not in HR_TOOLS


def test_generate_image_requires_source_write() -> None:
    assert TOOL_CAPABILITY["generate_image"]  # non-empty
    assert "source_write" in {c.value for c in TOOL_CAPABILITY["generate_image"]}

    ceo = {"role": "ceo", "permission_type": "readonly", "name": "归零"}
    hr = {"role": "hr", "permission_type": "readonly", "name": "知远"}
    executor = {
        "role": "前端模块工程师",
        "permission_type": "executor",
        "name": "拾光",
    }
    qa = {
        "role": "游戏测试工程师",
        "permission_type": "executor",
        "name": "Echo",
    }
    assert policy_service.hard_check(ceo, "generate_image") is not None
    assert policy_service.hard_check(hr, "generate_image") is not None
    assert policy_service.hard_check(executor, "generate_image") is None
    assert policy_service.hard_check(qa, "generate_image") is None


@pytest.mark.asyncio
async def test_generate_image_rejects_empty_prompt(tmp_path: Path) -> None:
    result = await generate_image_tool(
        GenerateImageParams(prompt="  "),
        agent_id="a1",
        workspace=str(tmp_path),
    )
    assert result.success is False
    assert "prompt" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_generate_image_missing_config(tmp_path: Path) -> None:
    with patch(
        "hiveweave.tools.image_gen_tools.ModelService"
    ) as MockSvc:
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=None)
        result = await generate_image_tool(
            GenerateImageParams(prompt="a cat"),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    assert "生图" in (result.error or "") or "image" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_generate_image_rejects_coding_base_url(tmp_path: Path) -> None:
    fake = {
        "id": "m1",
        "name": "Wrong",
        "model_id": "doubao-seedream-5.0-lite",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "api_key": "ark-test",
        "is_active": True,
    }
    with patch(
        "hiveweave.tools.image_gen_tools.ModelService"
    ) as MockSvc:
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=fake)
        result = await generate_image_tool(
            GenerateImageParams(prompt="x"),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    assert "Plan" in (result.error or "") or "plan" in (result.error or "")


@pytest.mark.asyncio
async def test_generate_image_path_escape(tmp_path: Path) -> None:
    fake = {
        "id": "m1",
        "name": "Seedream",
        "model_id": "doubao-seedream-5.0-lite",
        "base_url": PLAN_ROOT,
        "api_key": "ark-test",
        "is_active": True,
    }
    with patch(
        "hiveweave.tools.image_gen_tools.ModelService"
    ) as MockSvc:
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=fake)
        result = await generate_image_tool(
            GenerateImageParams(
                prompt="x",
                output_path="../outside.png",
            ),
            agent_id="a1",
            workspace=str(tmp_path),
        )
    assert result.success is False
    assert "out-of-workspace" in (result.error or "") or "Invalid" in (
        result.error or ""
    )


@pytest.mark.asyncio
async def test_generate_image_success(tmp_path: Path) -> None:
    fake = {
        "id": "m1",
        "name": "Seedream",
        "model_id": "doubao-seedream-5.0-lite",
        "base_url": PLAN_ROOT,
        "api_key": "ark-test",
        "is_active": True,
    }
    png_bytes = b"\x89PNG\r\n\x1a\nfake"

    gen_resp = MagicMock()
    gen_resp.status_code = 200
    gen_resp.json.return_value = {
        "data": [{"url": "https://cdn.example/img.png"}]
    }
    gen_resp.text = ""

    # Streamed download response
    async def _aiter_bytes():
        yield png_bytes

    dl_resp = MagicMock()
    dl_resp.status_code = 200
    dl_resp.is_redirect = False
    dl_resp.headers = {}
    dl_resp.aiter_bytes = _aiter_bytes

    post_client = MagicMock()
    post_client.post = AsyncMock(return_value=gen_resp)
    post_client.__aenter__ = AsyncMock(return_value=post_client)
    post_client.__aexit__ = AsyncMock(return_value=None)

    get_client = MagicMock()
    get_client.get = AsyncMock(return_value=dl_resp)
    get_client.__aenter__ = AsyncMock(return_value=get_client)
    get_client.__aexit__ = AsyncMock(return_value=None)

    clients = iter([post_client, get_client])

    with (
        patch(
            "hiveweave.tools.image_gen_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.image_gen_tools.httpx.AsyncClient",
            side_effect=lambda **_kw: next(clients),
        ),
    ):
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=fake)
        result = await generate_image_tool(
            GenerateImageParams(
                prompt="vogue portrait",
                output_path=".hiveweave/generated/test.png",
            ),
            agent_id="a1",
            workspace=str(tmp_path),
        )

    assert result.success is True, result.error
    assert result.extra.get("path") == ".hiveweave/generated/test.png"
    assert "url" not in result.extra  # signed CDN URL omitted from history
    saved = tmp_path / ".hiveweave" / "generated" / "test.png"
    assert saved.is_file()
    assert saved.read_bytes() == png_bytes
    post_url = post_client.post.await_args.args[0]
    assert post_url.endswith("/images/generations")
    assert "/plan/" in post_url


@pytest.mark.asyncio
async def test_generate_image_blocks_ssrf_download(tmp_path: Path) -> None:
    fake = {
        "id": "m1",
        "name": "Seedream",
        "model_id": "doubao-seedream-5.0-lite",
        "base_url": PLAN_ROOT,
        "api_key": "ark-test",
        "is_active": True,
    }
    gen_resp = MagicMock()
    gen_resp.status_code = 200
    gen_resp.json.return_value = {
        "data": [{"url": "http://127.0.0.1/secret.png"}]
    }
    gen_resp.text = ""

    post_client = MagicMock()
    post_client.post = AsyncMock(return_value=gen_resp)
    post_client.__aenter__ = AsyncMock(return_value=post_client)
    post_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "hiveweave.tools.image_gen_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.image_gen_tools.httpx.AsyncClient",
            return_value=post_client,
        ),
    ):
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=fake)
        result = await generate_image_tool(
            GenerateImageParams(prompt="x"),
            agent_id="a1",
            workspace=str(tmp_path),
        )

    assert result.success is False
    assert "blocked" in (result.error or "").lower() or "internal" in (
        result.error or ""
    ).lower()


@pytest.mark.asyncio
async def test_generate_image_http_error(tmp_path: Path) -> None:
    fake = {
        "id": "m1",
        "name": "Seedream",
        "model_id": "doubao-seedream-5.0-lite",
        "base_url": PLAN_ROOT,
        "api_key": "ark-test",
        "is_active": True,
    }
    gen_resp = MagicMock()
    gen_resp.status_code = 401
    gen_resp.text = '{"error":{"message":"unauthorized"}}'
    gen_resp.json.return_value = {"error": {"message": "unauthorized"}}

    client = MagicMock()
    client.post = AsyncMock(return_value=gen_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "hiveweave.tools.image_gen_tools.ModelService"
        ) as MockSvc,
        patch(
            "hiveweave.tools.image_gen_tools.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        MockSvc.return_value.resolve_image_gen_model = AsyncMock(return_value=fake)
        result = await generate_image_tool(
            GenerateImageParams(prompt="x"),
            agent_id="a1",
            workspace=str(tmp_path),
        )

    assert result.success is False
    assert "401" in (result.error or "")
