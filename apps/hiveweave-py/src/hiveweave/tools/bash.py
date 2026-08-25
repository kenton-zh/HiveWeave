"""Bash tool — shell command execution with sandbox + self-destruct guard.

契约 02: 工具执行器 — bash 子模块
- 执行 shell 命令（Windows: 优先 Git Bash bash -c，无 Git Bash 时降级 cmd /s /c）
- POSIX: bash -c
- 120s 默认超时（max 600s），超时强制终止
- 路径沙箱：workdir 必须在 workspace_path 内
- 自毁命令拦截：7 个正则模式（rm -rf /, format, diskpart, shutdown, reboot, poweroff, halt）
- 输出截断：> 1MB 截断并追加标记（轻量截断，不存盘）
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

from hiveweave.util.tree_label import cwd_display

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

# P0-3 增量2 (audit 2026-07-28): long-running dev-server commands run forever
# and lock node_modules. When spawned via bash they were never registered, so
# stop_processes_for_worktree couldn't kill them → WinError 32 on worktree
# teardown. Detect such commands and route them to the registered spawn path
# (same mechanism start_dev_server uses) so the process is trackable/killable.
_DEV_SERVER_TRIGGER_RE = re.compile(
    r"(?:"
    r"(?:^|\s|;|&|\|)`?(?:"
    r"(?:npx\s+)?vite(?:\s|$)"               # vite / npx vite (bare = dev server)
    r"|(?:pythonw?|python3(?:\.\d+)?|py)(?:\.exe)?\s+-m\s+http\.server(?:\s|$)"
    r"|npx\s+(?:-y\s+)?(?:http-server|live-server|serve)(?:\s|$)"
    r"|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:dev|start|serve)(?:\s|$)"
    r"|bun\s+(?:run\s+)?(?:dev|start)(?:\s|$)"
    r"|next\s+dev(?:\s|$)"
    r"|nuxt\s+dev(?:\s|$)"
    r"|nodemon\b"
    r"|(?:pythonw?|python3)(?:\.exe)?\s+-m\s+uvicorn(?:\s|$)"
    r"|(?:pythonw?|python3)(?:\.exe)?\s+-m\s+app\.server(?:\s|$)"
    r"|(?:pythonw?|python3)(?:\.exe)?(?:\s+-[^\s]+)*\s+"
    r"(?:['\"]?)(?:\.[/\\])?app[/\\]server\.py(?:\s|$)"
    r"|(?:pythonw?|python3)(?:\.exe)?\s+-m\s+flask\b"
    r"(?:\s+(?:--[\w-]+(?:[=\s][^\s;|&]+)?|-[A-Za-z](?:\s+[^\s;|&]+)?))*\s+run\b"
    r"|uv\s+run\b(?:\s+\S+)*?\s+flask\b"
    r"(?:\s+(?:--[\w-]+(?:[=\s][^\s;|&]+)?|-[A-Za-z](?:\s+[^\s;|&]+)?))*\s+run\b"
    r"|(?:pythonw?|python3)(?:\.exe)?\s+-m\s+gunicorn(?:\s|$)"
    r"|uv\s+run\b(?:\s+\S+)*?\s+(?<!\s--with\s)(?<!\s--extra\s)(?<!\s--group\s)(?<!\s--package\s)gunicorn(?:\s+\S|$)"
    r"|uv\s+run\b(?:\s+\S+)*?\s+(?<!\s--with\s)(?<!\s--extra\s)(?<!\s--group\s)(?<!\s--package\s)uvicorn(?:\s+\S|$)"
    r")"
    # 裸 uvicorn / gunicorn：仅段首（含 VAR=val 前缀），避免 --with uvicorn
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?uvicorn(?:\s+\S)"
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?gunicorn(?:\s+\S)"
    r"|(?:^|&&|\|\||;|\||&)\s*(?:[A-Za-z_][\w]*=\S+\s+)*`?flask\b"
    r"(?:\s+(?:--[\w-]+(?:[=\s][^\s;|&]+)?|-[A-Za-z](?:\s+[^\s;|&]+)?))*\s+run\b"
    r")",
    re.IGNORECASE,
)
# Blocking verbs that produce finite output — NOT dev servers (vite build,
# npm run build, npm test, etc.). Their presence disqualifies auto-routing.
_BLOCKING_VERB_RE = re.compile(
    r"\b(?:build|test|lint|install|ci|audit|eject|deploy)\b",
    re.IGNORECASE,
)
# 尾部后台符：注册 spawn / offturn job 已脱离前台，字面 & 会在 shell 里 orphan。
_TRAILING_AMP_RE = re.compile(r"\s*&+\s*$")
_UVICORN_HELP_RE = re.compile(
    r"(?:^|\s)(?:--help|-h|--version)(?:\s|$)",
    re.IGNORECASE,
)
_UVICORN_TOKEN_RE = re.compile(r"\buvicorn\b", re.IGNORECASE)
_APP_SERVER_TOKEN_RE = re.compile(
    r"(?:app\.server\b|app[/\\]server\.py\b)",
    re.IGNORECASE,
)
_FLASK_TOKEN_RE = re.compile(r"\bflask\b", re.IGNORECASE)
_GUNICORN_TOKEN_RE = re.compile(r"\bgunicorn\b", re.IGNORECASE)
_STATIC_SERVER_TOKEN_RE = re.compile(
    r"\b(?:http\.server|http-server|live-server|serve)\b",
    re.IGNORECASE,
)


def _strip_trailing_ampersand(command: str) -> str:
    return _TRAILING_AMP_RE.sub("", (command or "").strip()).strip()


def _has_trailing_ampersand(command: str) -> bool:
    return bool(_TRAILING_AMP_RE.search((command or "").strip()))


def _should_offturn_trailing_amp(command: str) -> bool:
    """前台 `cmd &` 且不是已识别的长驻服务 → 走 offturn job，禁止 shell 脱管。"""
    if not _has_trailing_ampersand(command):
        return False
    return _detect_dev_server_command(command) is None


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
    cmd = _strip_trailing_ampersand(command)
    if not cmd:
        return None
    if not _DEV_SERVER_TRIGGER_RE.search(cmd):
        return None
    from hiveweave.services.process_registry import uv_dep_consumed_token

    if (
        uv_dep_consumed_token(cmd, "gunicorn")
        or uv_dep_consumed_token(cmd, "uvicorn")
        or uv_dep_consumed_token(cmd, "flask")
    ):
        return None
    # Disqualify blocking verbs (vite build, npm run build:test, …).
    if _BLOCKING_VERB_RE.search(cmd):
        return None
    # uvicorn / flask / gunicorn / app.server / 静态服务器 --help 会立刻
    # 退出，不当成长驻服务。
    if (
        _UVICORN_TOKEN_RE.search(cmd)
        or _APP_SERVER_TOKEN_RE.search(cmd)
        or _FLASK_TOKEN_RE.search(cmd)
        or _GUNICORN_TOKEN_RE.search(cmd)
        or _STATIC_SERVER_TOKEN_RE.search(cmd)
    ) and _UVICORN_HELP_RE.search(cmd):
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
    agent_id: str | None = None,
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
        extract_ports_from_command,
        is_pid_alive,
        is_reserved_port,
        lookup_by_port,
        pick_observed_listen_port,
        prepare_spawn_command,
        prune_dead_processes,
        register,
        spawn_project_process,
        stop_process_by_port,
        terminate_spawned,
        listening_ports_for_pid,
    )
    from hiveweave.services.acl_sandbox.integration import acl_sandbox_active
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

    command = _strip_trailing_ampersand(command)
    pid = project_id or "default"
    # 阻塞调用（netstat 快照 / taskkill）统一下放线程池，避免卡住事件循环
    await asyncio.to_thread(prune_dead_processes)
    preferred = (
        port_hint
        if (port_hint and not is_reserved_port(port_hint))
        else 3000
    )
    if is_reserved_port(preferred):
        return {
            "success": False, "output": "",
            "error": (
                f"Refusing to start dev server on reserved platform port "
                f"{preferred}. Use start_dev_server or a project port (3000+)."
            ),
            "blocked": True,
        }

    own_live_pref = [
        r for r in await asyncio.to_thread(lookup_by_port, preferred)
        if r.project_id == pid and is_pid_alive(r.pid)
    ]
    if own_live_pref:
        await asyncio.to_thread(stop_process_by_port, pid, preferred)

    if port_hint and not is_reserved_port(port_hint):
        port = int(port_hint)
    else:
        port = await asyncio.to_thread(allocate_project_port, pid, preferred)
    other_on_port = [
        r for r in await asyncio.to_thread(lookup_by_port, port)
        if r.project_id != pid and is_pid_alive(r.pid)
    ]
    if other_on_port:
        port = await asyncio.to_thread(allocate_project_port, pid, port + 1)
    await asyncio.to_thread(prune_dead_processes)
    own_on_port = [
        r for r in await asyncio.to_thread(lookup_by_port, port)
        if r.project_id == pid and is_pid_alive(r.pid)
    ]
    if own_on_port:
        await asyncio.to_thread(stop_process_by_port, pid, port)

    if is_reserved_port(port):
        return {
            "success": False, "output": "",
            "error": (
                f"Refusing to start dev server on reserved platform port "
                f"{port}. Use start_dev_server or a project port (3000+)."
            ),
            "blocked": True,
        }

    try:
        if acl_sandbox_active():
            # P1 §5.7：dev server 收编 —— 受限长驻 spawn，注册 process_registry。
            # E10：传 argv（逐元素引用，修剥引号根因）。
            from hiveweave.services.acl_sandbox.integration import (
                build_confined_argv,
                resolve_project_root,
            )
            from hiveweave.services.acl_sandbox.service import spawn_confined

            cmd2, extra_env, prep_err = prepare_spawn_command(
                command, project_id=project_id, preferred_port=port
            )
            if prep_err:
                return {
                    "success": False, "output": "",
                    "error": prep_err, "blocked": True,
                }
            project_root = await resolve_project_root(project_id)
            sres = await spawn_confined(
                argv=build_confined_argv(cmd2),
                workdir=cwd,
                workspace_path=cwd,
                agent_id=agent_id or "unknown",
                project_id=project_id,
                project_workspace_path=project_root,
                entry="dev_server",
                long_running=True,
                env_extra=extra_env,
            )
            if sres is not None:
                proc = _ConfinedDevProc(sres["job"])
                spawn_err = None
                meta = {
                    "command": cmd2,
                    "cwd": cwd,
                    "pid": proc.pid,
                    "env_port": extra_env.get("PORT") or extra_env.get("VITE_PORT"),
                }
            else:
                proc, spawn_err, meta = spawn_project_process(
                    command, cwd=cwd, project_id=project_id, preferred_port=port
                )
        else:
            proc, spawn_err, meta = spawn_project_process(
                command,
                cwd=cwd,
                project_id=project_id,
                preferred_port=port,
            )
    except SandboxUnavailableError as e:
        # fail-closed：沙箱不可用 → 直接干净拒绝，不重复 spawn / 不落原生。
        log.warning(
            "bash.dev_server_sandbox_unavailable",
            error=str(e), command=command[:120], cwd=cwd[:120],
        )
        return e.to_tool_dict()
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

        r = await asyncio.to_thread(
            _sp.run,
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if r.returncode == 0:
            commit = (r.stdout or "").strip()
    except Exception:
        pass

    registered_port = port
    env_port = meta.get("env_port")
    if env_port:
        try:
            ep = int(env_port)
            if not is_reserved_port(ep):
                registered_port = ep
        except (TypeError, ValueError):
            pass

    observed = None
    for _ in range(12):
        observed = await asyncio.to_thread(
            pick_observed_listen_port, proc.pid, registered_port
        )
        if observed:
            break
        await asyncio.sleep(0.25)
    probed = bool(observed)
    if observed:
        registered_port = observed
    else:
        raw_listen = await asyncio.to_thread(listening_ports_for_pid, proc.pid)
        if raw_listen and all(is_reserved_port(p) for p in raw_listen):
            await asyncio.to_thread(terminate_spawned, proc)
            return {
                "success": False, "output": "",
                "error": (
                    f"Refusing reserved LISTEN port(s) {raw_listen} "
                    f"(pid={proc.pid}). Use a project port (3000+)."
                ),
                "blocked": True,
            }

    if is_reserved_port(registered_port):
        await asyncio.to_thread(terminate_spawned, proc)
        return {
            "success": False, "output": "",
            "error": (
                f"Refusing to register reserved platform port "
                f"{registered_port}."
            ),
            "blocked": True,
        }

    try:
        await asyncio.to_thread(register, ProcessRecord(
            project_id=project_id or "",
            port=registered_port,
            pid=proc.pid,
            cwd=cwd,
            command=meta.get("command") or command,
            commit=commit,
        ))
    except Exception as e:
        log.warning(
            "bash.dev_server_register_failed",
            error=str(e), pid=proc.pid, port=registered_port, cwd=cwd[:120],
        )
        await asyncio.to_thread(terminate_spawned, proc)
        return {
            "success": False, "output": "",
            "error": f"Failed to register dev server: {e}",
            "blocked": True,
        }

    port_note = ""
    if not probed and (
        _APP_SERVER_TOKEN_RE.search(command)
        and not extract_ports_from_command(command)
    ):
        port_note = (
            f"  NOTE: no LISTEN port observed yet for pid={proc.pid}; "
            f"registry uses PORT={registered_port}. If lookup misses, "
            f"call lookup_dev_server after the app binds, or pass "
            f"--port on 3000+.\n"
        )

    log.info(
        "bash.dev_server_auto_registered",
        pid=proc.pid, port=registered_port, cwd=cwd[:120],
        probed=probed,
        command=(meta.get("command") or command)[:120],
    )
    return {
        "success": True,
        "output": (
            f"[hiveweave] Dev server auto-registered from bash.\n"
            f"  pid={proc.pid} port={registered_port} {_cwd_style_hint(cwd)}\n"
            f"  command: {meta.get('command') or command}\n"
            f"  URL: http://localhost:{registered_port}/\n"
            f"{port_note}"
            f"  This process is tracked — stop_dev_server / "
            f"lookup_dev_server to stop or inspect; "
            f"stop_processes_for_worktree kills it on teardown.\n"
            f"  (Routed from bash because the command is a long-running dev "
            f"server; blocking on it would time out and orphan the process.)\n"
            f"\nExit code: 0"
        ),
        "error": None,
    }

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
    """Bash 子进程环境：白名单 + HIVEWEAVE_BASH 标记。"""
    from hiveweave.util.safe_env import build_child_env

    return build_child_env(cwd, bash_markers=True)


# ── P1: ACL 沙箱接线（spec §5.7） ─────────────────────────────
def _maybe_append_venv_hint(workspace: str | None, error_msg: str) -> str:
    """E9: python 依赖缺失失败时提示项目 .venv 解释器（官方依赖路径）。

    命中 ``ModuleNotFoundError`` / ``No module named`` 且项目已初始化 .venv
    时追加一行指引；未命中/无 venv → 原样返回（fail-open，零扰动）。
    """
    low = error_msg.lower()
    if "module not found error" not in low and "no module named" not in low:
        return error_msg
    try:
        from hiveweave.services.venv_setup import project_venv_python

        venv_py = project_venv_python(workspace)
        if not venv_py:
            return error_msg
        return error_msg + (
            f"\n\n[venv hint] 项目已提供虚拟环境，缺的依赖请装进 .venv "
            f"(uv pip install --python \"{venv_py}\" <包>)，再用 "
            f"\"{venv_py}\" 运行以生效（勿用 --target 装进源码树）。"
        )
    except Exception:
        return error_msg


def _native_shaped(result: dict) -> dict[str, Any]:
    """把 spawn_confined 的 {exit_code,stdout,stderr,timed_out} 归一为 native 形态。"""
    stdout = result.get("stdout", "") or ""
    stderr = result.get("stderr", "") or ""
    combined = stdout + ("\n" + stderr if stdout and stderr else stderr)
    return {
        "output": combined,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out", False)),
        "error": None,
    }


async def _run_sandboxed(
    command: str,
    cwd: str,
    timeout_s: float | None,
    *,
    workspace_path: str,
    agent_id: str | None,
    project_id: str | None,
    entry: str,
    long_running: bool = False,
    env_extra: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """沙箱 on 时受限执行；返回 None = 沙箱未启用（调用方回退 native）。"""
    from hiveweave.services.acl_sandbox.integration import (
        acl_sandbox_active,
        build_confined_argv,
        resolve_project_root,
    )
    from hiveweave.services.acl_sandbox.service import spawn_confined

    if not acl_sandbox_active():
        return None
    project_root = await resolve_project_root(project_id)
    result = await spawn_confined(
        argv=build_confined_argv(command),
        workdir=cwd,
        workspace_path=workspace_path,
        agent_id=agent_id or "unknown",
        project_id=project_id,
        project_workspace_path=project_root,
        timeout_s=timeout_s or 0,
        entry=entry,
        long_running=long_running,
        env_extra=env_extra,
    )
    if result is None or result.get("long_running"):
        return result
    return _native_shaped(result)


class _ConfinedDevProc:
    """Popen-like shim for a sandboxed long-running dev server（pid + terminate）。"""

    def __init__(self, job):
        self.job = job
        self.pid = job.pid

    def terminate(self) -> None:
        try:
            self.job.terminate()
        except Exception:  # noqa: BLE001
            pass

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

# Test-runner exclude flags that only *mention* .hiveweave so pytest/vitest/jest
# skip worktrees. Stripped before the .hiveweave + file-op guard so injected
# `--ignore=.hiveweave` does not self-block (`import` in `python -c` is a
# file-op token). Real ops like `cat .hiveweave/data.db` still match after.
_HIVEWEAVE_TEST_EXCLUDE_RE = re.compile(
    r"(?:^|\s)(?:"
    r"--ignore(?:-glob)?(?:\s+|=)\s*['\"]?(?:\*\*/?)?\.hiveweave(?:/\*\*)?['\"]?(?=['\"\s]|$)"
    r"|--exclude(?:\s+|=)\s*['\"]?(?:\*\*/?)?\.hiveweave(?:/\*\*)?['\"]?(?=['\"\s]|$)"
    r"|--testPathIgnorePatterns(?:\s+|=)\s*['\"]?\\?\.hiveweave['\"]?(?=['\"\s]|$)"
    r")",
    re.IGNORECASE,
)


def _strip_hiveweave_test_excludes(command: str) -> str:
    """Remove test-runner flags whose only .hiveweave mention is an exclude."""
    stripped = _HIVEWEAVE_TEST_EXCLUDE_RE.sub(" ", command or "")
    return re.sub(r"[ \t]{2,}", " ", stripped).strip()

# 放行的 .hiveweave 子目录 — agent 可在这些子目录内执行文件操作
# 与 file.py 的 allowed_subdirs（_check_hiveweave_dir）保持一致:
# shared=团队共享 / reports, drafts, worktrees=工作文件 / handoffs=交接文档
# (?![\w.-]) 精确拒绝「路径名续字符」：放行 `git -C .hiveweave/worktrees`（尾随空格/结尾），
# 拦 `.hiveweave/shared-evil/`、`.hiveweave/worktrees2/` 这类前缀目录（\b 会被 d- / s2 击穿）
_ALLOWED_HW_SUBDIRS = re.compile(
    r"\.hiveweave[\\/]+(?:shared|reports|drafts|worktrees|handoffs)(?![\w.-])",
    re.IGNORECASE,
)


def _check_hiveweave_command(command: str) -> bool:
    """Return True if the command targets `.hiveweave` with a file operation.

    拦截 agent 通过 bash 读写/删除/复制 .hiveweave 内系统文件（data.db 等）。
    `cd .hiveweave` 和 `ls .hiveweave` 这类无害命令不拦。
    放行指向 shared/reports/drafts/worktrees/handoffs 子目录的文件操作（团队共享/工作文件）。
    """
    command = _strip_hiveweave_test_excludes(command)
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
    # 目标型护栏（敏感文件 / .hiveweave）先于命令模式护栏：同一命令多重命中时
    # 报更具体、更可行动的原因（如 rm -rf .hiveweave 报系统目录而非 rm-rf 提示）。
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
    # slack-clone_01 P0: 命令模式护栏（taskkill //IM / rm -rf / pkill …）
    # + 受保护 PID 硬层。ask 无在线审批 → 降级 deny + 疏通提示。
    from hiveweave.services.command_guard import evaluate_command

    verdict = evaluate_command(command)
    if verdict.blocked:
        return True, f"Command blocked: {verdict.reason}"
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


def _map_unix_to_pwsh(cmd: str) -> str:
    """bash 惯用法命令 → pwsh 等价（受限 shell 方言适配，spec §18.3）。

    与 ``_map_unix_to_cmd``（cmd 兜底路径）不同，这里处理**带 unix flag** 的
    常见命令（pwsh 别名无 POSIX flag 语义，`ls -la` 会报参数错误）——只匹配
    明确模式，不误伤 pwsh 本就兼容的调用。
    """
    import re

    # ls -la / ls -l / ls -a → Get-ChildItem -Force（近似列出全部）
    cmd = re.sub(r'\bls\s+(-la|-l|--long|-al)\b', 'Get-ChildItem -Force', cmd)
    cmd = re.sub(r'\bls\s+-a\b', 'Get-ChildItem -Force', cmd)
    # head -N file → Get-Content file -TotalCount N
    cmd = re.sub(
        r'\bhead\s+-(\d+)\s+([^\s|;&]+)',
        r"Get-Content \2 -TotalCount \1", cmd)
    # tail -N file → Get-Content file -Tail N
    cmd = re.sub(
        r'\btail\s+-(\d+)\s+([^\s|;&]+)',
        r"Get-Content \2 -Tail \1", cmd)
    # mkdir -p dir → New-Item -ItemType Directory -Force -Path dir
    cmd = re.sub(
        r'\bmkdir\s+-p\s+([^\s|;&]+)',
        r"New-Item -ItemType Directory -Force -Path \1", cmd)
    # grep pattern file → Select-String -Pattern pattern file
    cmd = re.sub(
        r'\bgrep\s+([^\s|;&]+)\s+([^\s|;&]+)',
        r"Select-String -Pattern \1 \2", cmd)
    return cmd


def _normalize_for_pwsh(command: str) -> str:
    """受限 shell 方言适配（spec §18.3）：bash 惯用法 → pwsh 语法。

    受限模式 shell = pwsh 优先（Git Bash 不可用，S1），而 agent 习惯写 bash
    语法。只转换**明确**的 bash 惯用法；pwsh 本就兼容的（git/echo/pwd/重定向
    /``&&``/``||``/``$?``）不动：
    - ``export A=B [C=D]`` → ``$env:A='B'; $env:C='D';``（尾部 ``&&``/``||``/``;``
      一并吞掉 —— 赋值语句后不允许 pipeline-chain 运算符）
    - ``$VAR`` / ``${VAR}`` → ``$env:VAR``（排除 pwsh 保留变量）
    - ``source file`` → ``. file``（pwsh dot-source）
    - 带 unix flag 的常见命令（``ls -la`` / ``head -N`` / ``tail -N`` /
      ``mkdir -p`` / ``grep``）→ pwsh 等价
    - python3/pip3 → python/pip（与 native 一致）
    """
    import re

    # 排除 pwsh 保留/常用变量 —— 这些是 PowerShell 语言量，不能当 env 引用。
    # ``env`` 必须排除：`$env:X` 已限定 env，裸转 `$env:env:X` 会坏。
    _PWSH_RESERVED = {
        "_", "args", "input", "host", "PID", "HOME", "null", "true", "false",
        "LASTEXITCODE", "PSVersionTable", "MyInvocation", "Error", "Matches",
        "env", "PSScriptRoot", "PSCommandPath", "ExecutionContext", "ShellId",
    }

    # export A=B [C=D]（吞掉语句边界 `;`/`&&`/`||` —— 赋值后不能跟 chain 运算符）
    def _export_replacer(m: re.Match) -> str:
        body = m.group(1)
        pairs = re.findall(
            r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|([^\s;|&]+))',
            body)
        return "".join(
            f"$env:{k}='{v1 or v2}'; " for k, v1, v2 in pairs if k
        )

    cmd = re.sub(
        r'\bexport\s+([^;|&\n]+)\s*(?:&&|\|\||;)?\s*',
        _export_replacer, command)
    # ${VAR} → $env:VAR
    cmd = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', r'$env:\1', cmd)
    # 裸 $VAR → $env:VAR（bash 语义 = 环境变量）；排除 pwsh 保留量
    cmd = re.sub(
        r'(?<![$\w])\$([A-Za-z_][A-Za-z0-9_]*)',
        lambda m: m.group(0) if m.group(1) in _PWSH_RESERVED else f'$env:{m.group(1)}',
        cmd)
    # source file → . file
    cmd = re.sub(r'\bsource\s+', '. ', cmd)
    # python3/pip3
    cmd = re.sub(r'\bpython3\b', 'python', cmd)
    cmd = re.sub(r'\bpip3\b', 'pip', cmd)
    # 带 flag 的 unix 命令 → pwsh 等价
    cmd = _map_unix_to_pwsh(cmd)
    return cmd


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


async def _kill_subprocess(proc) -> None:
    """Kill the shell and its children (Windows process tree when possible)."""
    pid = getattr(proc, "pid", None)
    try:
        if pid and sys.platform.startswith("win"):
            from hiveweave.util.win_subprocess import windows_no_window_kwargs

            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **windows_no_window_kwargs(),
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


async def _run_native(command: str, cwd: str, timeout_s: int | None) -> dict[str, Any]:
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
        if timeout_s is None or timeout_s <= 0:
            stdout_bytes, stderr_bytes = await proc.communicate()
        else:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
    except asyncio.TimeoutError:
        await _kill_subprocess(proc)
        return {"output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": True, "error": None}
    except asyncio.CancelledError:
        await _kill_subprocess(proc)
        raise

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


def _cwd_style_hint(cwd: str, relative: str | None = None) -> str:
    """MAIN vs worktree label — relative path only, never dump D:\\ or /d/."""
    return (
        f"{cwd_display(cwd, relative)} "
        f"— relative paths; never invent /workspace"
    )


async def execute_bash(
    command: str,
    workdir: str,
    workspace_path: str,
    timeout_ms: int | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    unbounded: bool = False,
) -> dict[str, Any]:
    """Execute a bash command and return {success, output, error}.

    Performs:
      1. Self-destructive command check (7 patterns)
      2. Sandbox validation (workdir must be within workspace)
      3. Timeout: foreground clamped 5s..600s; unbounded/timeout_ms=0 waits
         until the process exits (background bash / job_kill).
      4. Execute (ACL sandbox > native)
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
                "error": f"Error: {reason}", "blocked": True}

    from hiveweave.services.eval_seal import sealed_bash_deny_for_workspace

    seal_reason = sealed_bash_deny_for_workspace(workspace_path, command)
    if seal_reason:
        log.warning("bash.eval_sealed", command_preview=command[:120])
        return {"success": False, "output": "",
                "error": f"Error: {seal_reason}", "blocked": True}

    # 1.5. Auto-source .hiveweave/env.sh if the project has one.
    # The project declares its own environment setup.
    hw_dir = str(Path(workspace_path) / ".hiveweave")
    # P1：沙箱 on 时受限 shell 是 pwsh/cmd，无法 source bash 语法的 env.sh ——
    # 跳过前缀（否则所有命令被 `source` 掐死），项目环境由 .hiveweave 之外
    # 的机制声明（见 spec §18.3 受限 shell 方言适配）。
    from hiveweave.services.acl_sandbox.integration import acl_sandbox_active

    if not acl_sandbox_active():
        command = _source_env_sh(command, hw_dir)

    # 2. Resolve cwd and validate sandbox
    ws = workspace_path or os.getcwd()
    if workdir:
        cwd = str(Path(ws) / workdir)
    else:
        cwd = ws

    if not _is_within_workspace(cwd, ws):
        return {"success": False, "output": "",
                "error": "Error: Sandbox violation - workdir must be within workspace",
                "blocked": True}

    if not Path(cwd).exists():
        return {"success": False, "output": "",
                "error": f"Error: Working directory does not exist: "
                         f"{cwd_display(cwd, workdir)}",
                "blocked": True}

    cwd_hint = _cwd_style_hint(cwd)

    # P0-3 增量2 (audit 2026-07-28): route long-running dev-server commands to
    # the registered spawn path so stop_processes_for_worktree can kill them.
    # Bash-spawned dev servers were unregistered → WinError 32 on teardown.
    port_hint = _detect_dev_server_command(command)
    if port_hint is not None:
        routed = await _run_registered_dev_server(
            command, cwd, workspace_path, project_id, port_hint,
            agent_id=agent_id,
        )
        if routed is not None:
            return routed
        # None = spawn failed to start; fall through to normal blocking path
        # so the agent sees the real error instead of a silent no-op.

    # 3. Timeout: 0 / unbounded = no local deadline (background start()).
    if unbounded or timeout_ms == 0:
        timeout_s: float | None = 0
    else:
        if timeout_ms is None:
            timeout_ms = DEFAULT_TIMEOUT_S * 1000
        timeout_ms = int(timeout_ms)
        # Heuristic: values 1-600 are likely seconds, not milliseconds
        if 1 <= timeout_ms <= 600:
            timeout_ms = timeout_ms * 1000
        timeout_ms = max(5000, min(timeout_ms, MAX_TIMEOUT_S * 1000))
        timeout_s = timeout_ms / 1000

    # 4. Choose execution backend
    # P1 (spec §5.7): ACL 沙箱 on 时受限执行；None = 未启用 → native。
    result = await _run_sandboxed(
        command, cwd, timeout_s,
        workspace_path=ws, agent_id=agent_id, project_id=project_id,
        entry="bash",
    )
    if result is None:
        result = await _run_native(command, cwd, int(timeout_s or 0))

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
                "error": None,
                "exit_code": 0}

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
    error_msg = _maybe_append_venv_hint(ws, error_msg)
    return {
        "success": False,  # non-zero exit is not success
        "output": f"{body}\n\n{cwd_hint}\nExit code: {exit_code}",
        "error": error_msg,
        "exit_code": exit_code,
    }


