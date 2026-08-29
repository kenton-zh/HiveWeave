"""FastAPI application entry point.

契约 15: SystemState + Application — startup/shutdown lifecycle.
契约 19: HTTP API — route registration + ApiKeyAuth middleware.
契约 12: Realtime — WebSocket route registration.

Startup sequence (对齐 Elixir Application.start/2):
1. init Meta DB (tables, indexes, DELETE journal mode)
2. Clear zombie streaming messages (is_streaming=true from prior crashes)
3. Seed default LLM model (OPENCODE_API_KEY → DeepSeek V4 Flash Free)
4. Start game time tick loop (5s interval)
5. Recover projects from agents table (boot-time repair)
6. Start all active agents (AgentManager.start_project_agents)

Shutdown sequence:
1. Stop game time tick loop
2. Stop all agent tasks
3. Close per-project DBs
4. Close Meta DB
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

# Force UTF-8 for stdout/stderr on Windows — prevents GBK encoding crashes
# when logging Unicode characters (emoji, CJK names) via structlog.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass  # Best-effort; may fail on redirected pipes

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import structlog

from hiveweave.config import settings


class _FlushFile:
    """Line-buffered-ish file that flushes every write (TEST21 M10)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1 is line-buffered only in text mode for tty; force flush.
        self._f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s: str) -> int:
        n = self._f.write(s)
        self._f.flush()
        return n

    def flush(self) -> None:
        self._f.flush()

    def fileno(self) -> int:
        return self._f.fileno()

    def isatty(self) -> bool:
        return False


class _TeeStream:
    """Write to multiple streams (console + durable log file)."""

    def __init__(self, *streams: object) -> None:
        self._streams = streams

    def write(self, s: str) -> int:
        for st in self._streams:
            write = getattr(st, "write", None)
            if write is not None:
                write(s)
            flush = getattr(st, "flush", None)
            if flush is not None:
                flush()
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            flush = getattr(st, "flush", None)
            if flush is not None:
                flush()

    def isatty(self) -> bool:
        return False


_LOG_FILE_STREAM: _FlushFile | None = None


def _configure_logging() -> None:
    """Configure structlog once at import (JSON when HIVEWEAVE_LOG_JSON=1).

    TEST21 M10: when ``HIVEWEAVE_LOG_FILE`` is set, tee every log line to that
    path with per-write flush so Ctrl+C / kill cannot evaporate a buffered
    stdout redirect.
    """
    global _LOG_FILE_STREAM
    json_logs = os.getenv("HIVEWEAVE_LOG_JSON", "").lower() in ("1", "true", "yes")
    level_name = os.getenv("HIVEWEAVE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_logs:
        renderer: object = structlog.processors.JSONRenderer()
    else:
        try:
            colors = sys.stdout.isatty()
        except Exception:
            colors = False
        renderer = structlog.dev.ConsoleRenderer(colors=colors)

    log_stream: object = sys.stdout
    log_path = (os.getenv("HIVEWEAVE_LOG_FILE") or "").strip()
    if log_path:
        try:
            _LOG_FILE_STREAM = _FlushFile(Path(log_path))
            log_stream = _TeeStream(sys.stdout, _LOG_FILE_STREAM)
        except OSError as e:
            # Fail closed at lifespan via log_vital_sign; here keep console.
            print(
                f"WARNING: cannot open HIVEWEAVE_LOG_FILE={log_path!r}: {e}",
                file=sys.stderr,
            )
            _LOG_FILE_STREAM = None

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=log_stream),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s",
        stream=cast(Any, log_stream),
        level=cast(int, level),
    )


_configure_logging()
log = structlog.get_logger(__name__)


