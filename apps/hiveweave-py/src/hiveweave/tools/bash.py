"""Bash tool — shell command execution with sandbox + self-destruct guard.

契约 02: 工具执行器 — bash 子模块
- 执行 shell 命令（Windows: 优先 Git Bash bash -c，无 Git Bash 时降级 cmd /s /c）
- POSIX: bash -c
- 120s 默认超时（max 600s），超时强制终止
- 路径沙箱：workdir 必须在 workspace_path 内
- 自毁命令拦截：7 个正则模式（rm -rf /, format, diskpart, shutdown, reboot, poweroff, halt）
- 输出截断：> 1MB 截断并追加标记（轻量截断，不存盘）
- Docker sandbox 选项（BASH_SANDBOX=docker，预留接口）
- 环境变量注入 HIVEWEAVE_BASH=1 + HIVEWEAVE_WORKSPACE=<cwd>
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

DEFAULT_TIMEOUT_S = 120          # 2 minutes
MAX_TIMEOUT_S = 600              # 10 minutes hard cap
MAX_CAPTURE_BYTES = 1_048_576    # 1MB — bash 专用轻量截断阈值
# P2-1 fix: 非零退出时返回 stdout/stderr 各自的尾部 4KB（tail 而非 head），
# 让 agent 看到真正的报错信息（编译错误、堆栈通常在输出末尾），避免盲目重试。
ERROR_TAIL_BYTES = 4_096

# D4: Per-(agent_id, cwd) consecutive failure counter. When an agent keeps
# getting non-zero exits in the same directory (even with different flags/args),
# we append guidance after CWD_FAILURE_STREAK_THRESHOLD consecutive failures.
_cwd_failure_streak: dict[tuple[str, str], int] = {}
CWD_FAILURE_STREAK_THRESHOLD = 5
_CWD_FAILURE_STREAK_MAX_ENTRIES = 200

_CWD_FAILURE_HINT = (
    "\n\n[HINT: {n} consecutive failures in this directory. "
    "Read the full error output above carefully — the root cause is likely stated there. "
    "Consider a fundamentally different approach instead of retrying variations. "
    "Verify the working directory is correct. "
    "If stuck, use message_peer to ask a colleague for help.]"
)

# ANSI 转义序列（颜色 / 光标控制）。Windows 下 Git Bash、cmd 及许多 CLI 会
# 输出 VT 颜色码，原样回传给 LLM 会污染上下文，需在尾部截断后剥离。
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
DOCKER_SANDBOX_IMAGE = "hiveweave/sandbox:latest"

# P0-3 增量2 (audit 2026-07-28): long-running dev-server commands run forever
# and lock node_modules. When spawned via bash they were never registered, so
# stop_processes_for_worktree couldn't kill them → WinError 32 on worktree
# teardown. Detect such commands and route them to the registered spawn path
# (same mechanism start_dev_server uses) so the process is trackable/killable.
_DEV_SERVER_TRIGGER_RE = re.compile(
    r"(?:^|\s|;|&|\|)`?(?:"
    r"(?:npx\s+)?vite(?:\s|$)"               # vite / npx vite (bare = dev server)
    r"|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:dev|start)(?:\s|$)"
    r"|bun\s+(?:run\s+)?(?:dev|start)(?:\s|$)"
    r"|next\s+dev(?:\s|$)"
    r"|nuxt\s+dev(?:\s|$)"
    r"|nodemon\b"
    r")",
    re.IGNORECASE,
)
# Blocking verbs that produce finite output — NOT dev servers (vite build,
# npm run build, npm test, etc.). Their presence disqualifies auto-routing.
_BLOCKING_VERB_RE = re.compile(
    r"\b(?:build|test|lint|install|ci|audit|eject|deploy)\b",
    re.IGNORECASE,
)


def _detect_dev_server_command(command: str) -> int | None:
    """Return port (0 = allocate) if *command* is a long-running dev server,
    or ``None`` if it should run through the normal blocking path.

    Dev servers never produce finite output — blocking on them just times out
    and orphans the process. Routing them to the registered spawn path makes
    them killable by ``stop_processes_for_worktree`` (fixes WinError 32).
    """
    if not command or not command.strip():
        return None
    # Strip trailing background operators — the registered spawn already
    # detaches; a literal `&` would background inside the shell and orphan.
    cmd = re.sub(r"\s*&+\s*$", "", command.strip()).strip()
    if not cmd:
        return None
    if not _DEV_SERVER_TRIGGER_RE.search(cmd):
        return None
    # Disqualify blocking verbs (vite build, npm run build:test, …).
    if _BLOCKING_VERB_RE.search(cmd):
        return None
    # Disqualify commands that pipe/redirect into a finite sink, e.g.
    # `vite --port 3000 > log.txt 2>&1 & echo done` — the agent intended a
    # background spawn with a captured log, not an interactive server. We
    # still register those, but only when there's no `echo`/`cat` tail that
    # implies the agent wants the shell to return with other output.
    from hiveweave.services.process_registry import extract_ports_from_command

    ports = extract_ports_from_command(cmd)
    return ports[0] if ports else 0


async def _run_registered_dev_server(
    command: str,
    cwd: str,
    workspace_path: str,
    project_id: str | None,
    port_hint: int,
) -> dict[str, Any] | None:
    """Spawn a dev server via the registered path (non-blocking, tracked).

    Mirrors ``start_dev_server``: allocate port, spawn via
    ``spawn_project_process``, register to ``process_registry`` with the
    worktree cwd, return immediately. Returns ``None`` to fall through to the
    normal blocking path if spawning fails to start.
    """
    from hiveweave.services.process_registry import (
        ProcessRecord,
        allocate_project_port,
        is_reserved_port,
        register,
        spawn_project_process,
    )

    pid = project_id or "default"
    port = port_hint if (port_hint and not is_reserved_port(port_hint)) else (
        allocate_project_port(pid, 3000)
    )
    if is_reserved_port(port):
        return {
            "success": False, "output": "",
            "error": (
                f"Refusing to start dev server on reserved platform port "
                f"{port}. Use start_dev_server or a project port (3000+)."
            ),
        }

    try:
        proc, spawn_err, meta = spawn_project_process(
            command,
            cwd=cwd,
            project_id=project_id,
            preferred_port=port,
        )
    except Exception as e:
        log.warning(
            "bash.dev_server_spawn_failed",
            error=str(e), command=command[:120], cwd=cwd[:120],
        )
        return None  # fall through to normal path
    if spawn_err or proc is None:
        log.warning(
            "bash.dev_server_spawn_error",
            error=spawn_err, command=command[:120], cwd=cwd[:120],
        )
        return None  # fall through — let normal path surface the error

    commit = ""
    try:
        import subprocess as _sp

        r = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if r.returncode == 0:
            commit = (r.stdout or "").strip()
    except Exception:
        pass

    try:
        register(ProcessRecord(
            project_id=project_id or "",
            port=port,
            pid=proc.pid,
            cwd=cwd,
            command=meta.get("command") or command,
            commit=commit,
        ))
    except Exception as e:
        log.warning(
            "bash.dev_server_register_failed",
            error=str(e), pid=proc.pid, port=port, cwd=cwd[:120],
        )

    log.info(
        "bash.dev_server_auto_registered",
        pid=proc.pid, port=port, cwd=cwd[:120],
        command=(meta.get("command") or command)[:120],
    )
    return {
        "success": True,
        "output": (
            f"[hiveweave] Dev server auto-registered from bash.\n"
            f"  pid={proc.pid} port={port} cwd={cwd}\n"
            f"  command: {meta.get('command') or command}\n"
            f"  URL: http://localhost:{port}/\n"
            f"  This process is tracked — stop_processes_for_worktree will "
            f"kill it on worktree teardown. Use lookup_dev_server to inspect.\n"
            f"  (Routed from bash because the command is a long-running dev "
            f"server; blocking on it would time out and orphan the process.)\n"
            f"\nExit code: 0"
        ),
        "error": None,
    }

# 环境变量白名单 — 只传系统必要变量给子进程，绝不传递任何含
# KEY/SECRET/TOKEN/PASSWORD 的变量（C5: 防止 API 密钥泄露）。
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "TEMP", "TMP", "SystemRoot", "COMSPEC", "LANG", "LC_ALL",
    "LC_CTYPE", "TERM", "SHELL", "USERNAME", "USERDOMAIN",
    "COMPUTERNAME", "OS", "PATHEXT", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # Python runtime support — not secrets, needed for venv/pip to work
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING",
    "PYTHONUTF8",
    # Node.js runtime support
    "NODE_PATH", "NODE_OPTIONS",
    # Proxy settings (needed for network access in tools)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})


def _source_env_sh(command: str, hw_dir: str) -> str:
    """Prepend .hiveweave/env.sh sourcing if the file exists.

    The project declares its own environment (venv, Docker alias, PATH, etc.)
    in a single shell script. HiveWeave just sources it before every command.
    No guessing — the project knows what it needs.

    Example .hiveweave/env.sh:
        [ -d .hiveweave/venv ] || python3 -m venv .hiveweave/venv
        source .hiveweave/venv/bin/activate
        export NODE_PATH="$PWD/.hiveweave/node_modules"
    """
    env_file = f"{hw_dir}/env.sh"
    if not os.path.exists(env_file):
        return command
    # Source env.sh, then run the command in the same shell
    return f"source {env_file} && {command}"


def _build_safe_env(cwd: str) -> dict[str, str]:
    """构建白名单环境变量，仅传递系统必要变量 + HiveWeave 标记。

    绝不传递 OPENAI_API_KEY / OPENCODE_API_KEY / DEEPSEEK_API_KEY 等密钥。
    Windows 环境变量大小写不敏感（Path 与 PATH 等价），白名单匹配也必须
    大小写不敏感，否则可能漏传 PATH（系统常存为 'Path'）。
    """
    safe_keys_upper = {k.upper() for k in _SAFE_ENV_KEYS}
    safe_env = {k: v for k, v in os.environ.items() if k.upper() in safe_keys_upper}
    safe_env["HIVEWEAVE_BASH"] = "1"
    safe_env["HIVEWEAVE_WORKSPACE"] = cwd
    # Force UTF-8 everywhere — prevents GBK encoding crashes on Windows
    # when agent output contains emoji or CJK extension chars (✅, 🚀, etc.)
    safe_env["PYTHONIOENCODING"] = "utf-8"
    safe_env["PYTHONUTF8"] = "1"
    safe_env["LANG"] = "en_US.UTF-8"
    safe_env["LC_ALL"] = "en_US.UTF-8"
    return safe_env

# Self-destructive command patterns (契约 02 — 7 patterns)
# Match semantics mirror Elixir check_self_destructive/1:
#   patterns 1-2 use word-boundary-anchored regex
#   patterns 3-6 use substring matching (intentional, mirrors Elixir)
#   pattern 7 uses word boundary on "halt"
SELF_DESTRUCTIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/"),            # rm -rf /
    re.compile(r"format\s+[a-z]:", re.I),   # format C:
    re.compile(r"diskpart", re.I),          # Windows disk partitioning (substring)
    re.compile(r"shutdown", re.I),          # OS shutdown (substring)
    re.compile(r"reboot", re.I),            # OS reboot (substring)
    re.compile(r"poweroff", re.I),          # OS poweroff (substring)
    re.compile(r"\bhalt\b", re.I),          # halt (word boundary)
]


def check_self_destructive(command: str) -> tuple[bool, str]:
    """Return (blocked, reason). blocked=True if command is destructive."""
    for pattern in SELF_DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return True, "system-level destructive command"
    return False, ""


# .hiveweave 系统目录保护的文件操作命令前缀。
# cd .hiveweave 不拦（无害），只拦真正会读/写/删/复制文件的命令。
_HIVEWEAVE_FILE_OPS = re.compile(
    r"\b(?:rm|del|erase|cat|type|cp|copy|mv|move|xcopy|robocopy|"
    r"echo|printf|tee|dd|truncate|strings|xxd|hexdump|od|base64|"
    r"touch|mkdir|rmdir|rd|ln|link|chmod|chown|attrib|cacls|"
    r"sqlite3|\.sqlite3|open|export|tar|zip|unzip|gzip|gunzip|"
    r"7z|rar|dump|backup|restore|import|load)\b",
    re.IGNORECASE,
)
_HIVEWEAVE_REF = re.compile(r"\.hiveweave\b", re.IGNORECASE)

# 放行的 .hiveweave 子目录 — agent 可在这些子目录内执行文件操作
# 与 file.py 的 allowed_subdirs 保持一致
_ALLOWED_HW_SUBDIRS = re.compile(
    r"\.hiveweave[\\/]+(?:shared|reports|drafts|worktrees)[\\/]",
    re.IGNORECASE,
)


def _check_hiveweave_command(command: str) -> bool:
    """Return True if the command targets `.hiveweave` with a file operation.

    拦截 agent 通过 bash 读写/删除/复制 .hiveweave 内系统文件（data.db 等）。
    `cd .hiveweave` 和 `ls .hiveweave` 这类无害命令不拦。
    放行指向 shared/reports/drafts/worktrees 子目录的文件操作（团队共享空间）。
    """
    if not _HIVEWEAVE_REF.search(command):
        return False
    if not _HIVEWEAVE_FILE_OPS.search(command):
        return False
    # 放行明确指向允许子目录的操作
    if _ALLOWED_HW_SUBDIRS.search(command):
        return False
    return True


def _extract_file_paths_from_command(command: str) -> list[str]:
    """从 bash 命令中提取可能的文件路径参数。

    Bug C-2 fix: 只提取重定向目标 (>, >>) 和行首命令的路径参数。
    不再匹配 heredoc 内容中的代码（如 setPassword(...)），
    避免 is_sensitive_path 误判。
    """
    paths: list[str] = []
    # 1. 重定向目标 (>, >>) — 只匹配 shell 重定向，不匹配代码中的 > =>
    # 按行处理，避免跨行匹配
    for line in command.split('\n'):
        line = line.strip()
        if not line:
            continue
        # 匹配 > 或 >> 后面的文件名
        # 排除: <=>, =>, >=, -> 以及 heredoc 标记 <<
        redirect_re = re.compile(r'(?<![<=>-])>(?:>)?\s+(\S+)')
        for m in redirect_re.finditer(line):
            token = m.group(1)
            # 跳过管道符、控制字符和代码 token
            if token in ('&', '|', '&&', '||', ';'):
                continue
            # 跳过含括号的 token（是代码不是文件路径）
            if '(' in token or ')' in token:
                continue
            paths.append(token)
    # 2. 按行分割，只检查每行开头的命令
    for line in command.split('\n'):
        line = line.strip()
        if not line:
            continue
        # 跳过 heredoc 内容行（不以命令开头的行）
        parts = line.split()
        if not parts:
            continue
        # 检查行首是否是文件操作命令
        file_cmds = {'cat', 'cp', 'mv', 'rm', 'touch', 'mkdir', 'chmod',
                     'chown', 'source', 'head', 'tail', 'less', 'more',
                     'tee', 'dd', 'ln'}
        cmd = parts[0].lower()
        # 处理 sudo 前缀
        if cmd == 'sudo' and len(parts) > 1:
            parts = parts[1:]
            cmd = parts[0].lower()
        if cmd in file_cmds:
            for part in parts[1:]:
                if part.startswith('-') or part in ('&&', '||', ';', '|', '&'):
                    continue
                # 跳过明显是代码的 token（含 = 或括号）
                if '=' in part or '(' in part or ')' in part:
                    continue
                paths.append(part)
    return paths


def _validate_command_safety(command: str) -> tuple[bool, str]:
    """统一命令安全校验 — 所有 shell 执行入口必须调用。

    整合: 自毁命令、敏感路径、.hiveweave 系统目录、平台端口/进程保护。
    Returns: (blocked, reason) — blocked=True 表示命令应被拦截。

    Bug C fix: is_sensitive_path 只检查提取出的文件路径参数，
    不再检查整个命令字符串。避免代码内容中包含 password/token
    等词时被误判为敏感文件引用。
    """
    blocked, reason = check_self_destructive(command)
    if blocked:
        return True, f"Command blocked: {reason}"
    from hiveweave.services.process_registry import check_platform_process_kill

    plat_err = check_platform_process_kill(command)
    if plat_err:
        return True, plat_err
    from hiveweave.tools.security import is_sensitive_path
    # Bug C fix: 只检查命令中的文件路径参数，不检查整个命令字符串
    file_paths = _extract_file_paths_from_command(command)
    for fp in file_paths:
        if is_sensitive_path(fp):
            return True, (f"Command references a sensitive file: {fp} "
                          f"(e.g. .env, *.pem, id_rsa, credentials). "
                          f"Use read_file with explicit approval instead.")
    if _check_hiveweave_command(command):
        return True, ("Command targets `.hiveweave` system directory. "
                      "System files (data.db, tool_outputs/) are managed by "
                      "HiveWeave internals.")
    return False, ""


def _is_within_workspace(candidate: str, workspace: str) -> bool:
    """Check whether `candidate` path stays inside `workspace` (after resolve)."""
    try:
        ws = Path(workspace).resolve()
        cand = Path(candidate).resolve()
    except (OSError, ValueError):
        return False
    if cand == ws:
        return True
    try:
        cand.relative_to(ws)
        return True
    except ValueError:
        return False


def _truncate_output(output: str) -> str:
    """Light-weight truncation: cap at 1MB (layer 2, bash-specific).

    P1 修复：不再直接截断丢数据。当输出超过 1MB 时，保留 head + tail 预览，
    并提示完整输出已由 ToolExecutor layer 1 存盘。
    （layer 1 阈值 50KB 会先于 layer 2 触发存盘）
    """
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURE_BYTES:
        return output
    # 超过 1MB — 保留 head 50 行 + tail 20 行
    lines = output.split("\n")
    if len(lines) <= 100:
        # 行数不多但单行超长（如 minified JS），按字符截断
        truncated = encoded[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
        return truncated + f"\n... [output truncated at 1MB, {len(encoded)} bytes total]"
    head = "\n".join(lines[:50])
    tail = "\n".join(lines[-20:])
    total = len(lines)
    return (
        f"{head}\n"
        f"\n... [{total - 70} lines omitted, {len(encoded)} bytes total. "
        f"See tool output file for full content] ...\n\n"
        f"{tail}"
    )


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences (color / cursor control).

    P2-1 fix: Windows 下 Git Bash / cmd 及许多 CLI（npm、pytest、tsc）会输出
    VT 颜色码，原样回传给 LLM 会污染上下文且浪费 token。剥离后再返回。
    在 POSIX 上调用也无害（无转义序列时原样返回）。
    """
    if not text:
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def _error_tail(text: str) -> str:
    """Return the LAST 4KB of a stream (tail, not head) for error reporting.

    P2-1 fix: 命令失败时真正的报错信息（编译错误、堆栈、缺失依赖提示）几乎
    总在输出末尾。此前只回首行导致 agent 看不到原因而盲目重试。改为返回尾部
    4KB，并先剥离 ANSI 转义序列。
    """
    if not text:
        return ""
    cleaned = _strip_ansi(text)
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) <= ERROR_TAIL_BYTES:
        return cleaned
    tail = encoded[-ERROR_TAIL_BYTES:].decode("utf-8", errors="replace")
    dropped = len(encoded) - ERROR_TAIL_BYTES
    return f"... [{dropped} earlier bytes omitted, showing last {ERROR_TAIL_BYTES} bytes]\n{tail}"


