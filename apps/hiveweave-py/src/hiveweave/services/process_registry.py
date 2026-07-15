"""Project process registry + platform reserved ports (P0).

Full process proxy is P2. P0: refuse reserved binds in agent tools and
register start_dev_server entries for URL→cwd/pid lookup.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# HiveWeave platform — project apps must never bind these
RESERVED_PORTS: frozenset[int] = frozenset({4000, 5173, 4173})

_PORT_FLAG_RE = re.compile(
    r"(?:--port[= ]|--listen[= ]|-p[= ])(\d{2,5})",
    re.IGNORECASE,
)
_PORT_ENV_RE = re.compile(
    r"(?:PORT|VITE_PORT)\s*=\s*(\d{2,5})",
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


# In-memory registry (per server process). Persisted DB optional later.
_registry: dict[str, ProcessRecord] = {}  # key: f"{project_id}:{port}"


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


def allocate_project_port(project_id: str, preferred: int = 3000) -> int:
    """Pick first free-looking port starting at preferred (skip reserved)."""
    used = {r.port for r in _registry.values() if r.project_id == project_id}
    used |= {r.port for r in _registry.values()}
    port = preferred
    while port in RESERVED_PORTS or port in used:
        port += 1
        if port > 3999:
            port = 3000
            break
    return port


def register(record: ProcessRecord) -> ProcessRecord:
    if is_reserved_port(record.port):
        raise ValueError(f"Cannot register reserved port {record.port}")
    key = f"{record.project_id}:{record.port}"
    _registry[key] = record
    log.info(
        "process_registered",
        project_id=record.project_id,
        port=record.port,
        pid=record.pid,
        cwd=record.cwd[:120],
    )
    return record


def unregister(project_id: str, port: int) -> None:
    _registry.pop(f"{project_id}:{port}", None)


def lookup_by_port(port: int) -> list[ProcessRecord]:
    return [r for r in _registry.values() if r.port == port]


def lookup_by_project(project_id: str) -> list[ProcessRecord]:
    return [r for r in _registry.values() if r.project_id == project_id]


def clear_registry_for_tests() -> None:
    _registry.clear()
