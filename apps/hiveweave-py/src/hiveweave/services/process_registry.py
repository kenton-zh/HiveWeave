"""Project process registry + platform reserved ports (P0/P2).

P0: refuse reserved binds in agent tools and register start_dev_server.
P2: spawn_project_process injects reserved-port env and rewrites known CLIs.
TEST21 M11: persist registry to JSON; hydrate + prune dead PIDs on lookup.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hiveweave.util.win_subprocess import Popen

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
# 裸 dev server（vite / npm run dev / pnpm dev）。
# ⚠ **段首锚是必需项，不是风格选择**：无锚时 `\bvite\b` 会命中 `.vite` **目录名**
# 与 `vite.config.ts` **文件名**（词边界在 `.` 与 `v` 之间成立），平台据此把整条
# 命令尾部追 `--port <P> --strictPort` ⇒ 命令变非法，agent 看到的是 pwsh
# 「找不到接受自变量 <port> 的参数」，归因指向命令本身。
# 实证 TEST_DSH_65：本项目 11 次（6 次可见失败 + 5 次静默）、跨项目 120 条 / 21 项目。
# 与 `_FLASK_RUN_FAMILY_RE` / `_UVICORN_FAMILY_RE` 同族取齐（二者各有段首锚）。
_SEGMENT_START_RE = r"(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*"
# vite 的**非服务子命令**：build / optimize / --version / --help 跑完即退，
# 不是长驻服务 —— 用**负向前瞻**约束在 `vite` 之后。
# ⚠ `preview` **不在**本清单：它是**长驻**静态服务器（预览生产构建），
# 默认 4173 亦在 `RESERVED_PORTS` 内 ⇒ 必须照常被判据拦下。
# ⚠ 不能用「对整条命令 search 阻塞动词」来排除（那正是原先的写法）：
# `npm install && npm run dev` 里别处的 `install` 会让排除生效 ⇒ 裸起 5173，
# 等于用一个新文本旁路换掉旧的；注释里写个 `# TODO test` 也能解除拒斥。
# 约束必须**贴着它要约束的那一段**。
# ⚠ 本表是**子命令 token 表**（B 档），但名字不带 `_NEEDLES/_PATTERNS/…` 后缀、
# 也不是 `re.compile(...)` 常量 ⇒ **落在文本判据棘轮的扫描面之外**（本仓已知缺口，
# 第三轮审计 P3）。这是**故意**的：它约束的是「`vite` 后面紧跟哪个子命令」这种
# 半结构化 token 位，不是自由自然语言的意图推断。若日后棘轮扩面到此形态，
# 应把它改名为 `_VITE_NON_SERVER_SUBCMD_NEEDLES` 并入基线，而不是靠它「看不见」。
_VITE_NON_SERVER_SUBCMD_NEG = (
    r"(?!\s+(?:build|optimize|--version|-v|--help|-h)\b)"
)
_BARE_DEV_SERVER_RE = re.compile(
    _SEGMENT_START_RE + r"(?:npx\s+)?vite\b" + _VITE_NON_SERVER_SUBCMD_NEG
    # `dev\b(?!:…)`：用**紧邻负向前瞻**排除 `dev:test` / `dev:lint` 这类
    # 「跑完即退」的脚本变体。
    # ⚠ 不要用 `dev(?:\s|$)` 这种尾部约束 —— 它会把 `npm run dev:staging`、
    # `npm run dev-server` 一起放行，而那些**是**长驻服务（旧 `\bdev\b` 会拒）
    # ⇒ 净新增漏网（第三轮审计 P2）。
    + r"|" + _SEGMENT_START_RE
    + r"(?:npm|pnpm)\s+(?:run\s+)?dev\b"
    + r"(?!:(?:test|spec|lint|build|e2e|check|typecheck)\b)",
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
        from hiveweave.util.win_subprocess import hidden_run

        if os.name == "nt":
            r = hidden_run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return parse_netstat_listen_ports(r.stdout or "", tree)
        pid_list = ",".join(str(p) for p in sorted(tree))
        r = hidden_run(
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
        from hiveweave.util.win_subprocess import hidden_run

        hidden_run(
            ["taskkill", "/F", "/T", "/PID", str(n)],
            capture_output=True,
            timeout=10,
        )
    else:
        import signal

        os.kill(n, signal.SIGTERM)


def terminate_spawned(proc: Popen | None) -> None:
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
    # 裸 dev server 无显式端口 ⇒ 会默认启在 5173（平台保留端口）。
    # ⚠ 判据必须是 `_BARE_DEV_SERVER_RE`，**不得**回退到 `"vite" in lower`
    # 这种裸子串：它会连 `.vite` 目录名 / `vite.config.ts` 文件名一起拒掉 ——
    # 那只是把「误改写」换成「误拒绝」，同一个洞换张脸。
    # ⚠⚠ 「有限输出子命令」的排除**内化在判据里**（`vite` 后负向前瞻 +
    # `dev(?:\s|$)` 尾部约束），**不再**用 `_SPAWN_BLOCKING_VERB_RE` 对整条
    # 命令 search —— 那种写法可绕过：`npm install && npm run dev` 里别处的
    # `install` 会让排除生效 ⇒ 裸起 5173（审计 2026-09-21 P1-b），
    # 注释里写个 `# TODO test` 同样能解除拒斥。
    lower = (command or "").lower()
    if (
        _BARE_DEV_SERVER_RE.search(command or "")
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


# 管道/分号分隔符 — 注入位置敏感标记。注入 flag 必须插在 runner 之后、
# 分隔符之前；无法定位 runner token 时 tail-append 会把 flag 交给管道
# 下游 cmdlet（F2 根因：`pytest … | Select-Object` 的尾部 --ignore 被
# Select-Object 当位置参数拒收）。


def _has_pipe_or_seq(command: str) -> bool:
    """命令含管道 / 分号分隔（未加引号段）。F2：含则注入位置必须敏感。"""
    cmd = command or ""
    quote: str | None = None
    for ch in cmd:
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch in ("|", ";"):
            return True
    return False


def _inject_hiveweave_test_exclude(command: str) -> tuple[str, str | None]:
    """Inject runner-specific excludes so .hiveweave/ worktrees don't pollute.

    Returns ``(rewritten_command, injection_note)`` — ``injection_note`` is
    None when no injection was attempted, otherwise a human-readable note
    describing what the platform rewrote (F2：让 Agent 看得见这双手)。

    Runner 定位纪律（audit P2 沿革）：
    - 在**全部**匹配里挑「后跟空白」的干净 token，取**最后一个** —— 复合
      命令把真正的 runner 调用放在前面的安装段之后（``pip install pytest
      && python -m pytest tests/`` → 注入 pytest 段，不碰 pytest-cov）。
    - 一个匹配后跟 ``-``/``.``/``;``/``/`` 是长名的一部分（pytest-cov、
      pytest.main()、import pytest;），不算 runner 调用，跳过继续找。
    - 找不到干净 token 时：命令含管道/分号 → **放弃注入并告警**（F2：tail
      会把 flag 交给管道下游 cmdlet，宁可不注入）；无管道 → tail-append
      依旧安全，保留历史 fallback。
    """
    cmd = command or ""
    if ".hiveweave" in cmd.lower() and (
        "--exclude" in cmd or "--ignore" in cmd or "testPathIgnore" in cmd
    ):
        return cmd, None

    def _try_inject(match_re: re.Pattern[str], flags: str, tool: str) -> tuple[str, str | None]:
        # 最后一个「后跟空白」的干净 runner 匹配
        chosen: re.Match[str] | None = None
        for m in match_re.finditer(cmd):
            nxt = cmd[m.end() : m.end() + 1]
            if nxt == "" or nxt in " \t\n":
                chosen = m
        if chosen is None:
            # F2：找不到干净 runner token —— 含管道/分号时放弃注入并告警；
            # 无管道时 tail-append 安全（历史 fallback 保留）。
            if _has_pipe_or_seq(cmd):
                return cmd, (
                    f"[platform injection skipped] cannot locate a clean "
                    f"{tool} runner token to insert `{flags}` before a "
                    f"pipe/separator — left the command untouched rather "
                    f"than corrupt it."
                )
            return f"{cmd.rstrip()} {flags}", None
        return (
            f"{cmd[: chosen.end()].rstrip()} {flags} "
            f"{cmd[chosen.end() :].lstrip()}"
        ).strip(), f"[platform injected] {flags} after {tool} runner."

    if _VITEST_RE.search(cmd) and "--exclude" not in cmd:
        return _try_inject(_VITEST_RE, f"--exclude {_HIVEWEAVE_EXCLUDE_GLOB}", "vitest")
    if _JEST_RE.search(cmd) and "testPathIgnorePatterns" not in cmd:
        return _try_inject(
            _JEST_RE, "--testPathIgnorePatterns=\\.hiveweave", "jest"
        )
    if _PYTEST_RE.search(cmd) and "--ignore" not in cmd and "--ignore-glob" not in cmd:
        return _try_inject(
            _PYTEST_RE,
            "--ignore=.hiveweave --ignore-glob=**/.hiveweave/**",
            "pytest",
        )
    return cmd, None


def prepare_spawn_command(
    command: str,
    *,
    project_id: str | None = None,
    preferred_port: int = 3000,
    routed_as_dev_server: bool = False,
) -> tuple[str, dict[str, str], str | None, dict | None]:
    """P2 process proxy: rewrite/guard command + inject reserved-port env.

    Returns ``(command, extra_env, error_message, injection_meta)``.
    ``injection_meta``（F2）: ``{note, injected, final_command}`` 描述平台
    对命令的改写 —— 让调用方把「平台动了什么」回显给 Agent
    （result_excerpt），改写可见才不会被当成模型/命令自身的问题。

    ``routed_as_dev_server``：调用方**已按 dev-server 判定路由**本命令时置 True
    （`bash.py` 的 `_DEV_SERVER_TRIGGER_RE` 命中后走注册 spawn 路径）。
    置 True 后若命令最终仍无端口，本函数**拒绝**而非放行 —— 见下方兜底注释。
    加这个参数的原因：上游路由判据比本模块判据**宽**，两者不齐会漏网。
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

    # P1-8a 新-④（2026-09-21）：可移植性别名归一化 —— `python3`→`python` /
    # `pip3`→`pip`。本路径最后经 `hidden_popen(cmd, shell=True)`（Windows 上是
    # cmd /c）起**长驻进程**，此前**完全不归一化** ⇒ `python3 -m uvicorn …` 在
    # Windows 上要么落 Store stub、要么落到 PATH 上恰好存在的某个解释器，
    # 与 bash 工具那四个分支（Git Bash / cmd / unix / pwsh）语义不一致。
    # ⚠ `skip_cmd_mapping=True`：只做别名，**不做** unix→cmd 动词映射
    # （命令是起服务的，动词映射会改坏参数语义；与 pwsh 分支同口径）。
    from hiveweave.tools.bash import _normalize_command  # 惰性：避免 tools↔services 环

    command = _normalize_command(command, skip_cmd_mapping=True)

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
                None,
            )

    # 裸 dev server 且无显式端口 → 拒斥 + 给唯一出口（**不再代其改写**）。
    #
    # 原先本函数另有一块「嗅探到 vite 后替 agent 尾部追加 `--port`」，两条害处：
    #   ① 判据无段首锚 ⇒ 误命中 `.vite` 目录名 / `vite.config.ts` 文件名，
    #      把正常命令改成非法命令（P0-1，跨项目 120 条 / 21 项目）；
    #   ② 它抢在拒斥之前命中 ⇒ agent **永远收不到**「用 start_dev_server」
    #      这条处方，于是学不会走显式通道，下一轮照样手写（循环不破）。
    # 该块已删除，改由本处拒斥 + 给处方接管。
    #
    # 带端口的命令不受影响：走下方 `if ports` 分支提前 return
    # （`start_dev_server` 走的正是带端口那条）。
    _bare_dev_err = check_command_reserved_ports(command)
    if _bare_dev_err:
        return command, {}, _bare_dev_err, None

    # TEST6 P0-3: exclude in-tree worktrees from glob test runners
    pre_inject_command = command
    injected_command, injection_note = _inject_hiveweave_test_exclude(command)
    command = injected_command
    injection_meta: dict | None = None
    if injection_note:
        injection_meta = {
            "note": injection_note,
            "injected": injected_command != pre_inject_command,
            "final_command": command,
        }

    ports = extract_ports_from_command(command)
    if ports:
        return command, extra_env, None, injection_meta

    # 原「裸 vite → 代 agent 尾部追加 `--port`」块已移除（2026-09-21，P0-1），
    # 理由见上方 `check_command_reserved_ports` 拒斥处的注释。

    # 裸 uvicorn 默认 8000，不在 reserved 内，但仍分配 3000+ 并注入 --port
    # （已有 --port/-p/PORT= 的上面已 return）。
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
        return rewritten, extra_env, None, injection_meta

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
        return command, extra_env, None, injection_meta

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
        return rewritten, extra_env, None, injection_meta

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
        return rewritten, extra_env, None, injection_meta

    # ⚠⚠ 兜底：**不依赖任何文本判据**，且必须排在**所有注入分支之后**。
    #
    # 调用方已按 dev-server 判定路由本命令（`bash.py` 的
    # `_DEV_SERVER_TRIGGER_RE` 命中 ⇒ 走注册 spawn 路径以便可杀），
    # 但命令里既无显式端口、上面也没有任何分支给它注入端口 ⇒ 放行会
    # **裸起在默认端口**（vite 默认 5173 = 平台保留端口）⇒ 撞平台。
    #
    # ⚠ 位置曾放错一次（2026-09-21 复审 P1）：早先放在四个注入分支**之前**，
    # 于是 `uvicorn app.main:app` / `flask run` / `python -m app.server` 这些
    # **由下面分支负责注入端口**的命令，在 `extra_env["PORT"]` 尚未设置时就被
    # 判「无端口」⇒ 工具路径（恒传 routed=True）全部被拒死。**必须在最后。**
    #
    # 为什么单靠判据堵不住漏认：判据宽窄只能调误报/漏报配比，`npx --yes vite`
    # 这类包装形态永远可能漏 —— 但「路由方说有端口、最终却没有」是个
    # **结构性事实**，与措辞无关。
    # `_UVICORN_HELP_RE` 排除：`--help/-h/--version` 跑完即退，即便被判据
    # 漏认成 dev server 也不该拒（它们不需要端口）。
    if (
        routed_as_dev_server
        and not extract_ports_from_command(command)
        and not extra_env.get("PORT")
        and not _UVICORN_HELP_RE.search(command or "")
    ):
        return (
            command,
            {},
            (
                "Refusing to start a dev server without an explicit project "
                "port — the default (vite 5173) is reserved for HiveWeave. "
                "Use start_dev_server or pass "
                "`--port <project_port> --strictPort`."
            ),
            None,
        )

    return command, extra_env, None, injection_meta