async def execute_run_command(
    command: str,
    cwd: str,
    timeout_ms: int,
    workspace_path: str,
    agent_id: str | None = None,
    project_id: str | None = None,
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
                "error": f"Error: {reason}", "blocked": True}

    from hiveweave.services.eval_seal import sealed_bash_deny_for_workspace

    seal_reason = sealed_bash_deny_for_workspace(workspace_path, command)
    if seal_reason:
        log.warning("run_command.eval_sealed", command_preview=command[:120])
        return {"success": False, "output": "",
                "error": f"Error: {seal_reason}", "blocked": True}

    ws = workspace_path or os.getcwd()
    if cwd:
        full_cwd = str(Path(ws) / cwd)
    else:
        full_cwd = ws

    if not _is_within_workspace(full_cwd, ws):
        return {"success": False, "output": "",
                "error": "Error: Sandbox violation - cwd must be within workspace",
                "blocked": True}

    if not Path(full_cwd).exists():
        return {"success": False, "output": "",
                "error": f"Error: Working directory does not exist: "
                         f"{cwd_display(full_cwd, cwd)}",
                "blocked": True}

    safe_timeout = int(timeout_ms or 120_000)
    if 1 <= safe_timeout <= 600:
        safe_timeout = safe_timeout * 1000
    safe_timeout = max(5000, min(safe_timeout, MAX_TIMEOUT_S * 1000))
    timeout_s = safe_timeout // 1000

    log.info("run_command.execute", cwd=full_cwd, timeout_s=timeout_s,
             command_preview=command[:120])

    # P1 (spec §5.7): run_command 收编进沙箱；None = 未启用 → native。
    result = await _run_sandboxed(
        command, full_cwd, timeout_s,
        workspace_path=ws, agent_id=agent_id, project_id=project_id,
        entry="run_command",
    )
    if result is None:
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
                "error": None, "exit_code": 0}

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
    error_msg = _maybe_append_venv_hint(ws, error_msg)
    return {
        "success": False,
        "output": f"{body}\n\nExit code: {exit_code}",
        "error": error_msg,
        "exit_code": exit_code,
    }


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict, field_validator