def _update_cwd_failure_streak(agent_id: str, cwd: str, success: bool) -> str:
    """D4: Track consecutive failures per (agent_id, cwd) and return hint text.

    Returns the hint string when the streak reaches CWD_FAILURE_STREAK_THRESHOLD,
    empty string otherwise. Never blocks execution — purely advisory.
    """
    key = (agent_id, cwd)
    if success:
        _cwd_failure_streak.pop(key, None)
        return ""
    # Simple bounded eviction: clear all if dict grows too large
    if len(_cwd_failure_streak) > _CWD_FAILURE_STREAK_MAX_ENTRIES:
        _cwd_failure_streak.clear()
    count = _cwd_failure_streak.get(key, 0) + 1
    _cwd_failure_streak[key] = count
    if count >= CWD_FAILURE_STREAK_THRESHOLD:
        return _CWD_FAILURE_HINT.format(n=count)
    return ""


# ── Git Bash detection (Windows) ────────────────────────────
# P1 fix(TEST11-R3): Windows 下优先探测 Git Bash，用 bash -c 执行命令，
# 根治 cmd 不支持管道/变量赋值/&&复合/bash script.sh 的固有限制。
# cmd 映射降级为无 Git Bash 环境的兜底方案。

_BASH_EXE_PATH: str | None = None
_BASH_EXE_CHECKED: bool = False


