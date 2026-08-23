"""stop_dev_server, prune-on-allocate, kill-before-start, attestation banner."""

from __future__ import annotations

import os

import pytest

from hiveweave.llm.streamer.doom_loop import doom_loop_limit
from hiveweave.services.process_registry import (
    ProcessRecord,
    allocate_project_port,
    clear_registry_for_tests,
    lookup_by_port,
    lookup_by_project,
    prune_dead_processes,
    register,
    stop_process_by_port,
)
from hiveweave.tools.bash import (
    _attestation_fields_from_note,
    _combine_attestation_output,
)
from hiveweave.tools.dev_server_tools import (
    LookupDevServerParams,
    StartDevServerParams,
    StopDevServerParams,
    lookup_dev_server_tool,
    start_dev_server_tool,
    stop_dev_server_tool,
)

TEST_AGENT = "agent-stop"
TEST_PROJECT = "test-stop-project"


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_doom_loop_stop_dev_server_is_3():
    assert doom_loop_limit("stop_dev_server") == 3
    assert doom_loop_limit("start_dev_server") == 3


def test_allocate_prunes_dead_pids(monkeypatch):
    from hiveweave.services import process_registry as pr

    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: pid == 123)
    register(ProcessRecord(project_id="p1", port=3000, pid=999))
    register(ProcessRecord(project_id="p1", port=3001, pid=123))
    removed = prune_dead_processes()
    assert removed >= 1
    assert lookup_by_port(3000) == []
    assert lookup_by_port(3001)

    register(ProcessRecord(project_id="p1", port=3000, pid=888))
    port = allocate_project_port("p1", 3000)
    assert port == 3000
    assert lookup_by_port(3000) == []


def test_stop_process_by_port_project_scoped(monkeypatch):
    from hiveweave.services import process_registry as pr

    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    register(ProcessRecord(project_id="p1", port=3000, pid=111))
    register(ProcessRecord(project_id="p2", port=3000, pid=222))

    result = stop_process_by_port("p1", 3000)
    assert killed == [111]
    assert result["stopped"][0]["pid"] == 111
    remaining = lookup_by_port(3000)
    assert len(remaining) == 1
    assert remaining[0].project_id == "p2"
    assert remaining[0].pid == 222


def test_stop_process_by_port_refuses_reserved(monkeypatch):
    from hiveweave.services import process_registry as pr

    killed: list[int] = []
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    result = stop_process_by_port("p1", 4000)
    assert killed == []
    assert result["failed"]
    assert "reserved" in result["failed"][0]["error"].lower()
    for port in (5173, 4173):
        r = stop_process_by_port("p1", port)
        assert r["failed"]
        assert killed == []


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
    async def sleep(self, *args, **kwargs) -> None:
        pass

    async def open_connection(self, host: str, port: int):
        return None, _FakeWriter()

    async def wait_for(self, coro, timeout: float = 0.0):
        return await coro

    async def to_thread(self, fn, /, *args, **kwargs):
        return fn(*args, **kwargs)


async def test_stop_dev_server_tool_kills_own_pid(monkeypatch):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3000, pid=555, cwd="/ws")
    )
    result = await stop_dev_server_tool(
        StopDevServerParams(preferred_port=3000, pid=555),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result.success, result.error
    assert killed == [555]
    assert lookup_by_project(TEST_PROJECT) == []


async def test_stop_dev_server_pid_must_match_registry(monkeypatch):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3000, pid=555, cwd="/ws")
    )
    result = await stop_dev_server_tool(
        StopDevServerParams(preferred_port=3000, pid=999),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert not result.success
    assert killed == []
    assert "not a registered server" in (result.error or "")


async def test_stop_dev_server_reserved_blocked(monkeypatch):
    await _patch_project_id(monkeypatch)
    result = await stop_dev_server_tool(
        StopDevServerParams(preferred_port=5173),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert not result.success
    assert result.blocked is True


async def test_start_kills_own_live_before_spawn(monkeypatch, tmp_path):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: pid == 777)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    register(
        ProcessRecord(
            project_id=TEST_PROJECT, port=3000, pid=777, cwd=workspace
        )
    )

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
    assert killed == [777]
    assert result.extra["port"] == 3000


async def test_start_does_not_kill_other_project(monkeypatch, tmp_path):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: pid == 888)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    register(
        ProcessRecord(project_id="other-proj", port=3000, pid=888, cwd=workspace)
    )

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
    assert killed == []
    assert result.extra["port"] != 3000
    assert lookup_by_port(3000)[0].pid == 888