from .base import tool
from .result import ToolResult


def _coerce_timeout_ms(v: Any) -> Any:
    """LLM often passes seconds (1-600). Scale before ge=5000, matching execute_bash."""
    if v is None or isinstance(v, bool):
        return v
    try:
        n = int(v)
    except (TypeError, ValueError):
        return v
    if 1 <= n <= 600:
        return n * 1000
    return n


class BashParams(BaseModel):
    """Parameters for bash tool."""
    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(
        description=(
            "The bash command to execute. Long-running uvicorn/vite/"
            "http.server: prefer start_dev_server (bash auto-registers "
            "them). Do not `uvicorn … &` in the foreground."
        ),
        json_schema_extra={"aliases": ["cmd", "run"]},
    )
    timeout: int = Field(
        default=120000,
        ge=5000,
        le=600000,
        description=(
            "Foreground only (5s–10min). Ignored when background=true. "
            "Default: 120000 (2 min). Max: 600000 (10 min). Values 1-600 "
            "are treated as seconds (e.g. 30 = 30s). The executor kills "
            "the command on expiry."
        ),
        json_schema_extra={"aliases": ["timeout_ms", "timeoutMs"]},
    )
    background: bool = Field(
        default=False,
        description=(
            "Run off the org turn and return a job id immediately. "
            "No timeout. Then commit_turn(waiting) with waiting_on; "
            "do not poll. Woken with [BASH DONE]/[BASH FAILED]. "
            "Stop with job_kill. Default false keeps stdout in this turn. "
            "Do not use for vite / npm run dev / uvicorn / http.server — "
            "servers never finish (waiting_on would never fire)."
        ),
        json_schema_extra={"aliases": ["bg"]},
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description=(
            "Optional task id to bind test_run attestation "
            "(reviewers: pass the task under review)."
        ),
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )

    @field_validator("timeout", mode="before")
    @classmethod
    def _coerce_timeout(cls, v: Any) -> Any:
        return _coerce_timeout_ms(v)

    @field_validator("background", mode="before")
    @classmethod
    def _coerce_background(cls, v: Any) -> bool:
        if v is None or v is False:
            return False
        if v is True:
            return True
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)


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

    @field_validator("timeout", mode="before")
    @classmethod
    def _coerce_timeout(cls, v: Any) -> Any:
        return _coerce_timeout_ms(v)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description=(
            "Optional task id to bind test_run attestation "
            "(reviewers: pass the task under review)."
        ),
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