def _find_bash_exe() -> str | None:
    """Detect Git Bash (bash.exe) on Windows. Result cached after first call."""
    global _BASH_EXE_PATH, _BASH_EXE_CHECKED
    if _BASH_EXE_CHECKED:
        return _BASH_EXE_PATH
    _BASH_EXE_CHECKED = True

    import shutil

    # 1. PATH 上直接有 bash（Git for Windows 安装后默认加入 PATH）
    #    排除 WSL 的 bash（C:\Windows\System32\bash.exe）——它在 Linux 子系统
    #    中执行，路径语义不同（/mnt/c/... vs C:\...），会导致文件操作失败。
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        _BASH_EXE_PATH = found
        log.info("git_bash_detected", source="PATH", path=found)
        return found

    # 2. 常见 Git for Windows 安装路径
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            _BASH_EXE_PATH = path
            log.info("git_bash_detected", source="known_path", path=path)
            return path

    log.debug("git_bash_not_found")
    return None


def _normalize_command(command: str, *, skip_cmd_mapping: bool = False) -> str:
    """Pre-process command for cross-platform compatibility.

    - python3 → python (Windows: python3.exe doesn't exist; Unix: alias if absent)
    - pip3 → pip
    - P2 fix(TEST10): Windows 下常见 unix 命令映射到 cmd 等价物
    - P2 fix(TEST11-R3): 带 unix 风格 flag 的命令不映射，避免参数错乱
    """
    import re
    # Replace python3/pip3 with python/pip (word-boundary safe)
    cmd = re.sub(r'\bpython3\b', 'python', command)
    cmd = re.sub(r'\bpip3\b', 'pip', cmd)

    # Windows: map common unix commands to cmd equivalents (fallback path only)
    if sys.platform.startswith("win") and not skip_cmd_mapping:
        cmd = _map_unix_to_cmd(cmd)
    return cmd