def _assert_log_vital_sign() -> None:
    """TEST21 M10: if a log file was requested, refuse to run without it."""
    log_path = (os.getenv("HIVEWEAVE_LOG_FILE") or "").strip()
    if not log_path:
        return
    if _LOG_FILE_STREAM is None:
        print(
            f"FATAL: HIVEWEAVE_LOG_FILE={log_path!r} is set but not writable. "
            "Refusing to start (logs would be lost on kill).",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        _LOG_FILE_STREAM.write(
            f'{{"event":"log_vital_sign","path":"{log_path}","ok":true}}\n'
        )
        _LOG_FILE_STREAM.flush()
    except OSError as e:
        print(f"FATAL: log_vital_sign write failed: {e}", file=sys.stderr)
        sys.exit(1)
    log.info("log_vital_sign", path=log_path)


async def _scan_legacy_stash_warnings() -> None:
    """P1-2 行为层补：启动后提醒遗留 git stash（r2/r3 实证 stash@{0}
    滞留两轮、.gitignore 人机拉锯重演）。只读扫描 + 聚合 warning，不阻塞。"""
    import sqlite3
    import subprocess

    try:
        conn = sqlite3.connect(settings.get_meta_db_path())
        rows = conn.execute(
            "SELECT name, workspace_path, is_started FROM projects "
            "WHERE is_started=1"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.debug("legacy_stash_meta_read_failed", error=str(e))
        return
    for name, ws, _started in rows:
        if not ws or not Path(ws).exists():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", ws, "stash", "list"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            ).stdout or ""
            if out.strip():
                log.warning(
                    "legacy_stash_pending",
                    project=name,
                    workspace=ws,
                    stash_head=out.strip().splitlines()[0][:120],
                    hint="merge 会硬拒 MAIN dirty；请 pop/清点遗留工作再重启组织",
                )
        except Exception as e:
            log.debug("legacy_stash_git_failed", project=name, error=str(e))


def _code_version() -> str:
    """TEST6 P3: surface the running code's git short hash in startup logs.

    TEST6 ran gates against fixes committed hours earlier that the (stale)
    backend process had never loaded — invisible until behavior was audited.
    Resolve ``.git/HEAD`` → ref → short hash; any failure returns "unknown".
    """
    try:
        # main.py → hiveweave/ → src/ → hiveweave-py/ → apps/ → repo root
        git_dir = Path(__file__).resolve().parents[4] / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_file = git_dir / ref
            if ref_file.exists():
                return ref_file.read_text(encoding="utf-8").strip()[:12]
            packed = (git_dir / "packed-refs").read_text(
                encoding="utf-8", errors="replace"
            )
            for ln in packed.splitlines():
                if ln.endswith(f" {ref}"):
                    return ln.split(" ", 1)[0][:12]
            return "unknown"
        return head[:12]  # detached HEAD
    except Exception:
        return "unknown"


def _install_console_signal_guard() -> None:
    """Windows 隐藏 conhost 场景的 Ctrl 信号守卫（误伤不停机）。

    背景（审计 2026-08-29 停机根因）：工具子进程（pwsh/bash 等，经
    tools/bash.py _run_native spawn）与后端共享同一个隐藏 conhost 且同进程组；
    子进程内 `os.kill(pid, 0)` 探活会被 Windows 当作 CTRL_C_EVENT 广播
    （CTRL_C_EVENT == 0，conhost 对 pid/pgid 处理有已知 bug），命中整个
    控制台 → uvicorn 默认 SIGINT handler 把后端优雅停机。

    deepseek-harness 同款防御（sandbox-windows-acl runner SetConsoleCtrlHandler
    忽略自身 CTRL+C + 保留子进程自理）。两个豁免保证「停得掉也停得掉」：
    1) env 逃生门：``HIVEWEAVE_IGNORE_CTRL_SIGNALS=off`` 恢复 uvicorn 默认
       handler（任何场景都能显式关守卫）；
    2) 交互可见控制台不装守卫（前台 start-backend.bat / 可见终端直跑）：物理
       可按 Ctrl+C，保留优雅停机；仅隐藏 conhost（start-all.bat / VBS，按键
       不可达）才吞信号，防误伤广播停全栈。
    停平台恒保留硬通道：stop.bat / taskkill /F、关闭控制台（CTRL_CLOSE=硬杀）。
    """
    if not sys.platform.startswith("win"):
        return

    # 方案 2：env 逃生门 —— 显式关守卫，恢复 uvicorn 默认 handler。
    flag = os.getenv("HIVEWEAVE_IGNORE_CTRL_SIGNALS", "on").strip().lower()
    if flag in ("off", "0", "false", "no", "disabled"):
        log.info("console_signal_guard_disabled_by_env", value=flag)
        return

    import ctypes  # noqa: PLC0415
    import signal  # noqa: PLC0415

    # 方案 1：可见控制台 = 交互开发，保留默认 Ctrl+C 优雅停机；隐藏 conhost
    # （VBS SW_HIDE，物理按键不可达）才装守卫。无控制台窗口（GetConsoleWindow=0）
    # 按隐藏处理——脚本/集成终端类场景同样要防误伤广播。
    try:
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 64 位 Python 下 HWND 是 64 位句柄：不设 restype 会按 c_int 截断
        # （高位被截 → hwnd 变 0/负数 → 可见控制台被误判为隐藏而误装守卫）。
        get_console_window = kernel32.GetConsoleWindow
        get_console_window.restype = wintypes.HWND
        is_window_visible = user32.IsWindowVisible
        is_window_visible.argtypes = [wintypes.HWND]
        is_window_visible.restype = wintypes.BOOL
        hwnd = get_console_window()
        if hwnd and is_window_visible(hwnd):
            log.info(
                "console_signal_guard_skipped_visible_console",
                note="visible console: Ctrl+C keeps graceful stop",
            )
            return
    except Exception:
        pass

    # 信号上下文避免 import/分配/stdio 流重入：ctypes 原语与缓冲一次性预取，
    # 日志用 os.write 走 fd 直写，绕过 TextIOWrapper/_TeeStream 锁。
    get_console_process_list = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_console_process_list = kernel32.GetConsoleProcessList
        get_console_process_list.argtypes = [
            ctypes.POINTER(ctypes.c_uint),
            ctypes.c_uint,
        ]
        get_console_process_list.restype = ctypes.c_uint
    except Exception:
        pass
    pid_buf = (ctypes.c_uint * 16)()

    def _on_ctrl_signal(signum: int, _frame: object) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        pids: list[int] = []
        if get_console_process_list is not None:
            try:
                got = int(get_console_process_list(pid_buf, len(pid_buf)))
                pids = [int(p) for p in pid_buf[:got]]
            except Exception:
                pass
        line = (
            '{"event":"console_signal_ignored","signum":%d,"name":"%s",'
            '"console_pids":[%s]}\n' % (signum, name, ",".join(str(p) for p in pids))
        ).encode("utf-8", errors="replace")
        for stream in (sys.stdout, sys.stderr, _LOG_FILE_STREAM):
            if stream is None:
                continue
            try:
                os.write(stream.fileno(), line)
            except Exception:
                pass

    signal.signal(signal.SIGINT, _on_ctrl_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_ctrl_signal)
    log.info(
        "console_signal_guard_installed",
        note="SIGINT/SIGBREAK logged but ignored; stop platform via stop.bat",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown.

    契约 15: SystemState + Application
    """
    from hiveweave.db.meta import init_meta_db, close_meta_db
    from hiveweave.db.project import close_all as close_project_dbs
    from hiveweave.services.system_state import SystemState
    from hiveweave.services.model import ModelService
    from hiveweave.services.game_time import GameTimeService
    from hiveweave.services.chat_message import ChatMessageService
    from hiveweave.services.approval import approval_service
    from hiveweave.agents.supervisor import agent_manager

    # ── Startup ──────────────────────────────────────────────
    _assert_log_vital_sign()
    # 覆盖 uvicorn 的 SIGINT/SIGBREAK handler（serve 内 capture_signals 先注册，
    # lifespan startup 晚于它，此处覆盖生效）——共享控制台误伤只记录不退出。
    try:
        _install_console_signal_guard()
    except Exception as e:
        log.warning("console_signal_guard_failed", error=str(e))
    log.info("app_starting", port=settings.port, code_version=_code_version())

    # 0. slack-clone_01 P0-2: 登记平台宿主进程保护集（自身 + 祖先链 +
    #    HIVEWEAVE_PROTECTED_PIDS）。kill 族命令命中受保护 PID 一律 deny，
    #    不受规则开关影响（底线层，防 taskkill //PID 误杀后端）。
    try:
        from hiveweave.services.command_guard import init_process_protection

        init_process_protection()
    except Exception as e:
        log.warning("command_guard_init_failed", error=str(e))

    # 0b. 宿主环境探测（platform-issue-remediation Phase 0：T3.2/T3.3 前置）。
    #    启动时把「这台宿主能做什么」探成不可变结果；单条探测失败不炸启动
    #    （fail-closed 落在消费方 get_capability，不在这里）。
    try:
        from hiveweave.services.host_env import (
            register_builtin_probes,
            run_startup_probes,
        )

        register_builtin_probes()
        startup_probes = await asyncio.to_thread(run_startup_probes)
        log.info(
            "host_env_probed",
            ok=sorted(startup_probes),
        )
    except Exception as e:
        log.warning("host_env_probe_failed", error=str(e))

    # 1. Init Meta DB
    # Security/Fail-fast: Meta DB 是整个系统的基石 — 路由表、projects、llm_models
    # 都在这里。初始化失败若继续运行会导致 agent 路由错乱、写入丢失、沉默故障。
    # 改为 fail-fast：log.critical + stderr 提示 + sys.exit(1)。
    try:
        await init_meta_db()
        log.info("meta_db_initialized")
    except Exception as e:
        log.critical("meta_db_init_failed", error=str(e))
        print(f"FATAL: Meta DB init failed: {e}", file=sys.stderr)
        sys.exit(1)

    # 1b. Register built-in lifecycle hook handlers (task-advance, …)
    try:
        from hiveweave.hooks.handlers import register_builtin_handlers

        register_builtin_handlers()
        log.info("lifecycle_hooks_registered")
    except Exception as e:
        log.warning("lifecycle_hooks_register_failed", error=str(e))

    # 2. Clear zombie streaming messages
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.db.project import ensure_project_db
        projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
        for p in projects:
            try:
                conn = await ensure_project_db(p["workspace_path"])
                svc = ChatMessageService(p["id"])
                await svc.clear_stuck_streaming()
            except Exception as e:
                log.warning("zombie_clear_failed", project_id=p["id"], error=str(e))
        log.info("zombie_streaming_cleared", projects=len(projects))
    except Exception as e:
        log.warning("zombie_streaming_clear_failed", error=str(e))

    # 2a. E16 (复盘 P2): 数据卫生 —— 启动收尾 sweep，清算上次进程残留的
    # status='running' agent_runs 孤儿行（归并为 interrupted，语义同既有
    # interrupt_run，供恢复/审计读取）。
    try:
        from hiveweave.services.run_ledger import sweep_stale_agent_runs

        projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
        swept = 0
        for p in projects:
            swept += await sweep_stale_agent_runs(p["workspace_path"])
        if swept:
            log.info("stale_agent_runs_swept", count=swept)
    except Exception as e:
        log.warning("stale_agent_runs_sweep_failed", error=str(e))

    # 2b. R12 fix: 清理过期工具输出临时文件（7 天保留期）
    try:
        from hiveweave.tools.executor import ToolExecutor
        projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
        cleaned = 0
        for p in projects:
            try:
                # R13 fix: p 是 aiosqlite.Row，不支持 .get()，改用 [] 索引
                # （查询显式 SELECT workspace_path，列一定存在；NULL 时返回 None）
                ws = p["workspace_path"]
                if ws:
                    ToolExecutor.cleanup_tool_outputs(ws)
                    cleaned += 1
            except Exception as e:
                log.warning("tool_output_cleanup_failed", project_id=p["id"], error=str(e))
        log.info("tool_outputs_cleaned", projects=cleaned)
    except Exception as e:
        log.warning("tool_output_cleanup_init_failed", error=str(e))

    # R4: token_utils 的 %TEMP%/hiveweave_tool_output/ 存盘点此前无任何清理
    # 调用方（同一平台两套存盘点，一套有 GC 一套没有）。与 executor 版一起
    # 在启动时清理 7 天前文件。
    try:
        from hiveweave.conversation.token_utils import cleanup_tool_outputs
        cleanup_tool_outputs()
        log.info("token_tool_outputs_cleaned")
    except Exception as e:
        log.warning("token_tool_output_cleanup_failed", error=str(e))

    # 2b-migration: Legacy agent migration from meta_db to per-project DB
    # has been removed. The old 'agents' table and 'agent_index' table are
    # cleaned up by _migrate_meta_schema() in meta.py (DROP TABLE IF EXISTS).
    # Agent routing is now handled by AgentRouter (in-memory) rebuilt at startup.

    # 2c. Recover stale git worktrees for executor agents
    # If a worktree directory was deleted (e.g., by sandbox cleanup or manual
    # deletion), the git branch ref remains and blocks re-creation with -b.
    # Step 1: prune stale worktree metadata. Step 2: re-create missing worktrees
    # for active executor agents and update their workspace_path in DB.
    try:
        from hiveweave.services.git_worktree import GitWorktreeService, _git
        from hiveweave.db import meta as meta_db
        from hiveweave.db import project as project_db
        import time as _wt_time
        projects = await meta_db.query(
            "SELECT id, workspace_path FROM projects WHERE 1=1"
        )
        recovered = 0
        for p in projects:
            ws = p["workspace_path"]
            if not ws or not (Path(ws) / ".git").exists():
                continue
            # Prune stale worktree metadata
            await _git(["worktree", "prune"], ws)
            # Find writer agents (executor + builder coordinator) with missing
            # worktrees (agents 表在 per-project DB)；CEO/HR 强制项目根，跳过
            try:
                proj_conn = await project_db.get_project_db_by_project_id(p["id"])
            except project_db.ProjectDbError:
                continue
            agent_cursor = await proj_conn.execute(
                "SELECT id, name, role, short_id, workspace_path, permission_type "
                "FROM agents WHERE project_id=? AND status='active' "
                "AND permission_type IN ('executor', 'coordinator')",
                [p["id"]],
            )
            agents = await agent_cursor.fetchall()
            await agent_cursor.close()
            from hiveweave.services.git_worktree import agent_gets_write_worktree
            agents = [a for a in agents if agent_gets_write_worktree(dict(a))]
            gwt = GitWorktreeService()
            for a in agents:
                short_id = a["short_id"]
                cur_ws = a["workspace_path"] or ""
                # Check if worktree directory exists
                if cur_ws and Path(cur_ws).exists() and (Path(cur_ws) / ".git").exists():
                    # Clear stale worktree_error left by soft-fail races
                    # (tree healthy but error string never wiped).
                    try:
                        row_err = await proj_conn.execute(
                            "SELECT worktree_error FROM agents WHERE id=?",
                            [a["id"]],
                        )
                        err_row = await row_err.fetchone()
                        await row_err.close()
                        if err_row and err_row[0]:
                            await proj_conn.execute(
                                "UPDATE agents SET worktree_error=NULL, "
                                "updated_at=? WHERE id=?",
                                [int(_wt_time.time() * 1000), a["id"]],
                            )
                            await proj_conn.commit()
                            log.info(
                                "worktree_error_cleared_healthy",
                                agent_id=a["id"],
                                short_id=short_id,
                            )
                    except Exception:
                        pass
                    continue  # Worktree is fine
                # Recreate
                role = a["role"] or "developer"
                result = await gwt.create(
                    workspace_path=ws,
                    short_id=short_id,
                    task_name=role,
                )
                if result.get("success") and result.get("path"):
                    # BUG-FIX: 直接用 proj_conn 更新，不走 project_db.execute(agent_id)。
                    # 后者依赖 agent_router 内存映射，启动恢复时映射可能尚未包含
                    # 新创建的 agent，导致 "No project DB found for agent" 错误。
                    await proj_conn.execute(
                        "UPDATE agents SET workspace_path=?, worktree_error=NULL, "
                        "updated_at=? WHERE id=?",
                        [result["path"], int(_wt_time.time() * 1000), a["id"]],
                    )
                    await proj_conn.commit()
                    recovered += 1
                    log.info("worktree_recovered",
                             agent_id=a["id"], short_id=short_id,
                             path=result["path"])
                else:
                    err = result.get("message") or "worktree recover failed"
                    # BUG-4: create may report failure while tree already exists
                    from hiveweave.services.git_worktree import (
                        _has_git,
                        _worktree_path,
                    )

                    expected = _worktree_path(ws, short_id)
                    if _has_git(expected):
                        await proj_conn.execute(
                            "UPDATE agents SET workspace_path=?, worktree_error=NULL, "
                            "updated_at=? WHERE id=?",
                            [expected, int(_wt_time.time() * 1000), a["id"]],
                        )
                        await proj_conn.commit()
                        recovered += 1
                        log.info(
                            "worktree_recovered_after_soft_fail",
                            agent_id=a["id"],
                            short_id=short_id,
                            path=expected,
                            soft_error=err,
                        )
                    else:
                        await proj_conn.execute(
                            "UPDATE agents SET worktree_error=?, updated_at=? WHERE id=?",
                            [err, int(_wt_time.time() * 1000), a["id"]],
                        )
                        await proj_conn.commit()
                        log.warning("worktree_recover_failed",
                                    agent_id=a["id"], short_id=short_id,
                                    error=err)
        log.info("worktree_recovery_done", recovered=recovered)
    except Exception as e:
        log.warning("worktree_recovery_init_failed", error=str(e))

    # P1 (spec §7.1 M-5)：ACL 沙箱启动回填 —— 存量项目 + worktree 逐个后台
    # verify-then-skip（串行防 IO 风暴；沙箱 off 时 no-op）。
    try:
        from hiveweave.services.acl_sandbox.integration import acl_sandbox_active
        from hiveweave.services.acl_sandbox.service import ensure_standing_grants

        if acl_sandbox_active():
            import asyncio as _asyncio

            async def _acl_backfill() -> None:
                try:
                    from hiveweave.db import project as project_db

                    projs = await meta_db.query(
                        "SELECT id, workspace_path FROM projects WHERE 1=1"
                    )
                    backfilled = 0
                    for p in projs:
                        root = p["workspace_path"]
                        if not root or not (Path(root) / ".git").exists():
                            continue
                        try:
                            await ensure_standing_grants(
                                workspace_path=root, project_workspace_path=root
                            )
                            try:
                                conn = await project_db.get_project_db_by_project_id(
                                    p["id"]
                                )
                                cur = await conn.execute(
                                    "SELECT workspace_path, short_id FROM agents "
                                    "WHERE project_id=? AND status='active'",
                                    [p["id"]],
                                )
                                rows = await cur.fetchall()
                                await cur.close()
                            except Exception:
                                rows = []
                            for a in rows:
                                wt = a["workspace_path"]
                                if wt and Path(wt).exists():
                                    await ensure_standing_grants(
                                        workspace_path=wt,
                                        project_workspace_path=root,
                                        agent_id=a["short_id"] or "system",
                                    )
                            backfilled += 1
                        except Exception as e:
                            log.warning(
                                "acl_sandbox_backfill_failed",
                                project_id=p["id"], error=str(e),
                            )
                    log.info("acl_sandbox_backfill_done", projects=backfilled)
                except Exception as e:
                    log.warning("acl_sandbox_backfill_error", error=str(e))

            _asyncio.create_task(_acl_backfill())
    except Exception as e:
        log.warning("acl_sandbox_backfill_spawn_failed", error=str(e))

    # P1 (spec §13)：哨兵探针循环 —— 沙箱 on 时周期注入 S-1-4 探针，断言
    # 各入口走受限令牌（任一入口未过 = 判据未达，log.error + 遥测）。
    try:
        from hiveweave.services.acl_sandbox.integration import acl_sandbox_active
        from hiveweave.services.acl_sandbox.sentinel import start_sentinel_loop

        if acl_sandbox_active():
            start_sentinel_loop()
    except Exception as e:
        log.warning("acl_sandbox_sentinel_start_failed", error=str(e))

    # 2b. TEST13 P1-3: reconcile orphan verification_cases
    try:
        from hiveweave.services.task import VerificationCaseService

        vcs = VerificationCaseService()
        vc_fixed = 0
        for p in projects:
            try:
                vc_fixed += await vcs.reconcile_orphans(p["id"])
            except Exception:
                pass
        if vc_fixed:
            log.info("verification_cases_reconcile_done", fixed=vc_fixed)
    except Exception as e:
        log.warning("verification_cases_reconcile_failed", error=str(e))

    # 3. Seed default model
    try:
        model_svc = ModelService()
        await model_svc.seed_default_model()
        log.info("default_model_seeded")
    except Exception as e:
        log.warning("default_model_seed_failed", error=str(e))

    # R4: 恢复/清理 pending approval 请求（重启后 _pending 丢失）
    try:
        await approval_service.cleanup_orphaned_requests()
        log.info("approval_requests_restored")
    except Exception as e:
        log.warning("approval_restore_failed", error=str(e))

    # 4. Start game time tick loop — only for started projects
    game_time_projects: list[str] = []
    try:
        from hiveweave.db import meta as meta_db
        # Bug K fix: 重启后所有项目默认"下班"，不自动启动 agents/game_time
        # 用户需要手动调用 POST /api/projects/{id}/activate 来"上班"
        await meta_db.execute(
            "UPDATE projects SET is_started = 0"
        )
        # 只启动 is_started=1 的项目（重启前已经"上班"的）
        # 由于上面刚重置为 0，这里实际上不会启动任何项目
        projects = await meta_db.query(
            "SELECT id FROM projects WHERE is_started = 1"
        )
        for p in projects:
            try:
                gt = GameTimeService(p["id"])
                await gt.start(p["id"])
                game_time_projects.append(p["id"])
            except Exception as e:
                log.warning("game_time_start_failed", project_id=p["id"], error=str(e))
        log.info("game_time_started", started_projects=len(projects),
                 total_projects="all reset to 0 on startup")
    except Exception as e:
        log.warning("game_time_init_failed", error=str(e))

    # 4b. Rebuild agent_router (in-memory agent_id → project_id routing)
    try:
        from hiveweave.services.agent_router import agent_router
        total = await agent_router.rebuild()
        log.info("agent_router_rebuilt", total_agents=total)
    except Exception as e:
        log.warning("agent_router_rebuild_failed", error=str(e))

    # 4c. P1(TEST9): 重启重建 wait 超时"闹钟"。agent_waits 持久化了 parked
    # wait 的 expires_at，但超时唤醒依赖 tick；重启后项目 off-duty、tick
    # 不跑，parked agent 会永久停摆。此处对所有项目：已到期 wait 立即
    # 清除+通知+唤醒，未到期 wait 武装一次性定时器兜底（activate 后 tick
    # 接管，幂等）。须在 agent_router 重建之后（[WAIT_TIMEOUT] 投递与
    # 唤醒依赖路由）。
    try:
        from hiveweave.db import meta as meta_db
        projects = await meta_db.query("SELECT id FROM projects WHERE 1=1")
        for p in projects:
            try:
                await GameTimeService(p["id"]).recover_wait_timeouts(p["id"])
            except Exception as e:
                log.warning("wait_recovery_failed", project_id=p["id"],
                            error=str(e))
        log.info("wait_timeouts_recovered", projects=len(projects))
    except Exception as e:
        log.warning("wait_recovery_init_failed", error=str(e))

    # 5. Start agents only for started projects
    try:
        projects = await meta_db.query(
            "SELECT id FROM projects WHERE is_started = 1"
        )
        for p in projects:
            try:
                await agent_manager.start_project_agents(p["id"])
            except Exception as e:
                log.warning("agent_start_failed", project_id=p["id"], error=str(e))
        log.info("agents_started", started_projects=len(projects))
    except Exception as e:
        log.warning("agent_recovery_failed", error=str(e))

    # 6. Start independent health supervisor (not dependent on game_time)
    try:
        from hiveweave.services.health_supervisor import health_supervisor
        health_supervisor.start()
    except Exception as e:
        log.warning("health_supervisor_start_failed", error=str(e))

    # Security: 启动序列末尾检测不安全配置（空 API key + 非 loopback host）。
    # 仅打 WARNING 日志，不阻止启动 — 让运维看到醒目提示后自行加固。
    try:
        from hiveweave.config import warn_if_insecure
        warn_if_insecure(settings.host, settings.api_key)
    except Exception as e:
        log.warning("security_warn_failed", error=str(e))

    def _spawn_legacy_stash_scan() -> None:
        async def _task() -> None:
            try:
                await _scan_legacy_stash_warnings()
            except Exception as e:
                log.warning("legacy_stash_scan_failed", error=str(e))

        _asyncio.create_task(_task())

    _spawn_legacy_stash_scan()
    log.info("app_started")

    yield

    # ── Shutdown ─────────────────────────────────────────────
    log.info("app_stopping")

    # Stop health supervisor first (before game_time)
    try:
        from hiveweave.services.health_supervisor import health_supervisor
        health_supervisor.stop()
    except Exception:
        pass

    # Stop game time tick loops
    for pid in game_time_projects:
        try:
            gt = GameTimeService(pid)
            await gt.stop(pid)
        except Exception as e:
            log.warning("game_time_stop_failed", project_id=pid, error=str(e))
    log.info("game_time_stopped")

    # Reap native off-turn jobs before stopping agents so completion
    # cannot trigger_subordinate and revive a stopped agent.
    try:
        from hiveweave.services.offturn import reap_all_offturn_jobs

        await reap_all_offturn_jobs()
    except Exception as e:
        log.warning("offturn_shutdown_reap_failed", error=str(e))

    # Stop all agents
    try:
        all_agents = agent_manager.list_all()
        # R10: list_all() 返回 Agent 对象，stop_agent 期望 agent_id 字符串
        agent_ids = [
            a.id if hasattr(a, "id") else str(a) for a in all_agents
        ]
        for agent_id in agent_ids:
            await agent_manager.stop_agent(agent_id)
        log.info("agents_stopped", count=len(agent_ids))
    except Exception as e:
        log.warning("agent_stop_failed", error=str(e))

    # Close DBs
    await close_project_dbs()
    await close_meta_db()

    # P1 (spec §7.1)：ACL 沙箱 runner/watcher/sentinel 线程回收 —— 受限子进程
    # 已由 KILL_ON_CLOSE Job 全灭，此处只回收排空池/watcher/探针（非守护线程
    # 不回收会阻塞进程退出）。
    try:
        from hiveweave.services.acl_sandbox.service import shutdown_runner
        from hiveweave.services.acl_sandbox.sentinel import stop_sentinel_loop
        from hiveweave.services.acl_sandbox.spawn import stop_watcher

        stop_sentinel_loop()
        stop_watcher()
        shutdown_runner()
    except Exception as e:
        log.warning("acl_sandbox_shutdown_failed", error=str(e))

    log.info("app_stopped")


app = FastAPI(
    title="HiveWeave API",
    version="0.1.0",
    description="HiveWeave — multi-agent AI workspace (Python port from Elixir/Phoenix)",
    lifespan=lifespan,
)

# ── Middleware ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 契约 19: ApiKeyAuth — 校验所有 /api/* 端点（settings.api_key 为空时全放行）
from hiveweave.api.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)

# BUG-009/012/013 fix: ensure all JSON responses carry charset=utf-8
# to prevent CJK mojibake when browsers/ proxies treat JSON as Latin-1
@app.middleware("http")
async def charset_middleware(request: Request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "application/json" in ct and "charset" not in ct:
        response.headers["content-type"] = f"{ct}; charset=utf-8"
    return response

# 请求日志中间件 — 记录每个 API 调用的耗时和状态码
import time as _time

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # 跳过高频轮询请求
    path = request.url.path
    skip_prefixes = (
        "/api/projects/", "/api/chat/questions", "/api/user-pings",
        "/api/communications", "/api/permissions/pending",
    )
    is_polling = any(path.startswith(p) for p in skip_prefixes) and request.method == "GET"

    if is_polling:
        return await call_next(request)

    start = _time.monotonic()
    try:
        response = await call_next(request)
        elapsed_ms = round((_time.monotonic() - start) * 1000)
        # 记录关键 API 调用
        if response.status_code >= 400 or path.startswith("/api/chat") or path.startswith("/api/org"):
            log.info(
                "http_request",
                method=request.method,
                path=path,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        return response
    except Exception as e:
        elapsed_ms = round((_time.monotonic() - start) * 1000)
        log.error(
            "http_request_error",
            method=request.method,
            path=path,
            error=str(e),
            elapsed_ms=elapsed_ms,
        )
        raise


# ── Route Registration ──────────────────────────────────────
from hiveweave.api.router import register_routes as _register_api
from hiveweave.realtime.channels import register_ws_routes as _register_ws
from hiveweave.realtime.phoenix_adapter import register_phoenix_route as _register_phoenix

_register_api(app)
_register_ws(app)
_register_phoenix(app)  # /socket/websocket — 前端 phoenix.js 兼容