def spawn_project_process(
    command: str,
    *,
    cwd: str,
    project_id: str | None = None,
    preferred_port: int = 3000,
    env: dict[str, str] | None = None,
    routed_as_dev_server: bool = False,
    **popen_kwargs: Any,
) -> tuple[Popen | None, str | None, dict[str, Any]]:
    """Spawn with reserved-port proxy. Returns (proc, error, meta)."""
    cmd, extra_env, err, _inj_meta = prepare_spawn_command(
        command,
        project_id=project_id,
        preferred_port=preferred_port,
        routed_as_dev_server=routed_as_dev_server,
    )
    if err:
        return None, err, {}

    # 白名单 env：不 copy 父进程（会把 API 密钥带进 dev server）。
    # 不用 bash 的 HIVEWEAVE_BASH 标记 —— spawn 不是 bash 工具。
    from hiveweave.util.safe_env import build_child_env
    from hiveweave.util.win_subprocess import hidden_popen

    child_env = build_child_env(cwd or "", bash_markers=False)
    if env:
        child_env.update(env)
    child_env.update(extra_env)
    creationflags = popen_kwargs.pop("creationflags", 0)
    if os.name == "nt":
        from hiveweave.util.win_subprocess import (
            CREATE_NEW_PROCESS_GROUP,
            merge_creationflags,
        )

        base = creationflags or CREATE_NEW_PROCESS_GROUP
        creationflags = merge_creationflags(base)
        # Hidden console for the whole tree — CREATE_NO_WINDOW alone would
        # let console grandchildren (node/bun/vite) allocate visible windows.
        # hidden_popen injects the SW_HIDE startupinfo when none is supplied.

    try:
        proc = hidden_popen(
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
