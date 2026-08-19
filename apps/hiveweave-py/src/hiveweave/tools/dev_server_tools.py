"""start_dev_server / stop_dev_server / lookup_dev_server — project process registry."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult
from hiveweave.tools.helpers import get_project_id
from hiveweave.util.tree_label import cwd_display
from hiveweave.services.process_registry import (
    ProcessRecord,
    allocate_project_port,
    check_command_reserved_ports,
    hydrate_registry,
    is_pid_alive,
    is_reserved_port,
    lookup_by_port,
    lookup_by_project,
    pick_observed_listen_port,
    prune_dead_processes,
    register,
    spawn_project_process,
    stop_process_by_port,
    terminate_spawned,
)

# Tail bytes to attach on failure (enough for vite/webpack error output)
_LOG_TAIL_BYTES = 4096

log = structlog.get_logger(__name__)


def _dev_server_log_path(workspace: str, port: int) -> Path:
    """Deterministic log path: <workspace>/.hiveweave/logs/dev-server-<port>.log"""
    log_dir = Path(workspace) / ".hiveweave" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"dev-server-{port}.log"


def _with_stale(records: list[ProcessRecord]) -> list[dict]:
    """Attach ``stale: true`` to records whose cwd no longer exists.

    Mark only — entries are kept in the registry (a process may still be
    alive or the dir may come back).
    """
    out: list[dict] = []
    for r in records:
        d = r.to_dict()
        d["stale"] = bool(r.cwd) and not Path(r.cwd).is_dir()
        d["pid_alive"] = is_pid_alive(r.pid)
        out.append(d)
    return out


def _read_log_tail(log_path: Path, max_bytes: int = _LOG_TAIL_BYTES) -> str:
    """Read the last ``max_bytes`` of a log file (best-effort)."""
    try:
        if not log_path.exists():
            return ""
        size = log_path.stat().st_size
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # skip partial line
            return f.read().strip()
    except Exception:
        return ""


class StartDevServerParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command: str | None = Field(
        default=None,
        description="Optional override command. Default: npx vite --host 0.0.0.0 --port <P> --strictPort",
    )
    preferred_port: int = Field(
        default=3000,
        alias="preferredPort",
        description="Preferred project port (must not be 4000/5173/4173).",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory relative to workspace (default: workspace root).",
    )


class LookupDevServerParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preferred_port: int | None = Field(
        default=None,
        alias="preferredPort",
        description=(
            "Optional port filter. Omit to list this project's servers."
        ),
        json_schema_extra={"aliases": ["preferredPort", "preferred_port", "port"]},
    )


class StopDevServerParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preferred_port: int = Field(
        alias="preferredPort",
        description=(
            "Project port to stop (must not be 4000/5173/4173)."
        ),
        json_schema_extra={"aliases": ["preferredPort", "preferred_port", "port"]},
    )
    pid: int | None = Field(
        default=None,
        description=(
            "Optional. Must match a registered pid for this project."
        ),
    )


async def _agent_active_verify_task(
    project_id: str, agent_id: str
) -> str | None:
    """Return the agent's in-flight (claimed/running) VERIFY task id, if any.

    Milestone VERIFY must validate MAIN: when an agent with an active VERIFY
    starts a dev server, the server must run against the project root, not its
    worktree (worktree code is stale for milestone QA). Cheap: called only on
    dev-server start; obligations re-read from DB each time.
    """
    try:
        from hiveweave.services.task import TaskService

        ts = TaskService()
        obligations = await ts.get_actionable_obligations(project_id, agent_id)
    except Exception:
        return None
    for t in obligations or []:
        if (
            t.get("assignee_id") == agent_id
            and t.get("status") in ("claimed", "running", "rework")
            and TaskService._is_verify_task(t)
        ):
            return str(t.get("id"))
    return None


@tool(
    "start_dev_server",
    "Start the project's Vite/dev server on a non-reserved port (never 5173/4000). "
    "Registers pid/cwd/port for URL lookup. Prefer this over bare `npm run dev` / `vite`. "
    "The spawned child process inherits only a whitelisted environment — project-specific "
    "variables must come from `.hiveweave/env.sh` or an inline `VAR=x cmd` command prefix. "
    "VERIFY agents: when you hold an in-flight VERIFY task, this resolves to the MAIN "
    "project root (never your worktree) so milestone QA runs against merged code.",
    requires_workspace=True,
    security_level="shell",
)
async def start_dev_server_tool(
    params: StartDevServerParams, agent_id: str, workspace: str
) -> ToolResult:
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    # VERIFY 取证必须在 MAIN 跑：agent 持有在途 VERIFY 任务时，把工作区解析到
    # 项目 MAIN 根，而不是其 worktree（worktree 代码对里程碑 QA 是陈旧的）。
    verify_main_note = ""
    verify_task_id = await _agent_active_verify_task(project_id, agent_id)
    if verify_task_id:
        from hiveweave.services.worktree_review import project_main_workspace

        try:
            main_ws = await project_main_workspace(project_id)
        except Exception as e:
            main_ws = None
            log.warning(
                "dev_server_verify_main_resolve_failed",
                project_id=project_id,
                error=str(e),
            )
        if main_ws:
            workspace = main_ws
            verify_main_note = (
                f"VERIFY {verify_task_id[:8]}: dev server resolved to MAIN "
                f"project root {cwd_display(workspace)}; "
                f"worktree code is stale for QA."
            )

    if is_reserved_port(params.preferred_port):
        return ToolResult.blocked_err(
            f"Port {params.preferred_port} is reserved for HiveWeave. "
            "Use preferredPort=3000 (or another free project port)."
        )

    work_cwd = workspace
    if params.cwd:
        from hiveweave.tools.file import _resolve_safe_detail

        full, hint = _resolve_safe_detail(workspace, params.cwd)
        if hint is not None:
            return ToolResult.blocked_err(f"Error: {hint}")
        if full is None:
            return ToolResult.blocked_err("cwd must stay inside workspace")
        work_cwd = full
    if not Path(work_cwd).is_dir():
        return ToolResult.blocked_err(
            f"Working directory does not exist: "
            f"{cwd_display(work_cwd, params.cwd)}"
        )

    if params.command:
        err = check_command_reserved_ports(params.command)
        if err:
            return ToolResult.blocked_err(err)

    # 阻塞调用（netstat 快照 / taskkill）统一下放线程池，避免卡住事件循环
    await asyncio.to_thread(prune_dead_processes)
    preferred = params.preferred_port
    own_live_pref = [
        r for r in await asyncio.to_thread(lookup_by_port, preferred)
        if r.project_id == project_id and is_pid_alive(r.pid)
    ]
    if own_live_pref:
        # Kill-before-start: reuse this project's port. Never kill others.
        await asyncio.to_thread(stop_process_by_port, project_id, preferred)

    port = await asyncio.to_thread(allocate_project_port, project_id, preferred)
    other_on_port = [
        r for r in await asyncio.to_thread(lookup_by_port, port)
        if r.project_id != project_id and is_pid_alive(r.pid)
    ]
    if other_on_port:
        port = await asyncio.to_thread(
            allocate_project_port, project_id, port + 1
        )
    await asyncio.to_thread(prune_dead_processes)
    own_on_port = [
        r for r in await asyncio.to_thread(lookup_by_port, port)
        if r.project_id == project_id and is_pid_alive(r.pid)
    ]
    if own_on_port:
        await asyncio.to_thread(stop_process_by_port, project_id, port)

    if params.command:
        # Inject allocated port if command has placeholder
        cmd = params.command.replace("{port}", str(port))
    else:
        cmd = (
            f"npx vite --host 0.0.0.0 --port {port} --strictPort"
        )

    # Detect package manager script
    pkg = Path(work_cwd) / "package.json"
    if not params.command and pkg.exists():
        cmd = f"npx vite --host 0.0.0.0 --port {port} --strictPort"

    # P0-2a: capture dev server output to a log file (was DEVNULL — black hole).
    log_path = _dev_server_log_path(workspace, port)
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    log_file.write(
        f"\n{'='*60}\n"
        f"[hiveweave] start_dev_server pid=? port={port} "
        f"cmd={cmd!r} cwd={work_cwd} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*60}\n"
    )
    log_file.flush()

    from hiveweave.services.eval_seal import (
        is_eval_sealed,
        sealed_bash_deny_for_workspace,
    )

    if is_eval_sealed(workspace) and not params.command and cmd.startswith("npx "):
        cmd = "npx --offline " + cmd[4:]
    seal_reason = sealed_bash_deny_for_workspace(workspace, cmd)
    if seal_reason:
        log_file.close()
        return ToolResult.blocked_err(f"Error: {seal_reason}")

    try:
        proc, spawn_err, meta = spawn_project_process(
            cmd,
            cwd=work_cwd,
            project_id=project_id,
            preferred_port=port,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        if spawn_err or proc is None:
            log_file.close()
            tail = _read_log_tail(log_path)
            msg = spawn_err or "Failed to start"
            if tail:
                msg += f"\n--- log tail ({log_path.name}) ---\n{tail[-2000:]}"
            return ToolResult.err(msg)
        cmd = meta.get("command") or cmd
    except Exception as e:
        log_file.close()
        tail = _read_log_tail(log_path)
        msg = f"Failed to start: {e}"
        if tail:
            msg += f"\n--- log tail ({log_path.name}) ---\n{tail[-2000:]}"
        return ToolResult.err(msg)

    # Health: process alive + a non-reserved port eventually listens.
    # Prefer the allocated port; if the app ignores PORT (app.server),
    # register the observed LISTEN port instead of killing the process.
    listening_port: int | None = None
    for _ in range(15):
        await asyncio.sleep(0.4)
        if proc.poll() is not None:
            log_file.close()
            tail = _read_log_tail(log_path)
            msg = (
                f"Dev server exited (code={proc.returncode}). Command was: {cmd}"
            )
            if tail:
                msg += f"\n--- log tail ({log_path.name}) ---\n{tail[-2000:]}"
            return ToolResult.err(msg)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=0.5,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            del reader
            listening_port = port
            break
        except Exception:
            observed = await asyncio.to_thread(
                pick_observed_listen_port, proc.pid, port
            )
            if observed:
                listening_port = observed
                break
            continue

    if listening_port is None:
        await asyncio.to_thread(terminate_spawned, proc)
        log_file.close()
        tail = _read_log_tail(log_path)
        msg = (
            f"Dev server pid={proc.pid} started but no non-reserved LISTEN "
            f"port was observed (tried {port}). Command was: {cmd}"
        )
        if tail:
            msg += f"\n--- log tail ({log_path.name}) ---\n{tail[-2000:]}"
        return ToolResult.err(msg)
    port = listening_port

    # Server is listening — detach the log file (process keeps the fd).
    # Do NOT close: the child process owns the fd via inheritance.
    # (On Windows the file stays readable; on POSIX the fd lives with the child.)

    commit = ""
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        r = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **windows_no_window_kwargs(),
        )
        if r.returncode == 0:
            commit = (r.stdout or "").strip()
    except Exception:
        pass

    try:
        rec = await asyncio.to_thread(
            register,
            ProcessRecord(
                project_id=project_id,
                port=port,
                pid=proc.pid,
                cwd=work_cwd,
                command=cmd,
                commit=commit,
            ),
        )
    except Exception as e:
        await asyncio.to_thread(terminate_spawned, proc)
        return ToolResult.err(f"Failed to register dev server: {e}")
    note = (
        f"Dev server started on http://localhost:{port}/ "
        f"(pid={proc.pid}, {cwd_display(work_cwd, params.cwd)}, listening=ok). "
        f"Log: {log_path}. "
        f"Do NOT use ports 5173/4000 — those are HiveWeave."
    )
    if verify_main_note:
        note = f"{note}\n{verify_main_note}"
    return ToolResult.ok(
        note,
        port=port,
        pid=proc.pid,
        cwd=work_cwd,
        command=cmd,
        url=f"http://localhost:{port}/",
        log_path=str(log_path),
        registry=rec.to_dict(),
        project_servers=[r.to_dict() for r in lookup_by_project(project_id)],
    )


@tool(
    "lookup_dev_server",
    "List this project's registered dev servers. Pass preferredPort to "
    "filter one port; omit to list all.",
    requires_workspace=False,
    security_level="read",
)
async def lookup_dev_server_tool(
    params: LookupDevServerParams, agent_id: str, workspace: str
) -> ToolResult:
    await asyncio.to_thread(hydrate_registry)
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err("No project")
    if params.preferred_port and not is_reserved_port(params.preferred_port):
        hits = [
            r for r in await asyncio.to_thread(
                lookup_by_port, params.preferred_port
            )
            if r.project_id == project_id
        ]
        if hits:
            return ToolResult.ok(
                f"Found {len(hits)} registration(s) on port {params.preferred_port}",
                servers=_with_stale(hits),
            )
        return ToolResult.ok(
            f"No registry entry for port {params.preferred_port}",
            servers=[],
        )
    servers = await asyncio.to_thread(lookup_by_project, project_id)
    return ToolResult.ok(
        f"{len(servers)} registered server(s) for this project",
        servers=_with_stale(servers),
    )


@tool(
    "stop_dev_server",
    "Stop this project's registered dev server on preferredPort. "
    "Optional pid must match a registry pid for this project. "
    "Uses taskkill /T /PID (never /IM or Stop-Process). Never kills "
    "HiveWeave ports 4000/5173/4173. For bg-bash-/bg-sub- jobs use job_kill.",
    requires_workspace=False,
    security_level="shell",
)
async def stop_dev_server_tool(
    params: StopDevServerParams, agent_id: str, workspace: str
) -> ToolResult:
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    port = int(params.preferred_port)
    if is_reserved_port(port):
        return ToolResult.blocked_err(
            f"Port {port} is reserved for HiveWeave. "
            "Use stop_dev_server on a project port (3000+), never 4000/5173/4173."
        )

    await asyncio.to_thread(hydrate_registry)
    await asyncio.to_thread(prune_dead_processes)

    if params.pid is not None:
        matches = [
            r for r in await asyncio.to_thread(lookup_by_project, project_id)
            if r.pid == int(params.pid)
        ]
        if not matches:
            return ToolResult.err(
                f"pid={params.pid} is not a registered server for this project. "
                "Use lookup_dev_server. job_kill is only for bg-bash-/bg-sub- jobs."
            )
        rec = matches[0]
        if rec.port != port:
            return ToolResult.err(
                f"pid={params.pid} is registered on port {rec.port}, "
                f"not preferredPort={port}."
            )

    result = await asyncio.to_thread(stop_process_by_port, project_id, port)
    failed = result.get("failed") or []
    stopped = result.get("stopped") or []
    if failed and not stopped:
        err = failed[0].get("error") or "stop failed"
        if "reserved" in str(err).lower():
            return ToolResult.blocked_err(str(err))
        return ToolResult.err(str(err), **result)
    if not stopped:
        return ToolResult.ok(
            f"No registered server on port {port} for this project. "
            "Use lookup_dev_server to list servers.",
            port=port,
            **result,
        )
    status = stopped[0].get("status") or "stopped"
    pid = stopped[0].get("pid")
    return ToolResult.ok(
        f"Stopped project server on port {port} (pid={pid}, status={status}).",
        port=port,
        pid=pid,
        **result,
    )

