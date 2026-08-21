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
import threading
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
# python -m http.server [flags] <port>：位置参数端口（http.server 无 --port
# 旗标；argparse 允许 --cgi/-b/--bind/-d/--directory/-p/--protocol 等旗标
# 出现在位置端口之前，须按 arity 跳过再取端口）
_HTTP_SERVER_POS_PORT_RE = re.compile(
    r"\bhttp\.server\s+"
    r"(?:(?:--cgi|--bind\s+\S+|--directory\s+\S+|--protocol\s+\S+"
    r"|-[bdp]\s+\S+)\s+)*"
    r"(\d{2,5})\b",
    re.IGNORECASE,
)
# gunicorn --bind 0.0.0.0:3000 / -b :3000 / --bind 8000
_GUNICORN_BIND_PORT_RE = re.compile(
    r"(?:--bind|-b)[= ]\s*(?:(?:\[[^\]]+\]|[\w.-]+):|:)?(\d{2,5})\b",
    re.IGNORECASE,
)
_VITE_BARE_RE = re.compile(
    r"\b(npx\s+)?vite\b|\bnpm\s+run\s+dev\b|\bpnpm\s+(?:run\s+)?dev\b",
    re.IGNORECASE,
)
# python -m app.server / python app/server.py (module-style; not tests)
# Do NOT inject --port: app.server may not accept uvicorn flags.
_APP_SERVER_FAMILY_RE = re.compile(
    r"(?:"
    r"(?:pythonw?|python3)(?:\.exe)?\s+-m\s+app\.server\b"
    r"|(?:pythonw?|python3)(?:\.exe)?(?:\s+-[^\s]+)*\s+"
    r"(?:['\"]?)(?:\.[/\\])?app[/\\]server\.py\b"
    r")",
    re.IGNORECASE,
)
# flask run / python -m flask [--app x] run / uv run flask run
# Segment-start only for bare flask (not `echo flask run`).
_FLASK_CLI_FLAGS = (
    r"(?:\s+(?:--[\w-]+(?:[=\s][^\s;|&]+)?|-[A-Za-z](?:\s+[^\s;|&]+)?))*"
)
_FLASK_RUN_FAMILY_RE = re.compile(
    r"(?:"
    r"(?:pythonw?|python3)(?:\.exe)?\s+-m\s+flask\b"
    r"|uv\s+run\b(?:\s+\S+)*?\s+flask\b"
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?flask\b"
    r")"
    + _FLASK_CLI_FLAGS
    + r"\s+run\b",
    re.IGNORECASE,
)
_UV_DEP_FLAG_RE = re.compile(
    r"--(?:with|extra|group|package)\s+\S+",
    re.IGNORECASE,
)
# gunicorn / python -m gunicorn / uv run gunicorn. Not --with gunicorn.
_GUNICORN_FAMILY_RE = re.compile(
    r"(?:"
    r"(?:pythonw?|python3)(?:\.exe)?\s+-m\s+gunicorn\b"
    r"|uv\s+run\b(?:\s+\S+)*?\s+(?<!\s--with\s)(?<!\s--extra\s)(?<!\s--group\s)(?<!\s--package\s)gunicorn(?:\s+\S|$)"
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?gunicorn(?:\s+\S)"
    r")",
    re.IGNORECASE,
)
# uvicorn / python -m uvicorn / uv run … uvicorn
# 裸 uvicorn 仅段首（含 VAR=val），避免 --with uvicorn / pip show uvicorn。
_UVICORN_FAMILY_RE = re.compile(
    r"(?:"
    r"(?:^|\s|;|&|\|)`?(?:"
    r"(?:pythonw?|python3)\s+-m\s+uvicorn\b"
    r"|uv\s+run\b(?:\s+\S+)*?\s+(?<!\s--with\s)(?<!\s--extra\s)(?<!\s--group\s)(?<!\s--package\s)uvicorn(?:\s+\S|$)"
    r")"
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?uvicorn(?:\s+\S)"
    r")",
    re.IGNORECASE,
)
_UVICORN_HELP_RE = re.compile(
    r"(?:^|\s)(?:--help|-h|--version)(?:\s|$)",
    re.IGNORECASE,
)
_SPAWN_BLOCKING_VERB_RE = re.compile(
    r"\b(?:build|test|lint|install|ci|audit|eject|deploy)\b",
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
# 阻塞调用（netstat/taskkill）经 asyncio.to_thread 下放线程池后，
# 注册表会被事件循环线程与 executor 线程并发访问 —— 全部读写走此锁。
_REGISTRY_LOCK = threading.RLock()


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def is_pid_alive(pid: int | None) -> bool:
    """True if *pid* is a live OS process (Windows OpenProcess / POSIX kill 0)."""
    return _is_pid_alive(pid)


def uv_dep_consumed_token(command: str, token: str) -> bool:
    """True if *token* appears only as a uv --with/--extra/--group/--package value."""
    if not token or not re.search(rf"\b{re.escape(token)}\b", command or "", re.I):
        return False
    stripped = _UV_DEP_FLAG_RE.sub(" ", command or "")
    return not re.search(rf"\b{re.escape(token)}\b", stripped, re.I)


def _ppid_map() -> dict[int, int]:
    """pid → parent pid. Empty on failure."""
    mapping: dict[int, int] = {}
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x2

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            kernel32.CreateToolhelp32Snapshot.argtypes = [
                wintypes.DWORD, wintypes.DWORD,
            ]
            kernel32.Process32FirstW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if not snapshot or snapshot == ctypes.c_void_p(-1).value:
                return {}
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                while ok:
                    mapping[int(entry.th32ProcessID)] = int(
                        entry.th32ParentProcessID
                    )
                    ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
        else:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                pid = int(name)
                try:
                    with open(
                        f"/proc/{pid}/stat", "r", encoding="ascii", errors="ignore"
                    ) as f:
                        stat = f.read()
                    tail = stat.rsplit(")", 1)[-1].split()
                    mapping[pid] = int(tail[1])
                except Exception:
                    continue
    except Exception as e:
        log.debug("ppid_map_failed", error=str(e))
        return {}
    return mapping


def descendant_pids(root: int, ppid_map: dict[int, int] | None = None) -> set[int]:
    """*root* plus descendants. On failure, *root* only (never invent pids)."""
    try:
        n = int(root)
    except (TypeError, ValueError):
        return set()
    if n <= 0:
        return set()
    if ppid_map is None:
        try:
            import psutil  # optional

            kids = {int(c.pid) for c in psutil.Process(n).children(recursive=True)}
            return {n, *kids}
        except Exception:
            ppid_map = _ppid_map()
    mapping = ppid_map
    children: dict[int, list[int]] = {}
    for pid, ppid in mapping.items():
        children.setdefault(int(ppid), []).append(int(pid))
    out = {n}
    stack = [n]
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, ()):
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out