def _map_unix_to_cmd(cmd: str) -> str:
    """Map unix commands to cmd equivalents with parameter protection.

    P2 fix(TEST11-R3): 带 unix 风格 flag（-x / --xxx / -N）的命令不映射，
    避免 ls -la → dir /b -la、mkdir -p → md -p、tail -30 → more -30 等错误。
    仅对无 flag 的简单调用做映射（ls → dir /b, cat f → type f）。
    """
    import re

    _UNIX_TO_CMD: dict[str, str] = {
        'ls': 'dir /b',
        'cat': 'type',
        'head': 'more',   # 近似
        'tail': 'more',   # 近似
        'cp': 'copy',
        'mv': 'move',
        'mkdir': 'md',
        'pwd': 'cd',
        'which': 'where',
        'clear': 'cls',
    }

    def _replacer(m: re.Match) -> str:
        unix_cmd = m.group(0)
        # 扫描当前命令段（到下一个 | / && / || / ; 为止）的参数 token
        rest = cmd[m.end():]
        seg_end = len(rest)
        for sep in ('|', '&&', '||', ';'):
            idx = rest.find(sep)
            if 0 <= idx < seg_end:
                seg_end = idx
        tokens = rest[:seg_end].split()
        # 任何 token 以 - 开头 → 有 unix flag → 不映射
        if any(t.startswith('-') for t in tokens):
            return unix_cmd
        return _UNIX_TO_CMD[unix_cmd]

    pattern = r'\b(?:' + '|'.join(re.escape(k) for k in _UNIX_TO_CMD) + r')\b'
    return re.sub(pattern, _replacer, cmd)