# LLM 常把 taskId 写进命令文本而非工具参数（TEST18 第二轮实锤：Vera 写
# `npx vitest run taskId=xxx` 和 `HW_TASK_ID=xxx npx vitest run`）——
# 提取后必须校验 ∈ open VERIFY 候选集，防命令里无关 taskId= 误绑。
_COMMAND_TASK_ID_RE = re.compile(
    r"\b(?:taskId|task_id|TASK_ID|HW_TASK_ID)=([0-9a-fA-F]{8,40})\b"
)


async def _resolve_test_attestation_task_id(
    project_id: str,
    agent_id: str,
    explicit: str | None = None,
    *,
    command: str | None = None,
) -> tuple[str | None, str]:
    """Bind test_run to a task.

    Priority (TEST6 audit S4/S5 + TEST18 P0-3/P0-4):
      1. explicit taskId
      2. reviewer path — sole submitted/reviewing where creator=self
         OR reviewer_id=self
      3. in-flight VERIFY assigned to self when other VERIFYs are queued
         (or in-flight has left created/claimed/running)
      4. VERIFY assignee path — sole open VERIFY (created|claimed|running)
         where assignee=self (VERIFY skips assign=claim, so include created)
      5. assignee path — sole running/claimed where assignee=self
      6. reviewing >1 / VERIFY >1 → refuse silent bind + candidate note
      7. 0 match but REVIEW-capable → candidate tip (do NOT auto-bind)

    Fallback (TEST18 P0-2): when multiple open VERIFY exist, extract
    taskId=/TASK_ID=/HW_TASK_ID= from the command text and bind only if the
    extracted value uniquely matches an open VERIFY id (prefix match
    allowed). This rescues agents who wrote the taskId into the command
    instead of the tool parameter.

    Returns ``(task_id | None, tool_note)``.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip(), ""
    from hiveweave.services.task import TaskService

    ts = TaskService()
    # Reviewer path FIRST (S4): intent to approve is more likely than
    # self-execution when the agent is also an assignee of a parent task.
    try:
        all_tasks = await ts.list_tasks(project_id)
    except Exception:
        all_tasks = []

    def _is_my_review(t: dict) -> bool:
        if t.get("status") not in ("submitted", "reviewing"):
            return False
        if str(t.get("creator_id") or "") == str(agent_id):
            return True
        # TEST18 P0-4: pinned reviewer_id also counts
        if str(t.get("reviewer_id") or "") == str(agent_id):
            return True
        return False

    reviewing = [t for t in (all_tasks or []) if _is_my_review(t)]
    if len(reviewing) == 1:
        return reviewing[0].get("id"), ""
    if len(reviewing) > 1:
        # S5: refuse silent bind — listing candidates is discoverable;
        # binding the "wrong" task creates false evidence that never matches.
        lines = [
            "\n\n[attestation_bind] test_run left UNBOUND: multiple tasks "
            "awaiting your review. Pass taskId explicitly, e.g.:",
        ]
        for t in reviewing[:6]:
            tid = str(t.get("id") or "")
            title = (t.get("title") or "")[:40]
            st = t.get("status") or "?"
            lines.append(f"  - taskId={tid} status={st} title={title!r}")
        lines.append(
            "Re-run the test command with taskId=<id> to bind evidence "
            "for approve."
        )
        return None, "\n".join(lines)

    try:
        mine = await ts.list_tasks(project_id, assignee_id=agent_id)
    except Exception:
        mine = []

    # TEST18 P0-3 re-audit: VERIFY is often still `created` (skips
    # assign=claim). Prefer sole open VERIFY over generic running tasks so
    # stamp can bind without an explicit taskId (cwd is still the tool's).
    _VERIFY_OPEN = frozenset({"created", "claimed", "running"})
    verify_open = [
        t for t in (mine or [])
        if (t.get("status") or "") in _VERIFY_OPEN
        and TaskService._is_verify_task(t)
    ]
    # Queued created VERIFYs must not refuse/steal bind from the one occupying
    # MAIN (s3-clone_01: multiple open → unbound test_run). Prefer in-flight
    # even when it has already moved to submitted/reviewing (stale-baseline
    # re-run while CEO is reviewing).
    try:
        from hiveweave.tools.tasks.verify_spawn import _in_flight_verify_task

        holder = await _in_flight_verify_task(project_id)
    except Exception:
        holder = None
    holder_id = str((holder or {}).get("id") or "")
    holder_is_mine = (
        bool(holder_id)
        and str((holder or {}).get("assignee_id") or "") == str(agent_id)
    )
    if holder_is_mine:
        competing = [
            t for t in verify_open if str(t.get("id") or "") != holder_id
        ]
        if competing:
            return (
                holder_id,
                "\n\n[attestation_bind] bound to in-flight VERIFY "
                f"{holder_id[:8]} (queued VERIFYs ignored). Pass taskId "
                "explicitly to bind a different task.",
            )
        holder_in_open = any(
            str(t.get("id") or "") == holder_id for t in verify_open
        )
        if not holder_in_open:
            # In-flight already left created/claimed/running (submitted…).
            # Bind it only when this agent has no other active assignee work;
            # otherwise a running implementation task would lose the stamp.
            other_active = [
                t for t in (mine or [])
                if (t.get("status") or "") in ("running", "claimed")
                and str(t.get("id") or "") != holder_id
            ]
            if not other_active:
                return (
                    holder_id,
                    "\n\n[attestation_bind] bound to in-flight VERIFY "
                    f"{holder_id[:8]}. Pass taskId explicitly to bind a "
                    "different task.",
                )
    if len(verify_open) == 1:
        return verify_open[0].get("id"), ""
    if len(verify_open) > 1:
        # Fallback: extract taskId from command text (LLM often writes it
        # into the command instead of the tool param — TEST18 P0-2).
        if command and str(command).strip():
            for val in _COMMAND_TASK_ID_RE.findall(str(command)):
                cand = [
                    t for t in verify_open
                    if str(t.get("id") or "") == val
                    or (len(val) >= 8 and str(t.get("id") or "").startswith(val))
                ]
                if len(cand) == 1:
                    return (
                        cand[0].get("id"),
                        "\n\n[attestation_bind] bound taskId from command text "
                        f"({val[:12]}…); prefer the bash taskId parameter next time.",
                    )
                if len(cand) > 1:
                    break  # ambiguous prefix — fall through to refuse
        lines = [
            "\n\n[attestation_bind] test_run left UNBOUND: multiple open "
            "VERIFY tasks assigned to you. Pass taskId as the bash TOOL "
            "PARAMETER (not written into the command text):",
        ]
        for t in verify_open[:6]:
            tid = str(t.get("id") or "")
            title = (t.get("title") or "")[:40]
            st = t.get("status") or "?"
            lines.append(f"  - taskId={tid} status={st} title={title!r}")
        lines.append(
            "VERIFY tests must run via bash_main on MAIN — "
            "re-run with taskId=<id>."
        )
        return None, "\n".join(lines)

    active = [
        t for t in (mine or [])
        if t.get("status") in ("running", "claimed")
    ]
    if len(active) == 1:
        return active[0].get("id"), ""
    if len(active) > 1:
        lines = [
            "\n\n[attestation_bind] test_run left UNBOUND: multiple active "
            "tasks assigned to you. Pass taskId explicitly:",
        ]
        for t in active[:6]:
            tid = str(t.get("id") or "")
            title = (t.get("title") or "")[:40]
            st = t.get("status") or "?"
            lines.append(f"  - taskId={tid} status={st} title={title!r}")
        return None, "\n".join(lines)

    # TEST18 P0-4: REVIEW-capable helper (not creator/assignee/pinned) gets
    # an actionable tip listing open review candidates — never silent None.
    open_review = [
        t for t in (all_tasks or [])
        if t.get("status") in ("submitted", "reviewing")
    ]
    if open_review:
        has_review_cap = False
        try:
            from hiveweave.services.org import OrgService
            from hiveweave.services.policy import Capability, has_capability

            agent_row = await OrgService().get_agent(agent_id)
            has_review_cap = bool(
                agent_row and has_capability(agent_row, Capability.REVIEW)
            )
        except Exception:
            has_review_cap = False
        if has_review_cap:
            lines = [
                "\n\n[attestation_bind] test_run left UNBOUND: you hold REVIEW "
                "but are not the task creator/pinned reviewer/assignee. "
                "Pass taskId explicitly to bind evidence for approve/waive:",
            ]
            for t in open_review[:6]:
                tid = str(t.get("id") or "")
                title = (t.get("title") or "")[:40]
                st = t.get("status") or "?"
                lines.append(f"  - taskId={tid} status={st} title={title!r}")
            lines.append(
                "Example: bash(command='npm test', taskId=<id>)"
            )
            return None, "\n".join(lines)

    return None, ""


def _norm_ws(path: str) -> str:
    """Normalize workspace path for equality checks (Windows-safe)."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except Exception:
        return os.path.normcase(os.path.normpath(path))