async def test_lookup_includes_pid_alive(monkeypatch, tmp_path):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    existing = str(tmp_path / "alive")
    (tmp_path / "alive").mkdir()
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: pid == 111)
    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3001, pid=111, cwd=existing)
    )
    register(
        ProcessRecord(project_id=TEST_PROJECT, port=3002, pid=222, cwd=existing)
    )
    result = await lookup_dev_server_tool(
        LookupDevServerParams(),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result.success
    by_alive = {s["port"]: s["pid_alive"] for s in result.extra["servers"]}
    assert by_alive[3001] is True
    assert by_alive[3002] is False


def test_attestation_banner_and_extra_fields():
    note = "\n\n[attestation_id=abc123 kind=test_run exit=0] taskId=(unbound)"
    meta = _attestation_fields_from_note(note)
    assert meta["attestation_id"] == "abc123"
    assert meta["kind"] == "test_run"
    body = _combine_attestation_output("pytest ok\n", meta["banner"], note)
    assert body.startswith("[ATTESTATION] attestation_id=abc123 kind=test_run")
    assert "[attestation_id=abc123 kind=test_run exit=0]" in body
    again = _combine_attestation_output(body, meta["banner"], "")
    assert again.count("[ATTESTATION]") == 1


def test_kill_pid_refuses_self(monkeypatch):
    from hiveweave.services import process_registry as pr

    ran: list = []
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **k: ran.append(a)
    )
    with pytest.raises(PermissionError, match="protected pid"):
        pr._kill_pid(os.getpid())
    assert ran == []


def test_register_refuses_protected_pid():
    with pytest.raises(ValueError, match="protected pid"):
        register(ProcessRecord(project_id="p1", port=3000, pid=os.getpid()))


def test_stop_process_by_port_refuses_protected_pid(monkeypatch):
    from hiveweave.services import process_registry as pr

    ran: list = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **k: ran.append(a)
    )
    pr._registry["p1:3000"] = ProcessRecord(
        project_id="p1", port=3000, pid=os.getpid()
    )
    result = stop_process_by_port("p1", 3000)
    assert ran == []
    assert result["failed"]
    assert "protected" in result["failed"][0]["error"].lower()


def test_hydrate_skips_reserved_and_protected(monkeypatch, tmp_path):
    import json
    from hiveweave.services import process_registry as pr

    path = tmp_path / "reg.json"
    path.write_text(json.dumps({
        "p1:4000": {"project_id": "p1", "port": 4000, "pid": 12},
        "p1:3000": {"project_id": "p1", "port": 3000, "pid": os.getpid()},
        "p1:3001": {"project_id": "p1", "port": 3001, "pid": 424242},
    }))
    monkeypatch.setattr(pr, "_REGISTRY_PATH", path)
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    pr._registry.clear()
    pr._hydrated = False
    pr.hydrate_registry()
    ports = {r.port for r in pr._registry.values()}
    assert 4000 not in ports
    assert 3000 not in ports
    assert 3001 in ports


def test_prune_drops_reserved_and_protected(monkeypatch):
    from hiveweave.services import process_registry as pr

    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    pr._registry["p1:4000"] = ProcessRecord(project_id="p1", port=4000, pid=12)
    pr._registry["p1:3000"] = ProcessRecord(
        project_id="p1", port=3000, pid=os.getpid()
    )
    pr._registry["p1:3001"] = ProcessRecord(
        project_id="p1", port=3001, pid=424242
    )
    removed = prune_dead_processes()
    assert removed >= 2
    ports = {r.port for r in lookup_by_project("p1")}
    assert 4000 not in ports
    assert 3000 not in ports
    assert 3001 in ports