def _decode_output(raw: bytes) -> str:
    """P2 fix(TEST10): 解码子进程输出。

    优先 UTF-8（env 已设 PYTHONIOENCODING=utf-8），失败时回退到系统
    locale 编码（中文 Windows 为 GBK/CP936）。避免 cmd.exe 原生命令
    （dir/type/findstr）输出乱码。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        pass
    # Fallback: system locale (GBK on zh-CN Windows)
    try:
        import locale
        enc = locale.getpreferredencoding(False) or "gbk"
        return raw.decode(enc, errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


async def _run_native(command: str, cwd: str, timeout_s: int) -> dict[str, Any]:
    """Execute command via the OS native shell.

    P1 fix(TEST11-R3): Windows 下优先使用 Git Bash（bash -c），
    根治 cmd 不支持管道/变量赋值/&&复合/bash script.sh 的固有限制。
    无 Git Bash 时降级为 cmd /s /c + 命令映射兜底。
    """
    is_windows = sys.platform.startswith("win")
    if is_windows:
        bash_exe = _find_bash_exe()
        if bash_exe:
            # Git Bash 可用 — 直接执行，仅做 python3→python 等基础规范化
            command = _normalize_command(command, skip_cmd_mapping=True)
            shell_args = [bash_exe, "-c", command]
        else:
            # 无 Git Bash — cmd 兜底，启用 unix→cmd 命令映射
            command = _normalize_command(command)
            shell_args = ["cmd", "/s", "/c", command]
    else:
        command = _normalize_command(command)
        shell_args = ["bash", "-c", command]

    env = _build_safe_env(cwd)

    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            *shell_args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            **windows_no_window_kwargs(),
        )
    except FileNotFoundError as exc:
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": False,
                "error": f"Failed to spawn shell: {exc}"}
    except OSError as exc:
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": False,
                "error": f"Failed to spawn shell: {exc}"}

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        # Force-kill the process tree
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": True, "error": None}

    stdout = _decode_output(stdout_bytes) if stdout_bytes else ""
    stderr = _decode_output(stderr_bytes) if stderr_bytes else ""
    # 成功路径仍返回合并 output（保持原有行为）；失败路径用分离的
    # stdout/stderr 各自取尾部 4KB（P2-1 fix）。
    combined = stdout + ("\n" + stderr if stdout and stderr else stderr)
    return {
        "output": combined,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "timed_out": False,
        "error": None,
    }


async def _run_docker(command: str, cwd: str, timeout_s: int) -> dict[str, Any]:
    """Execute command inside a Docker sandbox container.

    BASH_SANDBOX=docker enables this path. Mounts the workspace read-write
    at /workspace inside the container. Best-effort: if docker is unavailable,
    falls back to native execution with a warning.
    """
    docker_cmd = [
        "docker", "run", "--rm",
        "-w", "/workspace",
        "-v", f"{cwd}:/workspace",
        "-e", "HIVEWEAVE_BASH=1",
        "-e", "HIVEWEAVE_WORKSPACE=/workspace",
        "--network", "host",
        DOCKER_SANDBOX_IMAGE,
        "sh", "-c", command,
    ]

    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            **windows_no_window_kwargs(),
        )
    except FileNotFoundError:
        log.warning("bash.docker_unavailable", reason="docker binary not found")
        return await _run_native(command, cwd, timeout_s)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": True, "error": None}

    stdout = _decode_output(stdout_bytes) if stdout_bytes else ""
    stderr = _decode_output(stderr_bytes) if stderr_bytes else ""
    combined = stdout + ("\n" + stderr if stdout and stderr else stderr)
    return {
        "output": combined,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "timed_out": False,
        "error": None,
    }


def _cwd_style_hint(cwd: str) -> str:
    """Human-readable cwd note for agents (Git Bash style on Windows)."""
    try:
        p = Path(cwd).resolve()
        native = str(p)
    except (OSError, ValueError):
        native = cwd
    posix = native.replace("\\", "/")
    # D:/foo → /d/foo for Git Bash copy-paste
    msys = posix
    if len(posix) >= 2 and posix[1] == ":":
        msys = "/" + posix[0].lower() + posix[2:]
    return (
        f"[cwd={native} | Git Bash style: {msys} — "
        f"use this or quoted Windows paths; never invent /workspace]"
    )


async def execute_bash(
    command: str,
    workdir: str,
    workspace_path: str,
    timeout_ms: int | None = None,
    use_docker: bool | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Execute a bash command and return {success, output, error}.

    Performs:
      1. Self-destructive command check (7 patterns)
      2. Sandbox validation (workdir must be within workspace)
      3. Timeout clamping (1s..600s)
      4. Execute (persistent sandbox > one-shot docker > native)
      5. Truncate output at 1MB (layer 2, bash-specific)
    """
    if not command or not command.strip():
        return {"success": False, "output": "",
                "error": "Error: command is required"}

    # 1. 统一命令安全校验 — 自毁命令 + 敏感路径 + .hiveweave 系统目录
    blocked, reason = _validate_command_safety(command)
    if blocked:
        log.warning("bash.blocked", reason=reason, command_preview=command[:120])
        return {"success": False, "output": "",
                "error": f"Error: {reason}"}

    # 1.5. Auto-source .hiveweave/env.sh if the project has one.
    # The project declares its own environment setup.
    hw_dir = str(Path(workspace_path) / ".hiveweave")
    command = _source_env_sh(command, hw_dir)

    # 2. Resolve cwd and validate sandbox
    ws = workspace_path or os.getcwd()
    if workdir:
        cwd = str(Path(ws) / workdir)
    else:
        cwd = ws

    if not _is_within_workspace(cwd, ws):
        return {"success": False, "output": "",
                "error": "Error: Sandbox violation - workdir must be within workspace"}

    if not Path(cwd).exists():
        return {"success": False, "output": "",
                "error": f"Error: Working directory does not exist: {cwd}"}

    cwd_hint = _cwd_style_hint(cwd)

    # P0-3 增量2 (audit 2026-07-28): route long-running dev-server commands to
    # the registered spawn path so stop_processes_for_worktree can kill them.
    # Bash-spawned dev servers were unregistered → WinError 32 on teardown.
    port_hint = _detect_dev_server_command(command)
    if port_hint is not None and not use_docker:
        routed = await _run_registered_dev_server(
            command, cwd, workspace_path, project_id, port_hint
        )
        if routed is not None:
            return routed
        # None = spawn failed to start; fall through to normal blocking path
        # so the agent sees the real error instead of a silent no-op.

    # 3. Clamp timeout
    if timeout_ms is None:
        timeout_ms = DEFAULT_TIMEOUT_S * 1000
    timeout_ms = int(timeout_ms)
    # Heuristic: values 1-600 are likely seconds, not milliseconds
    if 1 <= timeout_ms <= 600:
        timeout_ms = timeout_ms * 1000
    timeout_ms = max(5000, min(timeout_ms, MAX_TIMEOUT_S * 1000))
    timeout_s = timeout_ms / 1000

    # 4. Choose execution backend (priority: persistent sandbox > one-shot docker > native)
    result = None

    # 4. Choose execution backend
    if use_docker:
        result = await _run_docker(command, cwd, int(timeout_s))
    else:
        result = await _run_native(command, cwd, int(timeout_s))

    if result.get("error"):
        return {"success": False, "output": "",
                "error": f"Error: {result['error']}\n{cwd_hint}"}

    if result["timed_out"]:
        return {"success": False, "output": "",
                "error": "Error: Command timed out after "
                         f"{int(timeout_s)} seconds\n{cwd_hint}"}

    output = _truncate_output(result["output"])
    exit_code = result["exit_code"]

    if exit_code == 0:
        body = output if output.strip() else "(no output)"
        return {"success": True,
                "output": f"{body}\n\n{cwd_hint}\nExit code: 0",
                "error": None}

    body = output if output.strip() else "(no output)"
    # P2-1 fix: 失败时把 stdout/stderr 各自的尾部 4KB 放进 error 字段。
    # 真正的报错（编译错误、堆栈、缺失依赖）几乎总在输出末尾，此前只回首行
    # 导致 agent 看不到原因而盲目重试。_error_tail 同时剥离 ANSI 转义序列。
    stdout_tail = _error_tail(result.get("stdout", ""))
    stderr_tail = _error_tail(result.get("stderr", ""))
    detail_parts: list[str] = []
    if stdout_tail:
        detail_parts.append(f"[stdout tail]\n{stdout_tail}")
    if stderr_tail:
        detail_parts.append(f"[stderr tail]\n{stderr_tail}")
    detail = "\n".join(detail_parts)
    error_msg = f"Command exited with code {exit_code}"
    if detail:
        error_msg = f"{error_msg}\n{detail}"
    return {
        "success": False,  # non-zero exit is not success
        "output": f"{body}\n\n{cwd_hint}\nExit code: {exit_code}",
        "error": error_msg,
    }


