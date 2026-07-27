"""Project process registry + platform reserved ports (P0/P2).

P0: refuse reserved binds in agent tools and register start_dev_server.
P2: spawn_project_process injects reserved-port env and rewrites known CLIs.
TEST21 M11: persist registry to JSON; hydrate + prune dead PIDs on lookup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "process_registry.json"
)

# HiveWeave platform — project apps must never bind these
RESERVED_PORTS: frozenset[int] = frozenset({4000, 5173, 4173})

# Process image names that host the platform API / web UI.
# Agents must kill by *project* port (3000+), never wholesale node/python.
PROTECTED_PROCESS_IMAGES: frozenset[str] = frozenset({
    "node",
    "node.exe",
    "python",
    "python.exe",
    "pythonw",
    "pythonw.exe",
    "uvicorn",
})

_PORT_FLAG_RE = re.compile(
    r"(?:--port[= ]|--listen[= ]|-p[= ])(\d{2,5})",
    re.IGNORECASE,
)
_PORT_ENV_RE = re.compile(
    r"(?:PORT|VITE_PORT)\s*=\s*(\d{2,5})",
    re.IGNORECASE,
)
_VITE_BARE_RE = re.compile(
    r"\b(npx\s+)?vite\b|\bnpm\s+run\s+dev\b|\bpnpm\s+(?:run\s+)?dev\b",
    re.IGNORECASE,
)

# Kill / stop verbs (Windows + POSIX + common helpers)
_KILL_VERB_RE = re.compile(
    r"\b(?:"
    r"kill|killall|pkill|taskkill|stop-process|"
    r"kill-port|npx\s+kill-port|"
    r"fuser\b[^;\n|&]{0,40}-k"  # fuser -k …
    r")\b",
    re.IGNORECASE,
)

# Reference to a reserved platform port in kill/lookup context
_RESERVED_PORT_REF_RE = re.compile(
    r"(?:"
    r"(?:^|[\s`'\"(=/:])(?P<p1>4000|5173|4173)\b"  # bare / :4000 / =4000
    r"|LocalPort\s+(?P<p2>4000|5173|4173)\b"
    r"|-ti?:(?P<p3>4000|5173|4173)\b"  # lsof -ti:4000
    r"|(?P<p4>4000|5173|4173)/tcp\b"  # fuser 4000/tcp
    r")",
    re.IGNORECASE,
)

# Wholesale image kill: taskkill /IM node.exe, Stop-Process -Name python, …
_IMAGE_KILL_RE = re.compile(
    r"(?:"
    r"\btaskkill\b[^;\n|&]{0,80}(?:/IM|//IM|-IM)\s+"
    r"(?P<img1>node|pythonw?|uvicorn)(?:\.exe)?"
    r"|\bStop-Process\b[^;\n|&]{0,80}-Name\s+"
    r"(?P<img2>node|pythonw?|uvicorn)\b"
    r"|\b(?:pkill|killall)\b[^;\n|&]{0,60}\b"
    r"(?P<img3>node|pythonw?|uvicorn)\b"
    r"|\bGet-Process\b[^;\n|&]{0,60}\b"
    r"(?P<img4>node|pythonw?|uvicorn)\b[^;\n|&]{0,80}\bStop-Process\b"
    r"|\bpkill\b[^;\n|&]{0,40}-f[^;\n|&]{0,80}"
    r"(?:uvicorn|hiveweave\.main|vite)\b"
    r")",
    re.IGNORECASE,
)


@dataclass
class ProcessRecord:
    project_id: str
    port: int
    pid: int | None = None
    cwd: str = ""
    command: str = ""
    worktree: str = ""
    commit: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessRecord:
        return cls(
            project_id=str(data.get("project_id") or ""),
            port=int(data.get("port") or 0),
            pid=data.get("pid"),
            cwd=str(data.get("cwd") or ""),
            command=str(data.get("command") or ""),
            worktree=str(data.get("worktree") or ""),
            commit=str(data.get("commit") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )


# In-memory registry (per server process), hydrated from disk on lookup.
_registry: dict[str, ProcessRecord] = {}  # key: f"{project_id}:{port}"
_hydrated = False


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _persist_registry() -> None:
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.to_dict() for k, v in _registry.items()}
        _REGISTRY_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("process_registry_persist_failed", error=str(e))


def hydrate_registry() -> None:
    """Load registry from disk once; drop dead PIDs only on disk load (TEST21 M11).

    In-memory entries are kept until explicit unregister / clear — lookup must
    not wipe freshly registered records whose PID check races or is a test stub.
    """
    global _hydrated
    if _hydrated:
        return
    _hydrated = True
    if not _REGISTRY_PATH.exists():
        return
    try:
        raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        loaded: dict[str, ProcessRecord] = {}
        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            rec = ProcessRecord.from_dict(val)
            if rec.port and _is_pid_alive(rec.pid):
                loaded[key] = rec
        # Merge disk into memory (memory wins on key conflict)
        for key, rec in loaded.items():
            _registry.setdefault(key, rec)
        if len(loaded) != len(raw):
            _persist_registry()
    except Exception as e:
        log.warning("process_registry_hydrate_failed", error=str(e))


def prune_dead_processes() -> int:
    """Drop registry entries whose PID is gone. Returns count removed."""
    hydrate_registry()
    dead = [k for k, r in _registry.items() if not _is_pid_alive(r.pid)]
    for k in dead:
        _registry.pop(k, None)
    if dead:
        _persist_registry()
    return len(dead)


def is_reserved_port(port: int) -> bool:
    return int(port) in RESERVED_PORTS


def extract_ports_from_command(command: str) -> list[int]:
    """Parse explicit port numbers from a shell command string."""
    ports: list[int] = []
    for m in _PORT_FLAG_RE.finditer(command or ""):
        ports.append(int(m.group(1)))
    for m in _PORT_ENV_RE.finditer(command or ""):
        ports.append(int(m.group(1)))
    return ports


def check_command_reserved_ports(command: str) -> str | None:
    """Return error message if command targets a reserved port."""
    for port in extract_ports_from_command(command):
        if is_reserved_port(port):
            return (
                f"Port {port} is reserved for HiveWeave platform "
                f"(API/UI). Use a project port (e.g. 3000+) via "
                f"start_dev_server, not --port {port}."
            )
    # vite / npm run dev without --port often defaults to 5173
    lower = (command or "").lower()
    if (
        ("vite" in lower or "npm run dev" in lower or "pnpm dev" in lower)
        and not extract_ports_from_command(command)
        and "--port" not in lower
    ):
        return (
            "Refusing bare `vite`/`npm run dev` without an explicit project "
            f"port — default 5173 is reserved for HiveWeave. "
            f"Use start_dev_server or `vite --port <project_port> --strictPort`."
        )
    return None


def check_platform_process_kill(command: str) -> str | None:
    """Hard-block killing HiveWeave API/UI processes or reserved ports.

    Covers the TEST11 failure mode where an agent ran
    ``taskkill //F //IM node.exe`` (killed Vite :5173) or
    ``kill $(lsof -ti:4000)`` (would kill the API).

    Allowed: kill by *project* port (e.g. ``lsof -ti:3001``).
    """
    cmd = command or ""
    if not cmd.strip():
        return None

    img = _IMAGE_KILL_RE.search(cmd)
    if img:
        name = next((g for g in img.groups() if g), "node/python")
        return (
            f"Refusing to kill process image '{name}' — that hosts the "
            f"HiveWeave platform (API :4000 / UI :5173). "
            f"Stop *project* servers by port only "
            f"(e.g. `kill $(lsof -ti:3001)` / "
            f"`npx kill-port 3001`), never taskkill/pkill "
            f"{'/'.join(sorted({i.removesuffix('.exe') for i in PROTECTED_PROCESS_IMAGES}))}."
        )

    if _KILL_VERB_RE.search(cmd) and _RESERVED_PORT_REF_RE.search(cmd):
        ports = ",".join(str(p) for p in sorted(RESERVED_PORTS))
        return (
            f"Refusing to kill processes on HiveWeave reserved ports "
            f"({ports}). Use a project port (3000+) instead."
        )

    return None


def allocate_project_port(project_id: str, preferred: int = 3000) -> int:
    """Pick first free-looking port starting at preferred (skip reserved)."""
    hydrate_registry()
    used = {r.port for r in _registry.values() if r.project_id == project_id}
    used |= {r.port for r in _registry.values()}
    port = preferred
    while port in RESERVED_PORTS or port in used:
        port += 1
        if port > 3999:
            port = 3000
            break
    return port


def prepare_spawn_command(
    command: str,
    *,
    project_id: str | None = None,
    preferred_port: int = 3000,
) -> tuple[str, dict[str, str], str | None]:
    """P2 process proxy: rewrite/guard command + inject reserved-port env.

    Returns (command, extra_env, error_message).
    """
    extra_env = {
        "HIVEWEAVE_RESERVED_PORTS": ",".join(
            str(p) for p in sorted(RESERVED_PORTS)
        ),
        "HIVEWEAVE_FORBID_PORTS": ",".join(
            str(p) for p in sorted(RESERVED_PORTS)
        ),
    }

    # Explicit reserved port → hard reject
    for port in extract_ports_from_command(command):
        if is_reserved_port(port):
            return (
                command,
                {},
                (
                    f"Port {port} is reserved for HiveWeave platform "
                    f"(API/UI). Use a project port (e.g. 3000+) via "
                    f"start_dev_server, not --port {port}."
                ),
            )

    ports = extract_ports_from_command(command)
    if ports:
        return command, extra_env, None

    # Bare vite/npm run dev → allocate project port (don't leave as 5173)
    if _VITE_BARE_RE.search(command or ""):
        pid = project_id or "default"
        port = allocate_project_port(pid, preferred_port)
        extra_env["PORT"] = str(port)
        extra_env["VITE_PORT"] = str(port)
        if "vite" in (command or "").lower():
            rewritten = f"{command.rstrip()} --port {port} --strictPort"
        else:
            rewritten = f"PORT={port} {command}"
        log.info(
            "spawn_proxy_rewrote_vite",
            project_id=project_id,
            port=port,
            original=(command or "")[:80],
        )
        return rewritten, extra_env, None

    return command, extra_env, None


def spawn_project_process(
    command: str,
    *,
    cwd: str,
    project_id: str | None = None,
    preferred_port: int = 3000,
    env: dict[str, str] | None = None,
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen | None, str | None, dict[str, Any]]:
    """Spawn with reserved-port proxy. Returns (proc, error, meta)."""
    cmd, extra_env, err = prepare_spawn_command(
        command, project_id=project_id, preferred_port=preferred_port
    )
    if err:
        return None, err, {}

    child_env = os.environ.copy()
    for key in ("PORT", "VITE_PORT"):
        val = child_env.get(key)
        if val and val.isdigit() and is_reserved_port(int(val)):
            child_env.pop(key, None)
    if env:
        child_env.update(env)
    child_env.update(extra_env)

    creationflags = popen_kwargs.pop("creationflags", 0)
    if os.name == "nt":
        from hiveweave.util.win_subprocess import (
            merge_creationflags,
            windows_no_window_kwargs,
        )
        import subprocess as _sp

        base = creationflags or getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags = merge_creationflags(base)
        # Hidden console for the whole tree — CREATE_NO_WINDOW alone would
        # let console grandchildren (node/bun/vite) allocate visible windows.
        popen_kwargs.setdefault(
            "startupinfo", windows_no_window_kwargs().get("startupinfo")
        )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=True,
            env=child_env,
            creationflags=creationflags,
            **popen_kwargs,
        )
    except Exception as e:
        return None, f"Failed to spawn: {e}", {}

    meta = {
        "command": cmd,
        "cwd": cwd,
        "pid": proc.pid,
        "env_port": child_env.get("PORT") or child_env.get("VITE_PORT"),
    }
    return proc, None, meta


def register(record: ProcessRecord) -> ProcessRecord:
    hydrate_registry()
    if is_reserved_port(record.port):
        raise ValueError(f"Cannot register reserved port {record.port}")
    key = f"{record.project_id}:{record.port}"
    _registry[key] = record
    _persist_registry()
    log.info(
        "process_registered",
        project_id=record.project_id,
        port=record.port,
        pid=record.pid,
        cwd=record.cwd[:120],
    )
    return record


def unregister(project_id: str, port: int) -> None:
    hydrate_registry()
    _registry.pop(f"{project_id}:{port}", None)
    _persist_registry()


def lookup_by_port(port: int) -> list[ProcessRecord]:
    hydrate_registry()
    return [r for r in _registry.values() if r.port == port]


def lookup_by_project(project_id: str) -> list[ProcessRecord]:
    hydrate_registry()
    return [r for r in _registry.values() if r.project_id == project_id]


def stop_processes_for_worktree(worktree_path: str) -> dict:
    """Stop all registered processes whose cwd is under *worktree_path*.

    Called before worktree teardown to release file locks (WinError 32).
    Returns ``{stopped: [...], failed: [...]}``.
    """
    hydrate_registry()
    norm_wt = os.path.normcase(os.path.normpath(worktree_path))
    norm_wt_sep = norm_wt + os.sep  # prefix with separator to avoid A003 matching A0031
    stopped: list[dict] = []
    failed: list[dict] = []

    to_check = [
        (key, rec)
        for key, rec in _registry.items()
        if rec.cwd and (
            os.path.normcase(os.path.normpath(rec.cwd)) == norm_wt
            or os.path.normcase(os.path.normpath(rec.cwd)).startswith(norm_wt_sep)
        )
    ]
    for key, rec in to_check:
        if not rec.pid or not _is_pid_alive(rec.pid):
            # Already dead — just unregister
            _registry.pop(key, None)
            stopped.append({"port": rec.port, "pid": rec.pid, "status": "already_dead"})
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(rec.pid)],
                    capture_output=True, timeout=10,
                )
            else:
                import signal
                os.kill(rec.pid, signal.SIGTERM)
            _registry.pop(key, None)
            stopped.append({"port": rec.port, "pid": rec.pid, "status": "killed"})
            log.info(
                "process_stopped_for_worktree",
                port=rec.port, pid=rec.pid, worktree=worktree_path[:120],
            )
        except Exception as e:
            failed.append({"port": rec.port, "pid": rec.pid, "error": str(e)})
            log.warning(
                "process_stop_failed_for_worktree",
                port=rec.port, pid=rec.pid, error=str(e),
            )

    if stopped or failed:
        _persist_registry()
    return {"stopped": stopped, "failed": failed}


def clear_registry_for_tests() -> None:
    _registry.clear()
    global _hydrated
    _hydrated = False
    try:
        if _REGISTRY_PATH.exists():
            _REGISTRY_PATH.unlink()
    except Exception:
        pass