def _is_same_workspace(a: str, b: str) -> bool:
    """True when *a* and *b* are the same directory after resolve.

    Worktrees live under ``<project>/.hiveweave/worktrees/<sid>/``, so
    "nested under project root" is not "on main". VERIFY UI evidence
    must use this, not :func:`_is_under_or_same`.
    """
    if not a or not b:
        return False
    return _norm_ws(a) == _norm_ws(b)


def _is_project_root_tree(path: str, main_ws: str) -> bool:
    """True when *path* is the project root or a non-worktree descendant.

    ``main/apps/web`` is still MAIN. ``main/.hiveweave/worktrees/A093`` is not.
    """
    if not _is_under_or_same(path, main_ws):
        return False
    n = _norm_ws(path).replace("\\", "/").lower()
    m = _norm_ws(main_ws).replace("\\", "/").lower().rstrip("/")
    rel = n[len(m):].lstrip("/") if n.startswith(m) else n
    return not (
        rel.startswith(".hiveweave/worktrees/")
        or "/.hiveweave/worktrees/" in f"/{rel}/"
    )


def _is_under_or_same(child: str, parent: str) -> bool:
    """True when child path equals parent or is nested under it."""
    if not child or not parent:
        return False
    try:
        c = Path(child).resolve()
        p = Path(parent).resolve()
        return c == p or p in c.parents
    except Exception:
        cn = _norm_ws(child)
        pn = _norm_ws(parent)
        return cn == pn or cn.startswith(pn.rstrip("\\/") + os.sep) or cn.startswith(
            pn.rstrip("\\/") + "/"
        )


