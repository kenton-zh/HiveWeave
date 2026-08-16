"""browse restart/reset recycles the agent-browser session (close, then fresh)."""

from __future__ import annotations

import pytest

from hiveweave.tools.browse_tools import (
    BROWSE_RESTART_HINT,
    BROWSE_RESTART_OK,
    BrowseParams,
    _map_ab_argv,
    browse_exec,
    browse_tool,
)


def test_restart_alias_maps_to_close():
    argv, stdin = _map_ab_argv(["restart"], "")
    assert argv == ["close"]
    assert stdin is None


def test_reset_alias_maps_to_close():
    argv, stdin = _map_ab_argv(["reset"], "")
    assert argv == ["close"]
    assert stdin is None


@pytest.mark.asyncio
async def test_restart_runs_close_then_fresh_message(browse_fake_proc, tmp_path):
    with browse_fake_proc as ctx:
        ctx.out = b"closed"
        result = await browse_tool(
            BrowseParams(args=["restart"]),
            "agent-1",
            str(tmp_path),
        )
    assert result.success is True
    assert result.output == BROWSE_RESTART_OK
    assert "browser session closed" in result.output
    assert "starts fresh" in result.output


@pytest.mark.asyncio
async def test_timeout_hint_mentions_restart(browse_fake_proc, tmp_path, monkeypatch):
    async def fake_drain(*_a, **_k):
        return -1, b"", b""

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._run_and_drain", fake_drain
    )
    with browse_fake_proc:
        result = await browse_tool(
            BrowseParams(args=["goto", "http://127.0.0.1:3000"]),
            "agent-1",
            str(tmp_path),
        )
    assert result.success is False
    text = (result.error or "") + (result.output or "")
    assert "timed out" in text
    assert "restart" in text
    assert BROWSE_RESTART_HINT in text
    assert 'browse(["restart"])' in text


@pytest.mark.asyncio
async def test_browse_exec_timeout_appends_restart_hint(
    browse_fake_proc, tmp_path, monkeypatch
):
    async def fake_drain(*_a, **_k):
        return -1, b"", b""

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._run_and_drain", fake_drain
    )
    with browse_fake_proc:
        rc, _out, err = await browse_exec(
            ["eval", "1+1"], str(tmp_path), timeout_sec=5, agent_id="agent-1"
        )
    assert rc == -1
    assert "timed out" in err
    assert BROWSE_RESTART_HINT in err
