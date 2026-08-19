"""Vision injection + assert_visual gate tests."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hiveweave.tools.browse_tools  # noqa: F401 — register tools
import pytest

from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.provider import AnthropicHandler, OpenAIHandler
from hiveweave.services.attestation import (
    BROWSE_E2E_KIND,
    VISUAL_CHECK_KIND,
    AttestationService,
    required_attestation_kinds,
)
from hiveweave.services.permission import READONLY_TOOLS
from hiveweave.services.vision import (
    load_image_for_llm,
    messages_without_images,
    resolve_screenshot_path,
    strip_images_from_messages,
)
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.browse_tools import (
    AssertVisualParams,
    _screenshot_path_from_argv,
    assert_visual_tool,
)
from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS


def _tiny_png(path: Path) -> None:
    # 1x1 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    path.write_bytes(raw)


def test_screenshot_path_from_argv():
    assert _screenshot_path_from_argv(["screenshot", "evidence/a.png"]) == "evidence/a.png"
    assert _screenshot_path_from_argv(["screenshot"]) is None
    assert _screenshot_path_from_argv(["goto", "http://x"]) is None


@pytest.mark.asyncio
async def test_browse_screenshot_success_text_includes_abs_path(tmp_path: Path, monkeypatch):
    """Regression: screenshot_path extra field is dropped by tool_exec — the
    injected-text note MUST carry the path so the next turn still knows the file.
    """
    from hiveweave.tools.browse_tools import browse_tool

    ws = tmp_path / "ws"
    ws.mkdir()
    png = ws / "evidence" / "flow.png"
    png.parent.mkdir()
    _tiny_png(png)

    args = ["screenshot", "evidence/flow.png"]

    async def fake_browse_exec(argv, workspace, timeout_sec=60, agent_id=None):
        return 0, "saved " + argv[-1], ""

    async def fake_attest(**kwargs):
        return ""

    import hiveweave.tools.browse_tools as bt
    from hiveweave.tools.browse_tools import BrowseParams

    monkeypatch.setattr(bt, "browse_exec", fake_browse_exec)
    monkeypatch.setattr(bt, "issue_browse_e2e_attestation", fake_attest)

    result = await browse_tool(
        BrowseParams(args=args), agent_id="agent-1", workspace=str(ws)
    )
    assert result.success is True
    text = result.output
    rel = "evidence/flow.png"
    assert rel in text, text
    assert "assert_visual(" not in text
    assert "pixels attached" in text.lower() or "[VISION]" in text
    assert result.extra.get("images")


def test_load_image_for_llm(tmp_path: Path):
    png = tmp_path / "shot.png"
    _tiny_png(png)
    img = load_image_for_llm(png)
    assert img is not None
    assert img["media_type"] == "image/png"
    assert img["data"]
    assert load_image_for_llm(tmp_path / "missing.png") is None


def test_resolve_screenshot_path_sandboxed(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    inside = ws / "evidence" / "a.png"
    inside.parent.mkdir()
    _tiny_png(inside)
    assert resolve_screenshot_path(str(ws), "evidence/a.png") == inside.resolve()

    outside = tmp_path / "secret.png"
    _tiny_png(outside)
    assert resolve_screenshot_path(str(ws), str(outside)) is None
    assert resolve_screenshot_path(str(ws), "../secret.png") is None


def test_strip_images_keeps_last_two():
    msgs = [
        {"role": "tool", "content": "a", "images": [{"data": "1"}]},
        {"role": "tool", "content": "b", "images": [{"data": "2"}]},
        {"role": "tool", "content": "c", "images": [{"data": "3"}]},
    ]
    out = strip_images_from_messages(msgs, keep_last=2)
    assert "images" not in out[0]
    assert out[1].get("images")
    assert out[2].get("images")
    assert messages_without_images(out)[1].get("images") is None


def test_openai_normalizes_tool_images_to_user_turn():
    h = OpenAIHandler()
    msgs = [
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": "saved evidence/a.png",
            "images": [{"media_type": "image/png", "data": "AAAA"}],
        }
    ]
    out = h._normalize_messages_with_images(msgs)
    assert out[0]["role"] == "tool"
    assert "images" not in out[0]
    assert out[1]["role"] == "user"
    types = [p["type"] for p in out[1]["content"]]
    assert "image_url" in types


def test_openai_parallel_tools_images_after_full_tool_block():
    """P0: never insert user mid tool-block (Ark/OpenAI pairing)."""
    h = OpenAIHandler()
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{}, {}]},
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": "goto ok",
        },
        {
            "role": "tool",
            "tool_call_id": "t2",
            "content": "shot ok",
            "images": [{"media_type": "image/png", "data": "AAAA"}],
        },
        {
            "role": "tool",
            "tool_call_id": "t3",
            "content": "console ok",
        },
    ]
    out = h._normalize_messages_with_images(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "tool", "tool", "user"]
    assert all("images" not in m for m in out if m["role"] == "tool")
    assert any(
        p.get("type") == "image_url"
        for p in out[-1]["content"]
        if isinstance(p, dict)
    )


def test_estimate_tokens_counts_images():
    bare = estimate_tokens_for_messages([{"role": "tool", "content": "hi"}])
    with_img = estimate_tokens_for_messages([
        {
            "role": "tool",
            "content": "hi",
            "images": [{"data": "A" * 4000}],
        }
    ])
    assert with_img > bare + 100


def test_anthropic_tool_result_includes_image_blocks():
    h = AnthropicHandler()
    body = h.build_body(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "browse", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": "shot ok",
                "images": [{"media_type": "image/png", "data": "BBBB"}],
            },
        ],
        model_id="claude-test",
        stream=False,
        tools=None,
    )
    # Find tool_result content
    found_image = False
    for m in body["messages"]:
        for block in m.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    found_image = any(b.get("type") == "image" for b in content)
    assert found_image


def test_ui_policy_requires_browse_e2e_not_assert_visual_ritual():
    assert required_attestation_kinds("ui_browser_e2e") == frozenset(
        {BROWSE_E2E_KIND}
    )


def test_assert_visual_tool_registered():
    assert "assert_visual" in READONLY_TOOLS
    assert "assert_visual" in TOOL_PARAM_SCHEMAS
    assert get_tool_def("assert_visual") is not None


@pytest.mark.asyncio
async def test_assert_visual_rejects_short_observed(tmp_path: Path):
    png = tmp_path / "a.png"
    _tiny_png(png)
    result = await assert_visual_tool(
        AssertVisualParams(
            screenshot_path=str(png.name),
            observed="looks fine",
            verdict="pass",
        ),
        agent_id="agent-1",
        workspace=str(tmp_path),
    )
    assert result.success is False
    assert "observed" in (result.error or "").lower() or "chars" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_assert_visual_rejects_path_stub(tmp_path: Path):
    png = tmp_path / "flow.png"
    _tiny_png(png)
    # Long enough but only the basename — language-agnostic path stub
    result = await assert_visual_tool(
        AssertVisualParams(
            screenshot_path="flow.png",
            observed="flow.png flow.png flow.png flow.png flow.png",
            verdict="pass",
        ),
        agent_id="agent-1",
        workspace=str(tmp_path),
    )
    assert result.success is False
    assert "path" in (result.error or "").lower() or "stub" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_assert_visual_rejects_path_escape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "leak.png"
    _tiny_png(outside)
    result = await assert_visual_tool(
        AssertVisualParams(
            screenshot_path=str(outside),
            observed="Level select shows three cards and a Start button on the right side.",
            verdict="pass",
        ),
        agent_id="agent-1",
        workspace=str(ws),
    )
    assert result.success is False
    assert "workspace" in (result.error or "").lower() or "rejected" in (
        result.error or ""
    ).lower()


@pytest.mark.asyncio
async def test_assert_visual_rejects_missing_file(tmp_path: Path):
    result = await assert_visual_tool(
        AssertVisualParams(
            screenshot_path="missing.png",
            observed="Level select shows three cards and a Start button on the right side.",
            verdict="pass",
        ),
        agent_id="agent-1",
        workspace=str(tmp_path),
    )
    assert result.success is False
    assert "not found" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_verify_ids_rejects_visual_check_fail():
    svc = AttestationService()
    row = {
        "id": "att-fail",
        "kind": VISUAL_CHECK_KIND,
        "agent_id": "a1",
        "task_id": "t1",
        "exit_code": 1,
        "stdout_hash": "abc",
        "created_at": 9_999_999_999_000,
        "expires_at": 9_999_999_999_999,
    }
    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, return_value=row),
    ):
        ok, err = await svc.verify_ids(
            "p1",
            ["att-fail"],
            expected_kinds={VISUAL_CHECK_KIND},
            task_id="t1",
        )
    assert ok is False
    assert "fail" in err.lower() or "exit_code" in err.lower()


@pytest.mark.asyncio
async def test_verify_ids_requires_all_kinds():
    svc = AttestationService()

    async def _get(_pid, aid):
        if aid == "v1":
            return {
                "id": "v1",
                "kind": VISUAL_CHECK_KIND,
                "agent_id": "a1",
                "task_id": "t1",
                "exit_code": 0,
                "stdout_hash": "abc",
                "created_at": 9_999_999_999_000,
                "expires_at": 9_999_999_999_999,
            }
        return None

    with (
        patch.object(svc, "ensure_schema", new_callable=AsyncMock),
        patch.object(svc, "get", new_callable=AsyncMock, side_effect=_get),
    ):
        ok, err = await svc.verify_ids(
            "p1",
            ["v1"],
            expected_kinds={VISUAL_CHECK_KIND, BROWSE_E2E_KIND},
            task_id="t1",
        )
    assert ok is False
    assert "browse_e2e" in err.lower() or "missing" in err.lower()


def test_browse_skill_does_not_prescribe_assert_visual_ritual():
    from hiveweave.services.skill_registry import BUILTIN_SKILLS

    browse = next(s for s in BUILTIN_SKILLS if s["slug"] == "browse")
    text = browse["instructions"]
    assert "screenshot" in text.lower()
    assert "assert_visual" not in text
    assert "look_at_image" not in text