async def execute_run_command(
    command: str,
    cwd: str,
    timeout_ms: int,
    workspace_path: str,
) -> dict[str, Any]:
    """Lower-level escape hatch with self-destructive guard (A3 fix).

    Contract 02: run_command is the bash escape hatch included in core_tools.
    Previously skipped self-destructive check — now unified with execute_bash
    to prevent rm -rf /, format, shutdown etc. across all command execution.
    """
    if not command or not command.strip():
        return {"success": False, "output": "",
                "error": "Error: command is required"}

    # 统一命令安全校验 — 自毁命令 + 敏感路径 + .hiveweave 系统目录（A3 + 旁路修复）
    blocked, reason = _validate_command_safety(command)
    if blocked:
        log.warning("run_command.blocked", reason=reason,
                    command_preview=command[:120])
        return {"success": False, "output": "",
                "error": f"Error: {reason}"}

    ws = workspace_path or os.getcwd()
    if cwd:
        full_cwd = str(Path(ws) / cwd)
    else:
        full_cwd = ws

    if not _is_within_workspace(full_cwd, ws):
        return {"success": False, "output": "",
                "error": "Error: Sandbox violation - cwd must be within workspace"}

    if not Path(full_cwd).exists():
        return {"success": False, "output": "",
                "error": f"Error: Working directory does not exist: {full_cwd}"}

    safe_timeout = int(timeout_ms or 120_000)
    if 1 <= safe_timeout <= 600:
        safe_timeout = safe_timeout * 1000
    safe_timeout = max(5000, min(safe_timeout, MAX_TIMEOUT_S * 1000))
    timeout_s = safe_timeout // 1000

    log.info("run_command.execute", cwd=full_cwd, timeout_s=timeout_s,
             command_preview=command[:120])

    result = await _run_native(command, full_cwd, timeout_s)

    if result.get("error"):
        return {"success": False, "output": "",
                "error": f"Error: {result['error']}"}

    if result["timed_out"]:
        return {"success": False, "output": "",
                "error": f"Error: Command timed out after {timeout_s} seconds"}

    output = _truncate_output(result["output"])
    exit_code = result["exit_code"]

    if exit_code == 0:
        body = output if output.strip() else "(no output)"
        return {"success": True, "output": f"{body}\n\nExit code: 0",
                "error": None}

    body = output if output.strip() else "(no output)"
    # P2-1 fix: 同 execute_bash — 失败时返回 stdout/stderr 各自尾部 4KB。
    stdout_tail = _error_tail(result.get("stdout", ""))
    stderr_tail = _error_tail(result.get("stderr", ""))
    detail_parts: list[str] = []
    if stdout_tail:
        detail_parts.append(f"[stdout tail]\n{stdout_tail}")
    if stderr_tail:
        detail_parts.append(f"[stderr tail]\n{stderr_tail}")
    detail = "\n".join(detail_parts)
    error_msg = f"Command exited with code {exit_code}"
    if detail:
        error_msg = f"{error_msg}\n{detail}"
    return {
        "success": False,
        "output": f"{body}\n\nExit code: {exit_code}",
        "error": error_msg,
    }


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


