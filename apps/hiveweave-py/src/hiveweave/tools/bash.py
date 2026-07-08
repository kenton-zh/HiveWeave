"""Bash tool — shell command execution with sandbox + self-destruct guard.

契约 02: 工具执行器 — bash 子模块
- 执行 shell 命令（Windows: cmd /c，POSIX: bash -c）
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
DOCKER_SANDBOX_IMAGE = "hiveweave/sandbox:latest"

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
    """
    safe_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
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


def _normalize_command(command: str) -> str:
    """Pre-process command for cross-platform compatibility.

    - python3 → python (Windows: python3.exe doesn't exist; Unix: alias if absent)
    - pip3 → pip
    """
    import re
    # Replace python3/pip3 with python/pip (word-boundary safe)
    cmd = re.sub(r'\bpython3\b', 'python', command)
    cmd = re.sub(r'\bpip3\b', 'pip', command)
    return cmd


async def _run_native(command: str, cwd: str, timeout_s: int) -> dict[str, Any]:
    """Execute command via the OS native shell (cmd / bash)."""
    command = _normalize_command(command)
    is_windows = sys.platform.startswith("win")
    if is_windows:
        shell_args = ["cmd", "/c", command]
    else:
        shell_args = ["bash", "-c", command]

    env = _build_safe_env(cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell_args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return {"output": "", "exit_code": None, "timed_out": False,
                "error": f"Failed to spawn shell: {exc}"}
    except OSError as exc:
        return {"output": "", "exit_code": None, "timed_out": False,
                "error": f"Failed to spawn shell: {exc}"}

    try:
        stdout_bytes, _ = await asyncio.wait_for(
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
        return {"output": "", "exit_code": None, "timed_out": True, "error": None}

    output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    return {
        "output": output,
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
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("bash.docker_unavailable", reason="docker binary not found")
        return await _run_native(command, cwd, timeout_s)

    try:
        stdout_bytes, _ = await asyncio.wait_for(
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
        return {"output": "", "exit_code": None, "timed_out": True, "error": None}

    output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    return {
        "output": output,
        "exit_code": proc.returncode,
        "timed_out": False,
        "error": None,
    }


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

    # 1. Self-destructive guard
    blocked, reason = check_self_destructive(command)
    if blocked:
        log.warning("bash.blocked", reason=reason, command_preview=command[:120])
        return {"success": False, "output": "",
                "error": f"Error: Command blocked: {reason}"}

    # BUG-011 修复：敏感文件路径检查 — 阻止 `cat .env` / `cat ~/.ssh/id_rsa` 等
    # 绕过 file.py 敏感检查的 bash 命令。用 security.is_sensitive_path 做子串匹配。
    from hiveweave.tools.security import is_sensitive_path
    if is_sensitive_path(command):
        log.warning("bash.blocked_sensitive", command_preview=command[:120])
        return {"success": False, "output": "",
                "error": "Error: Command blocked: command references a "
                         "sensitive file (e.g. .env, *.pem, id_rsa, "
                         "credentials). Use read_file with explicit "
                         "approval instead."}

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

    # 3. Clamp timeout
    if timeout_ms is None:
        timeout_ms = DEFAULT_TIMEOUT_S * 1000
    timeout_ms = max(1000, min(int(timeout_ms), MAX_TIMEOUT_S * 1000))
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
                "error": f"Error: {result['error']}"}

    if result["timed_out"]:
        return {"success": False, "output": "",
                "error": "Error: Command timed out after "
                         f"{int(timeout_s)} seconds"}

    output = _truncate_output(result["output"])
    exit_code = result["exit_code"]

    if exit_code == 0:
        body = output if output.strip() else "(no output)"
        return {"success": True, "output": f"{body}\n\nExit code: 0",
                "error": None}

    body = output if output.strip() else "(no output)"
    return {
        "success": False,  # non-zero exit is not success
        "output": f"{body}\n\nExit code: {exit_code}",
        "error": f"Command exited with code {exit_code}",
    }


async def execute_run_command(
    command: str,
    cwd: str,
    timeout_ms: int,
    workspace_path: str,
) -> dict[str, Any]:
    """Lower-level escape hatch — NO self-destructive guard.

    Contract 02: run_command is the bash escape hatch included in core_tools.
    It is available to all roles but does NOT run the self-destructive check.
    """
    if not command or not command.strip():
        return {"success": False, "output": "",
                "error": "Error: command is required"}

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

    safe_timeout = max(1000, min(int(timeout_ms or 120_000), MAX_TIMEOUT_S * 1000))
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
    return {
        "success": False,
        "output": f"{body}\n\nExit code: {exit_code}",
        "error": f"Command exited with code {exit_code}",
    }