def _task_needs_main_workspace(
    task: dict | None, *, include_ui_policy: bool = False
) -> bool:
    """VERIFY (and optionally ui_browser_e2e) evidence must stamp project root.

    Used only as a reject belt after the agent picked a workspace tool.
    Do not silently rewrite cwd from this.
    """
    if not task:
        return False
    from hiveweave.services.task import TaskService

    if TaskService._is_verify_task(task):
        return True
    if include_ui_policy and (task.get("policy_id") or "") == "ui_browser_e2e":
        return True
    return False


async def resolve_project_main_cwd(project_id: str | None) -> tuple[str, str]:
    """Project-root cwd for explicit *_main tools. Never infers from the task.

    Returns ``(cwd, error)``. ``cwd`` is empty when the root cannot be resolved.
    """
    if not project_id:
        return "", "no project_id — cannot resolve project root cwd."
    try:
        from hiveweave.services.worktree_review import project_main_workspace

        main_ws = await project_main_workspace(project_id)
    except Exception as e:
        return "", f"cannot resolve project root workspace: {e}"
    if not main_ws:
        return (
            "",
            "cannot resolve project root workspace — "
            "bash_main / browse_main / game_run_case_main need the "
            "project workspace binding.",
        )
    return main_ws, ""


_ATTESTATION_BANNER_PREFIX = "[ATTESTATION]"
_ATTEST_FOOTER_RE = re.compile(
    r"\[attestation_id=(?P<id>\S+) kind=(?P<kind>\S+)"
)


def _attestation_tool_fields(
    aid: str, kind: str, exit_code: int
) -> tuple[str, str, dict[str, Any]]:
    """Return (banner, footer, extra) for a test_run attestation."""
    banner = f"{_ATTESTATION_BANNER_PREFIX} attestation_id={aid} kind={kind}"
    footer = f"\n\n[attestation_id={aid} kind={kind} exit={exit_code}]"
    extra: dict[str, Any] = {
        "attestation_id": aid,
        "kind": kind,
        "banner": banner,
    }
    return banner, footer, extra


def _attestation_fields_from_note(note: str) -> dict[str, Any]:
    """Parse banner + ToolResult extras from an attestation footer note."""
    m = _ATTEST_FOOTER_RE.search(note or "")
    if not m:
        return {}
    aid, kind = m.group("id"), m.group("kind")
    return {
        "attestation_id": aid,
        "kind": kind,
        "banner": (
            f"{_ATTESTATION_BANNER_PREFIX} attestation_id={aid} kind={kind}"
        ),
    }


def _combine_attestation_output(output: str, banner: str, suffix: str) -> str:
    """Append attestation footer and prefix a first-line banner if missing."""
    body = f"{output}{suffix}"
    if not banner:
        return body
    first = body.split("\n", 1)[0]
    if _ATTESTATION_BANNER_PREFIX in first:
        return body
    return f"{banner}\n{body}"


def _attestation_public_extra(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: meta[k] for k in ("attestation_id", "kind") if k in meta}