class BashParams(BaseModel):
    """Parameters for bash tool."""
    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(
        description="Shell command to execute.",
        json_schema_extra={"aliases": ["cmd", "run"]},
    )
    timeout: int = Field(
        default=120000,
        ge=5000,
        le=600000,
        description="Timeout in milliseconds. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds (e.g. 30 = 30s). Use 120000 for npm install.",
        json_schema_extra={"aliases": ["timeout_ms", "timeoutMs"]},
    )


class RunCommandParams(BaseModel):
    """Parameters for run_command tool."""
    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(
        description="Command to execute.",
        json_schema_extra={"aliases": ["cmd", "run"]},
    )
    cwd: str = Field(
        default="",
        description="Working directory (relative to workspace). Default: workspace root.",
    )
    timeout: int = Field(
        default=120000,
        ge=5000,
        le=600000,
        description="Timeout in milliseconds. Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 are treated as seconds.",
        json_schema_extra={"aliases": ["timeout_ms", "timeoutMs"]},
    )


@tool(
    "bash",
    "Executes a shell command on the local system. Use it to run CLI tools, scripts, git commands, or any system operation. Returns stdout and stderr of the command.",
    requires_workspace=True,
    security_level="shell",
)
async def bash_tool(params: BashParams, agent_id: str, workspace: str) -> ToolResult:
    """Execute a bash command."""
    from hiveweave.services.process_registry import prepare_spawn_command
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    cmd, _env, reserved_err = prepare_spawn_command(
        params.command, project_id=project_id
    )
    if reserved_err:
        return ToolResult.err(reserved_err)
    result = await execute_bash(
        command=cmd,
        workdir="",
        workspace_path=workspace,
        timeout_ms=params.timeout,
        project_id=project_id,
    )
    # D4: track consecutive failures per (agent_id, cwd)
    _streak_hint = _update_cwd_failure_streak(
        agent_id, workspace, bool(result.get("success"))
    )
    if result.get("success"):
        out = result["output"]
        # Phase 3: attest test runs
        try:
            from hiveweave.services.attestation import (
                attestation_service,
                is_test_command,
            )
            from hiveweave.services.task import TaskService

            if project_id and is_test_command(params.command or ""):
                tasks = await TaskService().list_tasks(
                    project_id, assignee_id=agent_id
                )
                active = [
                    t for t in tasks if t.get("status") in ("running", "claimed")
                ]
                task_id = active[0].get("id") if len(active) == 1 else None
                aid = await attestation_service.create(
                    project_id,
                    agent_id=agent_id,
                    kind="test_run",
                    command_or_url=(params.command or "")[:500],
                    exit_code=0,
                    workspace=workspace or "",
                    stdout=str(out)[-8000:],
                    task_id=task_id,
                )
                out = f"{out}\n\n[attestation_id={aid} kind=test_run]"
                if task_id:
                    try:
                        await TaskService().emit_task_event(
                            project_id,
                            task_id,
                            "test_attestation",
                            agent_id=agent_id,
                            summary=(
                                f"[test_attestation] task {task_id[:8]} "
                                f"via shell"
                            ),
                        )
                    except Exception:
                        pass
        except Exception as _att_err:
            # 此前静默 pass，attestation 签发失败无从排查（submit 被拒时
            # agent 完全不知道原因）。工具主流程不受影响，仅记录。
            log.warning("bash_attest_issue_failed", error=str(_att_err))
        return ToolResult.ok(out)
    # For bash, output contains the command output even on failure
    err_msg = result.get("error", "Command failed")
    if _streak_hint:
        err_msg = f"{err_msg}{_streak_hint}"
    return ToolResult.err(err_msg)


