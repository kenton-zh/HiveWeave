"""start_dev_server — allocate a non-reserved port and register the process."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult
from hiveweave.tools.helpers import get_project_id
from hiveweave.services.process_registry import (
    ProcessRecord,
    allocate_project_port,
    check_command_reserved_ports,
    hydrate_registry,
    is_reserved_port,
    lookup_by_port,
    lookup_by_project,
    register,
    spawn_project_process,
)

# Tail bytes to attach on failure (enough for vite/webpack error output)
_LOG_TAIL_BYTES = 4096


def _dev_server_log_path(workspace: str, port: int) -> Path:
    """Deterministic log path: <workspace>/.hiveweave/logs/dev-server-<port>.log"""
    log_dir = Path(workspace) / ".hiveweave" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"dev-server-{port}.log"


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


@tool(
    "start_dev_server",
    "Start the project's Vite/dev server on a non-reserved port (never 5173/4000). "
    "Registers pid/cwd/port for URL lookup. Prefer this over bare `npm run dev` / `vite`.",
    requires_workspace=True,
    security_level="shell",
)
async def start_dev_server_tool(
    params: StartDevServerParams, agent_id: str, workspace: str
) -> ToolResult:
    project_id = await get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    if is_reserved_port(params.preferred_port):
        return ToolResult.err(
            f"Port {params.preferred_port} is reserved for HiveWeave. "
            "Use preferredPort=3000 (or another free project port)."
        )

    port = allocate_project_port(project_id, params.preferred_port)
    work_cwd = workspace
    if params.cwd:
        work_cwd = str((Path(workspace) / params.cwd).resolve())
        if not work_cwd.startswith(str(Path(workspace).resolve())):
            return ToolResult.err("cwd must stay inside workspace")

    if params.command:
        err = check_command_reserved_ports(params.command)
        if err:
            return ToolResult.err(err)
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

    # Health: process alive + port eventually accepts TCP (pid+cwd already known)
    listening = False
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
            listening = True
            break
        except Exception:
            continue

    if not listening:
        try:
            proc.terminate()
        except Exception:
            pass
        log_file.close()
        tail = _read_log_tail(log_path)
        msg = (
            f"Dev server pid={proc.pid} started but port {port} never listened. "
            f"Command was: {cmd}"
        )
        if tail:
            msg += f"\n--- log tail ({log_path.name}) ---\n{tail[-2000:]}"
        return ToolResult.err(msg)

    # Server is listening — detach the log file (process keeps the fd).
    # Do NOT close: the child process owns the fd via inheritance.
    # (On Windows the file stays readable; on POSIX the fd lives with the child.)

    commit = ""
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        r = subprocess.run(
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

    rec = register(
        ProcessRecord(
            project_id=project_id,
            port=port,
            pid=proc.pid,
            cwd=work_cwd,
            command=cmd,
            commit=commit,
        )
    )
    return ToolResult.ok(
        f"Dev server started on http://localhost:{port}/ "
        f"(pid={proc.pid}, cwd={work_cwd}, listening=ok). "
        f"Log: {log_path}. "
        f"Do NOT use ports 5173/4000 — those are HiveWeave.",
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
    "Look up registered project dev servers by port or list this project's servers.",
    requires_workspace=False,
    security_level="read",
)
async def lookup_dev_server_tool(
    params: StartDevServerParams, agent_id: str, workspace: str
) -> ToolResult:
    hydrate_registry()
    project_id = await get_project_id(agent_id)
    if params.preferred_port and not is_reserved_port(params.preferred_port):
        hits = lookup_by_port(params.preferred_port)
        if hits:
            return ToolResult.ok(
                f"Found {len(hits)} registration(s) on port {params.preferred_port}",
                servers=[h.to_dict() for h in hits],
            )
        return ToolResult.ok(
            f"No registry entry for port {params.preferred_port}",
            servers=[],
        )
    if not project_id:
        return ToolResult.err("No project")
    servers = lookup_by_project(project_id)
    return ToolResult.ok(
        f"{len(servers)} registered server(s) for this project",
        servers=[s.to_dict() for s in servers],
    )