def parse_netstat_listen_ports(stdout: str, pids: set[int]) -> list[int]:
    """Parse `netstat -ano -p tcp` LISTENING rows whose last column is in *pids*."""
    want = {str(p) for p in pids}
    ports: list[int] = []
    for line in (stdout or "").splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 4 or parts[-1] not in want:
            continue
        addr = parts[1]
        if ":" not in addr:
            continue
        try:
            port = int(addr.rsplit(":", 1)[-1].rstrip("]"))
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.append(port)
    return sorted(set(ports))


def listening_ports_for_pid(pid: int | None) -> list[int]:
    """TCP LISTEN ports owned by *pid* or its descendants. Empty on failure."""
    if not pid:
        return []
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    tree = descendant_pids(n)
    if not tree:
        tree = {n}
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return parse_netstat_listen_ports(r.stdout or "", tree)
        pid_list = ",".join(str(p) for p in sorted(tree))
        r = subprocess.run(
            [
                "lsof", "-nP", "-a", "-p", pid_list,
                "-iTCP", "-sTCP:LISTEN",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        ports: list[int] = []
        for m in re.finditer(r":(\d{2,5})\s+\(LISTEN\)", r.stdout or ""):
            port = int(m.group(1))
            if 1 <= port <= 65535:
                ports.append(port)
        return sorted(set(ports))
    except Exception as e:
        log.debug("listening_ports_for_pid_failed", pid=n, error=str(e))
        return []


def pick_observed_listen_port(
    pid: int | None, preferred: int | None = None
) -> int | None:
    """First non-reserved LISTEN port for *pid*; prefer *preferred* if bound."""
    found = listening_ports_for_pid(pid)
    usable = [p for p in found if not is_reserved_port(p)]
    if not usable:
        return None
    if preferred:
        try:
            pref = int(preferred)
        except (TypeError, ValueError):
            pref = 0
        if pref in usable:
            return pref
    return usable[0]


def _pid_is_protected(pid: int) -> bool:
    """True if *pid* is this process, parent, or command_guard protected set."""
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return True
    if n <= 0:
        return True
    if n == os.getpid() or n == os.getppid():
        return True
    try:
        from hiveweave.services.command_guard import protected_pids

        return n in protected_pids()
    except Exception:
        # Fail closed: unknown protection set → do not kill.
        return True


def _protected_pids_or_none() -> set[int] | None:
    """command_guard protected set; ``None`` when unavailable (caller decides)."""
    try:
        from hiveweave.services.command_guard import protected_pids

        return protected_pids()
    except Exception:
        return None


def _record_is_stale_protected(pid: Any) -> bool:
    """Retention decision for hydrate/prune — NOT the kill decision.

    只丢「可证实」属于平台自身的记录（self/parent/守护集成员）；守护集
    不可用时保留记录（retention fail-open）。若此处与 `_pid_is_protected`
    一样 fail-closed，hydrate/prune 会在守护集失效时静默清空整个注册表
    （审计 2026-08-17 MAJOR-2）。误杀防线不受影响：kill 路径仍由
    `_pid_is_protected` fail-closed 兜底。
    """
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return True  # pid 非数字的损坏记录 → 丢弃
    if n <= 0:
        return True
    if n == os.getpid() or n == os.getppid():
        return True
    guarded = _protected_pids_or_none()
    if guarded is None:
        return False
    return n in guarded


def _kill_pid(pid: int) -> None:
    """Kill *pid* and its process tree. Windows: taskkill /F /T /PID (never /IM)."""
    n = int(pid)
    if _pid_is_protected(n):
        log.warning("process_kill_refused_protected_pid", pid=n)
        raise PermissionError(f"refusing to kill protected pid {n}")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(n)],
            capture_output=True,
            timeout=10,
        )
    else:
        import signal

        os.kill(n, signal.SIGTERM)