@tool(
    "run_command",
    "Executes a command and returns the output. Similar to bash but with explicit working directory support. Use for running scripts, builds, tests, or any system command.",
    requires_workspace=True,
    security_level="shell",
)
async def run_command_tool(params: RunCommandParams, agent_id: str, workspace: str) -> ToolResult:
    """Execute a command with explicit cwd."""
    from hiveweave.services.process_registry import prepare_spawn_command
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    cmd, _env, reserved_err = prepare_spawn_command(
        params.command, project_id=project_id
    )
    if reserved_err:
        return ToolResult.err(reserved_err)
    result = await execute_run_command(
        command=cmd,
        cwd=params.cwd,
        timeout_ms=params.timeout,
        workspace_path=workspace,
    )
    # D4: track consecutive failures per (agent_id, cwd)
    _effective_cwd = str(Path(workspace) / params.cwd) if params.cwd else workspace
    _streak_hint = _update_cwd_failure_streak(
        agent_id, _effective_cwd, bool(result.get("success"))
    )
    if result.get("success"):
        out = result["output"]
        try:
            from hiveweave.services.attestation import (
                attestation_service,
                is_test_command,
            )
            from hiveweave.services.task import TaskService

            if project_id and is_test_command(params.command or ""):
                tasks = await TaskService().list_tasks(
                    project_id, assignee_id=agent_id
                )
                active = [
                    t for t in tasks if t.get("status") in ("running", "claimed")
                ]
                task_id = active[0].get("id") if len(active) == 1 else None
                aid = await attestation_service.create(
                    project_id,
                    agent_id=agent_id,
                    kind="test_run",
                    command_or_url=(params.command or "")[:500],
                    exit_code=0,
                    workspace=workspace or "",
                    stdout=str(out)[-8000:],
                    task_id=task_id,
                )
                out = f"{out}\n\n[attestation_id={aid} kind=test_run]"
                if task_id:
                    try:
                        await TaskService().emit_task_event(
                            project_id,
                            task_id,
                            "test_attestation",
                            agent_id=agent_id,
                            summary=(
                                f"[test_attestation] task {task_id[:8]} "
                                f"via shell"
                            ),
                        )
                    except Exception:
                        pass
        except Exception as _att_err:
            # 此前静默 pass，attestation 签发失败无从排查（submit 被拒时
            # agent 完全不知道原因）。工具主流程不受影响，仅记录。
            log.warning("bash_attest_issue_failed", error=str(_att_err))
        return ToolResult.ok(out)
    err_msg = result.get("error", "Command failed")
    if _streak_hint:
        err_msg = f"{err_msg}{_streak_hint}"
    return ToolResult.err(err_msg)
