"""browse viewport command + goto desktop reset."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import hiveweave.tools.browse_tools as bt
from hiveweave.tools.browse_tools import (
    DEFAULT_VIEWPORT,
    VIEWPORT_USAGE,
    BrowseParams,
    _map_ab_argv,
    browse_tool,
    parse_viewport_args,
)


def test_parse_viewport_pair():
    assert parse_viewport_args(["390", "844"]) == (390, 844, None)


def test_parse_viewport_x_form():
    assert parse_viewport_args(["390x844"]) == (390, 844, None)


def test_parse_viewport_with_scale():
    assert parse_viewport_args(["1280", "900", "2"]) == (1280, 900, 2)


def test_parse_viewport_rejects_unknown():
    assert parse_viewport_args(["390"]) is None
    assert parse_viewport_args(["wide", "tall"]) is None
    assert parse_viewport_args(["12", "12"]) is None  # below min 32


def test_map_viewport_head_to_set_viewport():
    argv, stdin = _map_ab_argv(["viewport", "390", "844"], "")
    assert argv == ["set", "viewport", "390", "844"]
    assert stdin is None


def test_map_set_viewport_passthrough():
    argv, stdin = _map_ab_argv(["set", "viewport", "1280", "900"], "")
    assert argv == ["set", "viewport", "1280", "900"]
    assert stdin is None


@pytest.mark.asyncio
async def test_viewport_command_does_not_stamp(tmp_path):
    seen: list[list[str]] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        seen.append(list(argv))
        return 0, "ok", ""

    stamped = []

    async def fake_create(*_a, **_k):
        stamped.append(1)
        return "att"

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            fake_create,
        ),
    ):
        result = await browse_tool(
            BrowseParams(args=["viewport", "390", "844"]),
            "qa-1",
            str(tmp_path),
        )

    assert result.success is True
    assert seen == [["viewport", "390", "844"]]
    assert "390×844" in (result.output or "")
    assert stamped == []


@pytest.mark.asyncio
async def test_viewport_rejects_bad_args(tmp_path):
    with patch.object(bt, "resolve_browse_bin", return_value="fake-ab"):
        result = await browse_tool(
            BrowseParams(args=["viewport", "nope"]),
            "qa-1",
            str(tmp_path),
        )
    assert result.success is False
    assert VIEWPORT_USAGE.split(".")[0] in (result.error or "")


@pytest.mark.asyncio
async def test_goto_resets_desktop_viewport(tmp_path):
    seen: list[list[str]] = []

    async def fake_exec(argv, workspace, timeout_sec=60, agent_id=None):
        seen.append(list(argv))
        return 0, "opened", ""

    with (
        patch.object(bt, "browse_exec", fake_exec),
        patch.object(bt, "resolve_browse_bin", return_value="fake-ab"),
        patch("hiveweave.tools.helpers.get_project_id", AsyncMock(return_value=None)),
    ):
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:3000"]),
            "qa-1",
            str(tmp_path),
        )

    assert result.success is True
    assert seen[0] == ["goto", "http://127.0.0.1:3000"]
    assert seen[1] == [
        "set",
        "viewport",
        str(DEFAULT_VIEWPORT[0]),
        str(DEFAULT_VIEWPORT[1]),
    ]
    assert "1280×900" in (result.output or "")
    assert "desktop default" in (result.output or "")