def terminate_spawned(proc: subprocess.Popen | None) -> None:
    """Kill a spawn_project_process tree. Best-effort; never raises."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        _kill_pid(int(pid))
        return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


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
    with _REGISTRY_LOCK:
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
                try:
                    rec = ProcessRecord.from_dict(val)
                    if rec.port and is_reserved_port(int(rec.port)):
                        continue
                    if _record_is_stale_protected(rec.pid):
                        continue
                    if rec.port and _is_pid_alive(rec.pid):
                        loaded[key] = rec
                except Exception:
                    continue  # 单条损坏只丢该条，不中止整个加载
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
    with _REGISTRY_LOCK:
        dead = [
            k for k, r in _registry.items()
            if not _is_pid_alive(r.pid)
            or (r.port and is_reserved_port(int(r.port)))
            or _record_is_stale_protected(r.pid)
        ]
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
    for m in _HTTP_SERVER_POS_PORT_RE.finditer(command or ""):
        ports.append(int(m.group(1)))
    if re.search(r"\bgunicorn\b", command or "", re.IGNORECASE):
        for m in _GUNICORN_BIND_PORT_RE.finditer(command or ""):
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
    """Pick first free-looking port starting at preferred (skip reserved).

    Prunes dead PIDs first so stale registry rows do not occupy ports.
    """
    prune_dead_processes()
    used = {r.port for r in _registry.values() if r.project_id == project_id}
    used |= {r.port for r in _registry.values()}
    port = preferred
    while port in RESERVED_PORTS or port in used:
        port += 1
        if port > 3999:
            port = 3000
            break
    return port


# TEST6 P0-3: worktrees live under .hiveweave/worktrees/ — glob runners
# (vitest/jest/pytest) must not pick up sibling agent WIP as "main" failures.
_HIVEWEAVE_EXCLUDE_GLOB = "**/.hiveweave/**"
_VITEST_RE = re.compile(r"\b(?:npx\s+)?vitest\b", re.IGNORECASE)
_JEST_RE = re.compile(r"\b(?:npx\s+)?jest\b", re.IGNORECASE)
_PYTEST_RE = re.compile(
    r"\b(?:python3?\s+-m\s+)?pytest\b|\buv\s+run\s+pytest\b",
    re.IGNORECASE,
)


def _inject_hiveweave_test_exclude(command: str) -> str:
    """Append runner-specific excludes so .hiveweave/ worktrees don't pollute."""
    cmd = command or ""
    if ".hiveweave" in cmd.lower() and (
        "--exclude" in cmd or "--ignore" in cmd or "testPathIgnore" in cmd
    ):
        return cmd
    if _VITEST_RE.search(cmd) and "--exclude" not in cmd:
        return f"{cmd.rstrip()} --exclude {_HIVEWEAVE_EXCLUDE_GLOB}"
    if _JEST_RE.search(cmd) and "testPathIgnorePatterns" not in cmd:
        return (
            f"{cmd.rstrip()} --testPathIgnorePatterns=\\.hiveweave"
        )
    if _PYTEST_RE.search(cmd) and "--ignore" not in cmd and "--ignore-glob" not in cmd:
        return f"{cmd.rstrip()} --ignore=.hiveweave --ignore-glob=**/.hiveweave/**"
    return cmd


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

    # 尾部 & 会让后续注入的 --port 落到后台作业之外；spawn 路径已脱离前台。
    command = re.sub(r"\s*&+\s*$", "", (command or "").strip()).strip()

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

    # TEST6 P0-3: exclude in-tree worktrees from glob test runners
    command = _inject_hiveweave_test_exclude(command)

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

    # 裸 uvicorn 默认 8000，不在 reserved 内，但仍分配 3000+ 并注入 --port
    # （与 vite 一样走 allocate_project_port；已有 --port/-p/PORT= 的上面已 return）。
    if (
        _UVICORN_FAMILY_RE.search(command or "")
        and not uv_dep_consumed_token(command or "", "uvicorn")
        and not _SPAWN_BLOCKING_VERB_RE.search(command or "")
        and not _UVICORN_HELP_RE.search(command or "")
    ):
        pid = project_id or "default"
        port = allocate_project_port(pid, preferred_port)
        extra_env["PORT"] = str(port)
        rewritten = f"{command.rstrip()} --port {port}"
        log.info(
            "spawn_proxy_rewrote_uvicorn",
            project_id=project_id,
            port=port,
            original=(command or "")[:80],
        )
        return rewritten, extra_env, None

    # python -m app.server: long-running, killable via registry. Do NOT
    # inject --port (app.server may not accept uvicorn flags). PORT env only
    # when the command has no --port/-p already (handled above).
    if (
        _APP_SERVER_FAMILY_RE.search(command or "")
        and not _SPAWN_BLOCKING_VERB_RE.search(command or "")
        and not _UVICORN_HELP_RE.search(command or "")
    ):
        pid = project_id or "default"
        port = allocate_project_port(pid, preferred_port)
        extra_env["PORT"] = str(port)
        log.info(
            "spawn_proxy_app_server_port_env",
            project_id=project_id,
            port=port,
            original=(command or "")[:80],
        )
        return command, extra_env, None

    # flask run: default 5000. Inject --port (flask accepts it) + PORT env.
    if (
        _FLASK_RUN_FAMILY_RE.search(command or "")
        and not uv_dep_consumed_token(command or "", "flask")
        and not _SPAWN_BLOCKING_VERB_RE.search(command or "")
        and not _UVICORN_HELP_RE.search(command or "")
    ):
        pid = project_id or "default"
        port = allocate_project_port(pid, preferred_port)
        extra_env["PORT"] = str(port)
        rewritten = f"{command.rstrip()} --port {port}"
        log.info(
            "spawn_proxy_rewrote_flask",
            project_id=project_id,
            port=port,
            original=(command or "")[:80],
        )
        return rewritten, extra_env, None

    # gunicorn: default 8000. Inject --bind, not --port.
    if (
        _GUNICORN_FAMILY_RE.search(command or "")
        and not uv_dep_consumed_token(command or "", "gunicorn")
        and not _SPAWN_BLOCKING_VERB_RE.search(command or "")
        and not _UVICORN_HELP_RE.search(command or "")
    ):
        pid = project_id or "default"
        port = allocate_project_port(pid, preferred_port)
        extra_env["PORT"] = str(port)
        rewritten = f"{command.rstrip()} --bind 0.0.0.0:{port}"
        log.info(
            "spawn_proxy_rewrote_gunicorn",
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

    # 白名单 env：不 copy 父进程（会把 API 密钥带进 dev server）。
    # 不用 bash 的 HIVEWEAVE_BASH 标记 —— spawn 不是 bash 工具。
    from hiveweave.util.safe_env import build_child_env

    child_env = build_child_env(cwd or "", bash_markers=False)
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
        cwd_path = Path(cwd) if cwd else None
        log.warning(
            "process_spawn_failed",
            error=str(e),
            cwd=cwd,
            cwd_exists=str(cwd_path.exists()) if cwd_path else "n/a",
            cwd_is_dir=str(cwd_path.is_dir()) if cwd_path else "n/a",
            cwd_parent_exists=(
                str(cwd_path.parent.exists()) if cwd_path else "n/a"
            ),
        )
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
    if record.pid and _pid_is_protected(int(record.pid)):
        raise ValueError(f"Cannot register protected pid {record.pid}")
    with _REGISTRY_LOCK:
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
    with _REGISTRY_LOCK:
        _registry.pop(f"{project_id}:{port}", None)
        _persist_registry()


def lookup_by_port(port: int) -> list[ProcessRecord]:
    hydrate_registry()
    with _REGISTRY_LOCK:
        return [r for r in _registry.values() if r.port == port]


def lookup_by_project(project_id: str) -> list[ProcessRecord]:
    hydrate_registry()
    with _REGISTRY_LOCK:
        return [r for r in _registry.values() if r.project_id == project_id]


def stop_process_by_port(project_id: str, port: int) -> dict:
    """Stop registry records for THIS project+port only.

    Reuses taskkill /F /T /PID (never /IM). Unregisters after. Never
    kills HiveWeave reserved ports. Other projects' processes on the
    same port are left untouched.
    """
    hydrate_registry()
    stopped: list[dict] = []
    failed: list[dict] = []
    if is_reserved_port(int(port)):
        log.warning(
            "process_stop_refused_reserved_port",
            project_id=project_id,
            port=port,
        )
        return {
            "stopped": [],
            "failed": [
                {
                    "port": int(port),
                    "error": (
                        f"Refusing to kill reserved HiveWeave port {port}"
                    ),
                }
            ],
        }
    key = f"{project_id}:{int(port)}"
    with _REGISTRY_LOCK:
        rec = _registry.get(key)
        if rec is None or rec.project_id != project_id or rec.port != int(port):
            return {"stopped": [], "failed": []}
        if not rec.pid or not _is_pid_alive(rec.pid):
            _registry.pop(key, None)
            _persist_registry()
            return {
                "stopped": [
                    {"port": rec.port, "pid": rec.pid, "status": "already_dead"}
                ],
                "failed": [],
            }
        try:
            _kill_pid(int(rec.pid))
            _registry.pop(key, None)
            _persist_registry()
            stopped.append({"port": rec.port, "pid": rec.pid, "status": "killed"})
            log.info(
                "process_stopped_by_port",
                project_id=project_id,
                port=rec.port,
                pid=rec.pid,
            )
        except Exception as e:
            failed.append({"port": rec.port, "pid": rec.pid, "error": str(e)})
            log.warning(
                "process_stop_failed_by_port",
                project_id=project_id,
                port=rec.port,
                pid=rec.pid,
                error=str(e),
            )
    return {"stopped": stopped, "failed": failed}


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

    to_check: list[tuple[str, ProcessRecord]] = []
    with _REGISTRY_LOCK:
        to_check = [
            (key, rec)
            for key, rec in _registry.items()
            if rec.cwd and (
                os.path.normcase(os.path.normpath(rec.cwd)) == norm_wt
                or os.path.normcase(os.path.normpath(rec.cwd)).startswith(norm_wt_sep)
            )
        ]
        for key, rec in to_check:
            if rec.port and is_reserved_port(int(rec.port)):
                _registry.pop(key, None)
                failed.append({
                    "port": rec.port,
                    "pid": rec.pid,
                    "error": (
                        f"Refusing to kill reserved HiveWeave port {rec.port}"
                    ),
                })
                continue
            if not rec.pid or not _is_pid_alive(rec.pid):
                # Already dead — just unregister
                _registry.pop(key, None)
                stopped.append({"port": rec.port, "pid": rec.pid, "status": "already_dead"})
                continue
            try:
                _kill_pid(int(rec.pid))
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


def stop_processes_for_project(project_id: str) -> dict:
    """Stop all registered processes for a project (any cwd, incl. main).

    TEST6 evening P2-6: main-checkout dev servers are not bound to a
    worktree, so worktree teardown never kills them. Call on deactivate
    / project stop.
    """
    hydrate_registry()
    stopped: list[dict] = []
    failed: list[dict] = []
    with _REGISTRY_LOCK:
        to_check = [
            (key, rec)
            for key, rec in list(_registry.items())
            if rec.project_id == project_id
        ]
        for key, rec in to_check:
            if rec.port and is_reserved_port(int(rec.port)):
                _registry.pop(key, None)
                failed.append({
                    "port": rec.port,
                    "pid": rec.pid,
                    "error": (
                        f"Refusing to kill reserved HiveWeave port {rec.port}"
                    ),
                })
                continue
            if not rec.pid or not _is_pid_alive(rec.pid):
                _registry.pop(key, None)
                stopped.append(
                    {"port": rec.port, "pid": rec.pid, "status": "already_dead"}
                )
                continue
            try:
                _kill_pid(int(rec.pid))
                _registry.pop(key, None)
                stopped.append(
                    {"port": rec.port, "pid": rec.pid, "status": "killed"}
                )
                log.info(
                    "process_stopped_for_project",
                    project_id=project_id,
                    port=rec.port,
                    pid=rec.pid,
                )
            except Exception as e:
                failed.append(
                    {"port": rec.port, "pid": rec.pid, "error": str(e)}
                )
                log.warning(
                    "process_stop_failed_for_project",
                    project_id=project_id,
                    port=rec.port,
                    pid=rec.pid,
                    error=str(e),
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
