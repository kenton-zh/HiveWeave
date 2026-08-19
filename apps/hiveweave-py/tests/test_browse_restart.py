"""browse restart/reset hard-recycles the agent-browser session (no CLI close)."""

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
async def test_restart_hard_recycles_session(browse_fake_proc, tmp_path, monkeypatch):
    async def fake_recycle():
        return "hard-recycled"

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._hard_recycle_browser", fake_recycle
    )
    with browse_fake_proc:
        result = await browse_tool(
            BrowseParams(args=["restart"]),
            "agent-1",
            str(tmp_path),
        )
    assert result.success is True
    assert result.output == f"{BROWSE_RESTART_OK} [hard-recycled]"
    assert "browser session closed" in result.output
    assert "starts fresh" in result.output


@pytest.mark.asyncio
async def test_restart_does_not_spawn_cli(browse_fake_proc, tmp_path, monkeypatch):
    """Restart must not depend on the agent-browser CLI close — a wedged daemon
    ignores or hangs on close, so restart hard-kills the daemon instead."""

    async def fake_recycle():
        return "hard-recycled"

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._hard_recycle_browser", fake_recycle
    )
    with browse_fake_proc as ctx:
        result = await browse_tool(
            BrowseParams(args=["restart"]),
            "agent-1",
            str(tmp_path),
        )
    assert result.success is True
    assert ctx.spawn_env is None  # no agent-browser CLI subprocess spawned


@pytest.mark.asyncio
async def test_timeout_recycles_session_and_mentions_fresh(
    browse_fake_proc, tmp_path, monkeypatch
):
    async def fake_drain(*_a, **_k):
        return -1, b"", b""

    async def fake_recycle():
        return "hard-recycled"

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._run_and_drain", fake_drain
    )
    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._hard_recycle_browser", fake_recycle
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
    assert "hard-recycled" in text
    assert "starts a fresh browser" in text


@pytest.mark.asyncio
async def test_wait_timeout_keeps_plain_restart_hint(
    browse_fake_proc, tmp_path, monkeypatch
):
    """`wait` is intentionally long-lived; a wait timeout must not hard-recycle
    the session (would tear down a healthy browser on a legit slow wait)."""

    async def fake_drain(*_a, **_k):
        return -1, b"", b""

    recycled = []

    async def fake_recycle():
        recycled.append(1)
        return "hard-recycled"

    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._run_and_drain", fake_drain
    )
    monkeypatch.setattr(
        "hiveweave.tools.browse_tools._hard_recycle_browser", fake_recycle
    )
    with browse_fake_proc:
        result = await browse_tool(
            BrowseParams(args=["wait_for", ".ready", "5000"]),
            "agent-1",
            str(tmp_path),
        )
    assert result.success is False
    assert recycled == []
    text = (result.error or "") + (result.output or "")
    assert "timed out" in text
    assert BROWSE_RESTART_HINT in text


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