async def test_lookup_preferred_port_filters_other_project(monkeypatch, tmp_path):
    from hiveweave.services import process_registry as pr

    await _patch_project_id(monkeypatch)
    existing = str(tmp_path / "alive")
    (tmp_path / "alive").mkdir()
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    register(
        ProcessRecord(project_id="other-proj", port=3001, pid=111, cwd=existing)
    )
    result = await lookup_dev_server_tool(
        LookupDevServerParams(preferred_port=3001),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result.success
    assert result.extra["servers"] == []
    own = ProcessRecord(
        project_id=TEST_PROJECT, port=3001, pid=222, cwd=existing
    )
    register(own)
    result2 = await lookup_dev_server_tool(
        LookupDevServerParams(preferred_port=3001),
        agent_id=TEST_AGENT,
        workspace="",
    )
    assert result2.success
    servers = result2.extra["servers"]
    assert len(servers) == 1
    assert servers[0]["pid"] == 222
    assert servers[0]["project_id"] == TEST_PROJECT


def test_pid_is_protected_fail_closed_on_lookup_error(monkeypatch):
    from hiveweave.services import process_registry as pr

    def boom():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(
        "hiveweave.services.command_guard.protected_pids", boom
    )
    assert pr._pid_is_protected(424242) is True
    ran: list = []
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(PermissionError, match="protected pid"):
        pr._kill_pid(424242)
    assert ran == []


def test_stop_worktree_refuses_reserved_port(monkeypatch, tmp_path):
    from hiveweave.services import process_registry as pr

    wt = str(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    killed: list[int] = []
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: killed.append(pid))
    pr._registry["p1:4000"] = ProcessRecord(
        project_id="p1", port=4000, pid=12, cwd=wt
    )
    result = pr.stop_processes_for_worktree(wt)
    assert killed == []
    assert result["failed"]
    assert "reserved" in result["failed"][0]["error"].lower()
    assert lookup_by_port(4000) == []


async def test_bash_app_server_kills_preferred_before_allocate(
    monkeypatch, tmp_path
):
    """No --port restart must kill this project's live 3000, not allocate 3001."""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    from hiveweave.services import process_registry as pr
    from hiveweave.tools.bash import _run_registered_dev_server

    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    stopped_ports: list[int] = []
    real_stop = pr.stop_process_by_port

    def spy_stop(project_id, port):
        stopped_ports.append(int(port))
        return real_stop(project_id, port)

    monkeypatch.setattr(pr, "stop_process_by_port", spy_stop)
    monkeypatch.setattr(pr, "_is_pid_alive", lambda pid: pid == 777)
    monkeypatch.setattr(pr, "_kill_pid", lambda pid: None)
    register(
        ProcessRecord(project_id="p-bash", port=3000, pid=777, cwd=workspace)
    )

    class _Proc:
        pid = 888888

    def fake_spawn(cmd, *, cwd, project_id, preferred_port, **kwargs):
        return _Proc(), None, {
            "command": cmd,
            "env_port": str(preferred_port),
        }

    monkeypatch.setattr(pr, "spawn_project_process", fake_spawn)
    monkeypatch.setattr(pr, "pick_observed_listen_port", lambda *_a, **_k: None)

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("hiveweave.tools.bash.asyncio.sleep", _no_sleep)
    result = await _run_registered_dev_server(
        "python -m app.server",
        workspace,
        workspace,
        "p-bash",
        0,
    )
    assert result is not None
    assert result["success"] is True
    assert 3000 in stopped_ports
    servers = lookup_by_project("p-bash")
    assert len(servers) == 1
    assert servers[0].port == 3000
    assert servers[0].pid == 888888
    assert "PORT=3000" in (result.get("output") or "")


async def test_bash_registers_observed_listen_port(monkeypatch, tmp_path):
    """app.server that ignores PORT is registered on the observed LISTEN port."""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    from hiveweave.services import process_registry as pr
    from hiveweave.tools.bash import _run_registered_dev_server

    workspace = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()

    class _Proc:
        pid = 888888

    def fake_spawn(cmd, *, cwd, project_id, preferred_port, **kwargs):
        return _Proc(), None, {
            "command": cmd,
            "env_port": str(preferred_port),
        }

    monkeypatch.setattr(pr, "spawn_project_process", fake_spawn)
    monkeypatch.setattr(
        pr, "pick_observed_listen_port", lambda *_a, **_k: 8000
    )

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("hiveweave.tools.bash.asyncio.sleep", _no_sleep)
    result = await _run_registered_dev_server(
        "python -m app.server",
        workspace,
        workspace,
        "p-obs",
        0,
    )
    assert result is not None
    assert result["success"] is True
    servers = lookup_by_project("p-obs")
    assert len(servers) == 1
    assert servers[0].port == 8000
    assert servers[0].pid == 888888
    assert "port=8000" in (result.get("output") or "")
    assert "ignores PORT" not in (result.get("output") or "")