async def _issue_test_run_attestation(
    *,
    project_id: str,
    agent_id: str,
    command: str,
    workspace: str,
    stdout: str,
    exit_code: int,
    task_id: str | None,
    exec_cwd: str | None = None,
) -> str:
    """Create test_run attestation (success or failure). Return note fragment.

    ``workspace`` is the stamp/root workspace. ``exec_cwd`` (optional) is the
    actual directory the command ran in (e.g. workspace/params.cwd) — used for
    VERIFY under-main checks.
    """
    from hiveweave.services.attestation import (
        attestation_service,
        is_test_command,
    )
    from hiveweave.services.task import TaskService

    if not is_test_command(command or ""):
        return ""
    resolved, bind_note = await _resolve_test_attestation_task_id(
        project_id, agent_id, task_id, command=command
    )
    if not resolved and "VERIFY" in (bind_note or ""):
        return (
            "\n\n[VERIFY ATTEST REJECTED] cannot stamp unbound VERIFY "
            "test_run. Use bash_main (project root) with an explicit "
            "taskId."
            + bind_note
        )

    # TEST18 P0-3 / NEW-3: VERIFY attestation must stamp MAIN workspace HEAD,
    # and the test must have executed there (not stamp-only while running in
    # a worktree descendant).
    stamp_workspace = workspace
    is_verify = False
    if resolved:
        try:
            task = await TaskService().get_task(project_id, resolved)
        except Exception as e:
            return (
                "\n\n[VERIFY ATTEST REJECTED] cannot load bound task for "
                f"MAIN check: {e}"
                + bind_note
            )
        if task is None:
            return (
                "\n\n[VERIFY ATTEST REJECTED] bound task not found — "
                "no attestation issued."
                + bind_note
            )
        is_verify = TaskService._is_verify_task(task)
        if is_verify:
            try:
                from hiveweave.services.worktree_review import (
                    project_main_workspace,
                )

                main_ws = await project_main_workspace(project_id)
                if not main_ws:
                    return (
                        "\n\n[VERIFY ATTEST REJECTED] cannot resolve project "
                        "main workspace — no attestation issued. Re-run tests "
                        "after project workspace is bound."
                        + bind_note
                    )
                stamp_workspace = main_ws
                check_path = exec_cwd or workspace or ""
                if not _is_project_root_tree(check_path, main_ws):
                    return (
                        "\n\n[VERIFY ATTEST REJECTED] tests ran outside project "
                        f"root (exec={check_path!r} main={main_ws!r}). "
                        "Use bash_main (project root), not bash (your worktree)."
                        + bind_note
                    )
            except Exception as e:
                return (
                    f"\n\n[VERIFY ATTEST REJECTED] main stamp failed: {e}"
                    + bind_note
                )

    # TEST6 evening E3: always stamp HEAD so VERIFY baseline gate can fire
    commit_hash: str | None = None
    if stamp_workspace:
        try:
            from hiveweave.util.win_subprocess import windows_no_window_kwargs

            proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=stamp_workspace,
                **windows_no_window_kwargs(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0 and out:
                commit_hash = (
                    out.decode("utf-8", errors="replace").strip()[:40] or None
                )
        except Exception:
            commit_hash = None
    aid = await attestation_service.create(
        project_id,
        agent_id=agent_id,
        kind="test_run",
        command_or_url=(command or "")[:500],
        exit_code=int(exit_code) if exit_code is not None else 1,
        workspace=stamp_workspace or workspace or "",
        commit_hash=commit_hash,
        stdout=str(stdout)[-8000:],
        task_id=resolved,
    )
    _, footer, _extra = _attestation_tool_fields(
        aid, "test_run", int(exit_code) if exit_code is not None else 1
    )
    note = footer
    if resolved:
        note += f" taskId={resolved}"
    else:
        note += " taskId=(unbound)"
    if commit_hash:
        note += f" commit={commit_hash[:12]}"
    if is_verify and stamp_workspace:
        note += " stamped_from=main"
    note += bind_note
    # Soft warn when VERIFY task baseline mismatches (approve hard-gates later)
    if resolved and commit_hash and is_verify:
        try:
            task = await TaskService().get_task(project_id, resolved)
            if task:
                ev = task.get("evidence") or {}
                if isinstance(ev, str):
                    import json as _json

                    try:
                        ev = _json.loads(ev)
                    except Exception:
                        ev = {}
                target = ""
                if isinstance(ev, dict):
                    target = str(
                        ev.get("target_merge_commit")
                        or ev.get("merge_commit")
                        or ""
                    ).strip()
                if target and not (
                    commit_hash.lower().startswith(target[:12].lower())
                    or target.lower().startswith(commit_hash[:12].lower())
                ):
                    note += (
                        f"\n[VERIFY BASELINE WARN] attestation commit="
                        f"{commit_hash[:12]} ≠ target_merge_commit="
                        f"{target[:12]}. Re-run tests on MAIN (project root) "
                        f"at the current tip before approve."
                    )
        except Exception:
            pass
    if resolved and int(exit_code or 1) == 0:
        try:
            await TaskService().emit_task_event(
                project_id,
                resolved,
                "test_attestation",
                agent_id=agent_id,
                summary=(
                    f"[test_attestation] task {resolved[:8]} via shell"
                ),
            )
        except Exception:
            pass
    return note


async def _bash_background(
    *,
    params: BashParams,
    agent_id: str,
    cmd: str,
    exec_ws: str,
    project_id: str | None,
    verify_tid: str | None,
) -> ToolResult:
    """Run bash off the org turn; attestations still issue when the job finishes."""
    from hiveweave.services.offturn import (
        build_waiting_on,
        next_action_waiting,
        resolve_assignee_task_id,
        start_offturn_job,
    )

    cmd = _strip_trailing_ampersand(cmd)
    blocked, reason = _validate_command_safety(cmd)
    if blocked:
        log.warning("bash.blocked", reason=reason, command_preview=cmd[:120])
        return ToolResult.blocked_err(f"Error: {reason}")

    from hiveweave.services.eval_seal import sealed_bash_deny_for_workspace

    seal_reason = sealed_bash_deny_for_workspace(exec_ws, cmd)
    if seal_reason:
        log.warning("bash.eval_sealed", command_preview=cmd[:120])
        return ToolResult.blocked_err(f"Error: {seal_reason}")

    port_hint = _detect_dev_server_command(cmd)
    if port_hint is not None:
        routed = await _run_registered_dev_server(
            cmd, exec_ws, exec_ws, project_id, port_hint,
            agent_id=agent_id,
        )
        if routed is not None:
            if routed.get("success"):
                return ToolResult.ok(routed.get("output") or "")
            err_msg = routed.get("error") or "Dev server spawn failed"
            if routed.get("blocked"):
                return ToolResult.blocked_err(err_msg)
            return ToolResult.err(err_msg)

    attest_task = getattr(params, "task_id", None) or verify_tid
    orig_command = params.command or ""
    task_id = await resolve_assignee_task_id(
        project_id or "", agent_id, attest_task
    )

    async def _work() -> tuple[bool, str]:
        result = await execute_bash(
            command=cmd,
            workdir="",
            workspace_path=exec_ws,
            timeout_ms=0,
            project_id=project_id,
            agent_id=agent_id,
            unbounded=True,
        )
        _update_cwd_failure_streak(
            agent_id, exec_ws, bool(result.get("success"))
        )
        out = result.get("output") or ""
        exit_code = result.get("exit_code")
        if exit_code is None:
            exit_code = 0 if result.get("success") else 1
        attest_note = ""
        try:
            if project_id:
                attest_note = await _issue_test_run_attestation(
                    project_id=project_id,
                    agent_id=agent_id,
                    command=orig_command,
                    workspace=exec_ws or "",
                    stdout=str(out),
                    exit_code=int(exit_code),
                    task_id=attest_task,
                    exec_cwd=exec_ws or "",
                )
        except Exception as att_err:
            log.warning("bash_attest_issue_failed", error=str(att_err))
        meta = _attestation_fields_from_note(attest_note)
        banner = meta.get("banner") or ""
        combined = _combine_attestation_output(out, banner, attest_note)
        if _note_is_attest_rejected(attest_note) or _note_is_attest_rejected(
            combined
        ):
            return False, combined.strip()
        if result.get("success"):
            return True, combined.strip()
        err_msg = result.get("error") or "Command failed"
        return False, _combine_attestation_output(err_msg, banner, attest_note)

    job_id = start_offturn_job(
        kind="bash",
        agent_id=agent_id,
        project_id=project_id or "",
        worktree=exec_ws or "",
        work=_work,
        task_id=task_id,
    )
    waiting_on = build_waiting_on(job_id, task_id, agent_id=agent_id)
    return ToolResult.ok(
        f"Bash started off the org turn (job={job_id}). "
        f"{next_action_waiting(waiting_on)} "
        "You will be woken with [BASH DONE] or [BASH FAILED]. "
        "Do not nest this command inside the current LLM call.",
        job_id=job_id,
        waiting_on=waiting_on,
        task_id=task_id,
    )


@tool(
    "bash",
    "Execute a shell command in YOUR workspace (worktree if you have one). "
    "Fresh shell each call (cwd does not persist). Check Exit code: N. "
    "Project-root tests / MAIN QA: use bash_main, not this tool. "
    "Long scripts: background=true returns waiting_on — then "
    "commit_turn(waiting); woken with [BASH DONE] / [BASH FAILED]. "
    "Stop with job_kill. Not PowerShell; prefer Git Bash, tail -n N, "
    "uv run python. Do not background=true for vite / npm run dev / "
    "uvicorn / http.server — servers never finish, so waiting_on never "
    "fires; prefer start_dev_server. Do not append & on a foreground "
    "command.",
    requires_workspace=True,
    security_level="shell",
)
async def bash_tool(params: BashParams, agent_id: str, workspace: str) -> ToolResult:
    """Execute a bash command."""
    from hiveweave.services.process_registry import prepare_spawn_command
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    raw_cmd = params.command or ""
    cmd, _env, reserved_err = prepare_spawn_command(
        raw_cmd, project_id=project_id
    )
    if reserved_err:
        # H3: 保留端口是平台护栏拒绝（复审 P2-1）
        return ToolResult.blocked_err(reserved_err)
    cmd = _strip_trailing_ampersand(cmd)

    exec_ws = workspace or ""
    verify_tid: str | None = getattr(params, "task_id", None)

    if getattr(params, "background", False):
        return await _bash_background(
            params=params,
            agent_id=agent_id,
            cmd=cmd,
            exec_ws=exec_ws,
            project_id=project_id,
            verify_tid=verify_tid,
        )

    # 前台尾部 &：长驻服务走注册 spawn；其余必须 offturn job，禁止 shell 脱管。
    if _should_offturn_trailing_amp(raw_cmd):
        return await _bash_background(
            params=params,
            agent_id=agent_id,
            cmd=cmd,
            exec_ws=exec_ws,
            project_id=project_id,
            verify_tid=verify_tid,
        )

    result = await execute_bash(
        command=cmd,
        workdir="",
        workspace_path=exec_ws,
        timeout_ms=params.timeout,
        project_id=project_id,
        agent_id=agent_id,
    )
    # D4: track consecutive failures per (agent_id, cwd)
    _streak_hint = _update_cwd_failure_streak(
        agent_id, exec_ws, bool(result.get("success"))
    )
    out = result.get("output") or ""
    exit_code = result.get("exit_code")
    if exit_code is None:
        exit_code = 0 if result.get("success") else 1
    # TEST6 P0-3: record failed test runs too (exit≠0); P0-2: bind reviewer taskId
    attest_note = ""
    attest_task = getattr(params, "task_id", None) or verify_tid
    try:
        if project_id:
            attest_note = await _issue_test_run_attestation(
                project_id=project_id,
                agent_id=agent_id,
                command=params.command or "",
                workspace=exec_ws or "",
                stdout=str(out),
                exit_code=int(exit_code),
                task_id=attest_task,
                exec_cwd=exec_ws or "",
            )
    except Exception as _att_err:
        log.warning("bash_attest_issue_failed", error=str(_att_err))
    meta = _attestation_fields_from_note(attest_note)
    banner = meta.get("banner") or ""
    public = _attestation_public_extra(meta)
    return _shell_tool_result(
        success=bool(result.get("success")),
        blocked=bool(result.get("blocked")),
        output=out,
        error=result.get("error") or "Command failed",
        banner=banner,
        suffix=attest_note,
        public=public,
        streak_hint=_streak_hint,
    )


def _note_is_attest_rejected(text: str) -> bool:
    return "VERIFY ATTEST REJECTED" in (text or "")


def _shell_tool_result(
    *,
    success: bool,
    blocked: bool,
    output: str,
    error: str,
    banner: str,
    suffix: str,
    public: dict[str, Any],
    streak_hint: str = "",
) -> ToolResult:
    """Command ok + VERIFY belt reject must fail the tool, not look like a pass."""
    if success:
        combined = _combine_attestation_output(output, banner, suffix)
        if _note_is_attest_rejected(suffix) or _note_is_attest_rejected(combined):
            return ToolResult.err(combined.strip(), **public)
        return ToolResult.ok(combined, **public)
    err_msg = error or "Command failed"
    if streak_hint:
        err_msg = f"{err_msg}{streak_hint}"
    err_msg = _combine_attestation_output(err_msg, banner, suffix)
    if blocked:
        # H3: 平台护栏拒绝（Command blocked）≠ 模型空转 —— 标 blocked 供
        # stall 检测分流，文本/exit code 语义与 err 一致。
        return ToolResult.blocked_err(err_msg, **public)
    return ToolResult.err(err_msg, **public)


def _with_cwd_note(result: ToolResult, note: str) -> ToolResult:
    if not note:
        return result
    if result.output:
        result.output = f"{result.output}{note}"
    elif result.error:
        result.error = f"{result.error}{note}"
    else:
        result.output = note.strip()
    return result


@tool(
    "bash_main",
    "Execute a shell command at the PROJECT ROOT (shared MAIN), not your "
    "worktree. Same params as bash. Use this for milestone VERIFY tests, "
    "MAIN git/log, or anything that must see merged HEAD. Your own slice "
    "unit tests stay on bash (worktree). Platform does not rewrite bash cwd.",
    requires_workspace=True,
    security_level="shell",
)
async def bash_main_tool(
    params: BashParams, agent_id: str, workspace: str
) -> ToolResult:
    """bash at project root — agent chose MAIN explicitly."""
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    main_ws, err = await resolve_project_main_cwd(project_id)
    if not main_ws:
        return ToolResult.err(err)
    note = "\n\n[cwd=project root]"
    result = await bash_tool(params, agent_id, main_ws)
    return _with_cwd_note(result, note)


@tool(
    "run_command",
    "Executes a command and returns the output. Similar to bash but with explicit working directory support. Use for running scripts, builds, tests, or any system command. For reviewer test_run binding pass taskId.",
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
        # H3: 保留端口是平台护栏拒绝（复审 P2-1）
        return ToolResult.blocked_err(reserved_err)

    exec_ws = workspace or ""
    verify_tid: str | None = getattr(params, "task_id", None)

    result = await execute_run_command(
        command=cmd,
        cwd=params.cwd,
        timeout_ms=params.timeout,
        workspace_path=exec_ws,
        agent_id=agent_id,
        project_id=project_id,
    )
    # D4: track consecutive failures per (agent_id, cwd)
    _effective_cwd = str(Path(exec_ws) / params.cwd) if params.cwd else exec_ws
    _streak_hint = _update_cwd_failure_streak(
        agent_id, _effective_cwd, bool(result.get("success"))
    )
    out = result.get("output") or ""
    exit_code = result.get("exit_code")
    if exit_code is None:
        exit_code = 0 if result.get("success") else 1
    attest_note = ""
    attest_task = getattr(params, "task_id", None) or verify_tid
    try:
        if project_id:
            attest_note = await _issue_test_run_attestation(
                project_id=project_id,
                agent_id=agent_id,
                command=params.command or "",
                workspace=exec_ws or "",
                stdout=str(out),
                exit_code=int(exit_code),
                task_id=attest_task,
                exec_cwd=_effective_cwd or exec_ws or "",
            )
    except Exception as _att_err:
        log.warning("bash_attest_issue_failed", error=str(_att_err))
    meta = _attestation_fields_from_note(attest_note)
    banner = meta.get("banner") or ""
    public = _attestation_public_extra(meta)
    return _shell_tool_result(
        success=bool(result.get("success")),
        blocked=bool(result.get("blocked")),
        output=out,
        error=result.get("error") or "Command failed",
        banner=banner,
        suffix=attest_note,
        public=public,
        streak_hint=_streak_hint,
    )

