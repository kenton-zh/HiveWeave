"""M6 regression tests: dev server cwd pre-check, sandbox resolve semantics,
spawn failure visibility, and stale marking in lookup_dev_server.

Covers:
  1. cwd that does not exist -> clear error, no WinError 267 from Popen.
  2. prefix-bypass case (workspace="D:\\proj" + cwd="D:\\proj2\\x") -> rejected.
  3. valid cwd -> success path unchanged (spawn mocked).
  4. lookup_dev_server marks records stale when their cwd is gone.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hiveweave.services.process_registry import (
    ProcessRecord,
    clear_registry_for_tests,
    register,
)
from hiveweave.tools.dev_server_tools import (
    LookupDevServerParams,
    StartDevServerParams,
    lookup_dev_server_tool,
    start_dev_server_tool,
)

TEST_AGENT = "agent-m6"
TEST_PROJECT = "test-m6-project"


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


async def _patch_project_id(monkeypatch, project_id: str | None = TEST_PROJECT):
    async def fake_get_project_id(agent_id: str) -> str | None:
        return project_id

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.get_project_id", fake_get_project_id
    )


class _FakeProc:
    pid = 424242
    returncode = None

    def poll(self):
        return None


class _FakeWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeAsyncio:
    """Stand-in for module-level asyncio: health check connects instantly."""

    async def sleep(self, *args, **kwargs) -> None:
        pass

    async def open_connection(self, host: str, port: int):
        return None, _FakeWriter()

    async def wait_for(self, coro, timeout: float = 0.0):
        return await coro


async def test_missing_cwd_returns_clear_error(monkeypatch, tmp_path):
    await _patch_project_id(monkeypatch)
    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()

    async def fail_spawn(*args, **kwargs):
        raise AssertionError("spawn must not be reached for missing cwd")

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.spawn_project_process", fail_spawn
    )

    result = await start_dev_server_tool(
        StartDevServerParams(cwd="missing/sub"),
        agent_id=TEST_AGENT,
        workspace=workspace,
    )

    assert not result.success
    assert result.blocked is True
    assert "Working directory does not exist" in (result.error or "")
    assert "missing" in (result.error or "")
    assert "WinError" not in (result.error or "")


async def test_prefix_bypass_rejected(monkeypatch, tmp_path):
    """workspace=D:\\proj + cwd=D:\\proj2\\x must be rejected (old startswith bypass)."""
    await _patch_project_id(monkeypatch)
    workspace = str(tmp_path / "proj")
    sibling = tmp_path / "proj2" / "x"
    (tmp_path / "proj").mkdir()
    sibling.mkdir(parents=True)
    assert workspace.startswith(str(tmp_path / "proj"))
    assert str(sibling).startswith(str(tmp_path / "proj"))

    async def fail_spawn(*args, **kwargs):
        raise AssertionError("spawn must not be reached for sandbox violation")

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.spawn_project_process", fail_spawn
    )

    # Absolute sibling path (the exact audit bypass case)
    result = await start_dev_server_tool(
        StartDevServerParams(cwd=str(sibling)),
        agent_id=TEST_AGENT,
        workspace=workspace,
    )
    assert not result.success
    assert result.blocked is True
    assert result.error == "cwd must stay inside workspace"

    # Relative traversal form
    result = await start_dev_server_tool(
        StartDevServerParams(cwd=os.path.join("..", "proj2", "x")),
        agent_id=TEST_AGENT,
        workspace=workspace,
    )
    assert not result.success
    assert result.blocked is True
    assert result.error == "cwd must stay inside workspace"


async def test_valid_cwd_success_path_unchanged(monkeypatch, tmp_path):
    await _patch_project_id(monkeypatch)
    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()

    def fake_spawn(cmd, *, cwd, project_id, preferred_port, stdout, stderr):
        return _FakeProc(), None, {"command": cmd}

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.spawn_project_process", fake_spawn
    )
    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.asyncio", _FakeAsyncio()
    )

    result = await start_dev_server_tool(
        StartDevServerParams(preferred_port=3000),
        agent_id=TEST_AGENT,
        workspace=workspace,
    )

    assert result.success, result.error
    assert f"localhost:{result.extra['port']}/" in result.output
    assert result.extra["pid"] == _FakeProc.pid
    assert result.extra["cwd"] == workspace
    servers = result.extra["project_servers"]
    assert len(servers) == 1
    assert servers[0]["project_id"] == TEST_PROJECT


async def test_lookup_marks_stale_cwd(monkeypatch, tmp_path):
    await _patch_project_id(monkeypatch)
    existing = str(tmp_path / "alive")
    (tmp_path / "alive").mkdir()
    missing = str(tmp_path / "gone")

    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3001, pid=111, cwd=existing)
    )
    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3002, pid=222, cwd=missing)
    )

    # By-port branch
    result = await lookup_dev_server_tool(
        LookupDevServerParams(preferred_port=3002),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result.success
    servers = result.extra["servers"]
    assert len(servers) == 1
    assert servers[0]["port"] == 3002
    assert servers[0]["stale"] is True

    result = await lookup_dev_server_tool(
        LookupDevServerParams(preferred_port=3001),
        agent_id=TEST_AGENT,
        workspace="",
    )
    servers = result.extra["servers"]
    assert len(servers) == 1
    assert servers[0]["stale"] is False

    # Omit port → list this project (must not silently filter default 3000)
    result = await lookup_dev_server_tool(
        LookupDevServerParams(),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result.success
    by_stale = {s["port"]: s["stale"] for s in result.extra["servers"]}
    assert by_stale == {3001: False, 3002: True}


def test_spawn_child_env_strips_secrets_not_bash_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HIVEWEAVE_ARK_API_KEY", "ark-secret")
    monkeypatch.setenv("NODE_ENV", "development")
    captured: dict = {}

    class _FakePopen:
        pid = 99

        def __init__(self, *args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setattr(
        "hiveweave.services.process_registry.subprocess.Popen",
        _FakePopen,
    )
    from hiveweave.services.process_registry import spawn_project_process

    proc, err, _meta = spawn_project_process(
        "echo hi", cwd=str(tmp_path), project_id=TEST_PROJECT
    )
    assert err is None
    assert proc is not None
    env = captured["env"]
    assert "OPENAI_API_KEY" not in env
    assert "HIVEWEAVE_ARK_API_KEY" not in env
    assert env.get("HIVEWEAVE_BASH") != "1"
    assert env.get("NODE_ENV") == "development"
    assert env.get("HIVEWEAVE_WORKSPACE") == str(tmp_path)
    assert any(k.upper() == "PATH" for k in env)


def test_filtered_environ_excludes_node_options(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")
    monkeypatch.setenv("NODE_ENV", "development")
    from hiveweave.util.safe_env import build_child_env, filtered_environ

    mcp_env = filtered_environ()
    assert "NODE_OPTIONS" not in mcp_env
    assert mcp_env.get("NODE_ENV") == "development"
    child = build_child_env("/ws", bash_markers=False)
    assert child.get("NODE_OPTIONS") == "--require ./evil.js"


async def test_ghost_cwd_rejected_blocked(monkeypatch, tmp_path):
    await _patch_project_id(monkeypatch)
    wt = tmp_path / "project" / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)

    async def fail_spawn(*args, **kwargs):
        raise AssertionError("spawn must not be reached for ghost cwd")

    monkeypatch.setattr(
        "hiveweave.tools.dev_server_tools.spawn_project_process", fail_spawn
    )
    result = await start_dev_server_tool(
        StartDevServerParams(cwd=".hiveweave/worktrees/A044"),
        agent_id=TEST_AGENT,
        workspace=str(wt),
    )
    assert not result.success
    assert result.blocked is True
    assert "疑似重复 worktree 前缀路径" in (result.error or "")
