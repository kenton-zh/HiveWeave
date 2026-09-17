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

# 方言词表真值源（与 prompts/executor.py 共用；零依赖，不引入循环）。
from hiveweave.tools.shell_dialect import (
    ALIAS_FLAG_HINTS,
    UNIX_ONLY_HINTS,
)

# P1-3（B 结构解）：pwsh 输出的 UTF-8 编码钉（对齐 deepseek-harness
# pwsh-local 的 ENCODING_PREAMBLE index.ts:48-49）。Windows PowerShell
# 5.1 默认写 OEM 代码页会 garbled 非 ASCII；pwsh 7 默认 UTF-8 不受影响。
# 命令先钉编码再执行，避免中文 cat 输出乱码（report P1-3 实测 3 次乱码）。
PWSH_ENCODING_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false);\n"
)

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import structlog

from hiveweave.util import path_guard
from hiveweave.util.tree_label import cwd_display
from hiveweave.tools.fact_positions import classify_error_text
from hiveweave.tools.result import finalize_fact_dict
# F5（2026-09-17）：spawn 面事实的**唯一登记点**（决策面 + 加固面 + 执行面）。
# 上方 `_SHELL_FACT_FLAG_KEYS` 从此派生 —— 新增一种事实只在 policy 登记一次。
from hiveweave.services.acl_sandbox.policy import ALL_SPAWN_STAMP_KEYS

log = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────

DEFAULT_TIMEOUT_S = 120          # 2 minutes
MAX_TIMEOUT_S = 600              # 10 minutes hard cap
MAX_CAPTURE_BYTES = 1_048_576    # 1MB — bash 专用轻量截断阈值

# A-2 (P1-4) 按工具声明的默认超时（ms）。未列出的工具回退 DEFAULT_TIMEOUT_S。
# Pydantic 模型默认值、execute_bash/run_command/python_script 的兜底分支、
# 以及 executor.py 的 TOOL_PARAM_SCHEMAS 说明都从这里取数，避免"声明"与
# "实际执行"两处漂移。bash/pwsh 共享 BashParams → 同为 500s。
# F7（平台修复计划 2026-08-30）：统一三档阈值 —— bash/pwsh/脚本/命令
# 全部对齐 30s / 120s / 600s 三档，不再按出现顺序随手定值：
#   bash/pwsh/bash_main/pwsh_main : 600s（重档 —— 慢 build/test 面给足时间）
#   python_script                 : 120s（中档 —— 早于 streaming-zombie 收口，
#                                     脚本超时下放给 command 超时分类）
#   run_command                   : 120s（中档 —— 显式 cwd 逃生口）
# 5s-600s 的输入钳制与既有一致；MAX_TIMEOUT_S=600 即档位上限。
TOOL_DEFAULT_TIMEOUT_MS: dict[str, int] = {
    "bash": 600_000,          # 重档：慢 build/test 面给足时间
    "pwsh": 600_000,
    "bash_main": 600_000,
    "pwsh_main": 600_000,
    "python_script": 120_000,  # 中档 —— 低于 STREAMING_ZOMBIE_TIMEOUT_MS(300s)
    # 留足余量：静默跑满 300s 的脚本不会与 streaming-zombie 同刻被误判中断。
    "run_command": 120_000,    # 中档：显式 cwd 逃生口保持默认 120s
}
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

# B-1 P1-1 沙箱可写锚点（M1 死因的守补位）：受限 shell 的 $env:TEMP 已被沙箱
# 改写到工作区内 **agent 私有可写** 目录 .hiveweave/sandbox-temp/<agent_id>。
# 此文案注入 bash/pwsh 工具 description（系统提示），让代理知道有可写私有
# TEMP。P0（2026-09-05）：pytest tmp_path/--basetemp 在锚点下**原生可用**
# （平台注入 shim 修掉 CPython>=3.12 的 mode=0o700 → OWNER_RIGHTS 死岛，
# 见 acl_sandbox/temppatch.py），话术从「手动指 --basetemp」改为「共享缓存
# 墙才是要绕的」+ 失败自救路径（fresh 子目录 + 上报）。
SANDBOX_TEMP_GUIDE = (
    "\n\n[B-1 Sandbox anchor] On Windows the ACL sandbox redirects $env:TEMP / "
    "$env:TMP to YOUR private writable dir: `<workspace>/.hiveweave/"
    "sandbox-temp/<your_agent_id>`. Use it for scratch files, temp caches and "
    "runner temp; pytest tmp_path/--basetemp under this dir works natively. "
    "When pytest/vitest/npm runs hit \"Access is denied\"/\"Permission denied\" "
    "on a SHARED cache in the workspace (.pytest_cache/__pycache__/"
    "node_modules/.cache), disable the shared cache instead: pytest "
    "-p no:cacheprovider, or vitest --cache-dir=$env:TEMP/vitest-cache. If "
    "writes under your own TEMP ever fail with Access denied, point the "
    "runner at a FRESH subdir of $env:TEMP (e.g. pytest "
    "\"--basetemp=$env:TEMP/pt-<run>\") and report the failure — never retry "
    "the same reused temp directory."
)

# B-1 P1-1 ②：测试类命令 + 权限类失败 → 追加"可写锚点"hint（bash.py 失败输出增强）。
_TEST_CMD_RE = re.compile(
    r"\b(?:pytest|py\.test|vitest|jest|mocha|node\s+test|node\s+--test|"
    r"(?:npm|pnpm|yarn)\s+test|go\s+test|cargo\s+test|"
    r"mvn\s+test|gradle\s+test|dotnet\s+test)\b",
    re.IGNORECASE,
)
_ACCESS_DENIED_RE = re.compile(
    r"(?:access\s+is\s+denied|access\s+to\s+the\s+path|permission\s+denied|"
    r"eacces|denied|not\s+writable|could\s+not\s+(?:create|write)|"
    # F1（#18 审计后半，2026-09-16）：**受限沙箱里 EPERM 的主频是文件锁/共享缓存
    # unlink**（仓内 46 分钟税先例）—— 而那类失败的正确处方正是本提示（换 cache
    # 目录 / 换 fresh temp）。此前不认 EPERM ⇒ 那批连锚点提示都拿不到。
    r"EPERM|operation\s+not\s+permitted)",
    re.IGNORECASE,
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
    from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

    command = _strip_trailing_ampersand(command)
    pid = project_id or "default"
    # 阻塞调用（netstat 快照 / taskkill）统一下放线程池，避免卡住事件循环
    await asyncio.to_thread(prune_dead_processes)
    # ⚠️ 保留端口的**权威守卫不在这里**（2026-09-11 实测修正）。
    #
    # 曾经此处有两处 `if is_reserved_port(...)` 守卫（本处 + 分配后复查），
    # 但它们**恒不可达**：`preferred` 在 hint 保留时回落到 3000，而 3000
    # 永不保留（`RESERVED_PORTS = {4000, 4173, 5173}`）；随后
    # `prepare_spawn_command` 会再次改写端口。已删除，避免"看似有守卫"
    # 的假安全感。
    #
    # 真正的拦截点（含 `fact=bad_args` 归因）：
    # - `process_registry.check_command_reserved_ports`（命令文本直检，
    #   经 `prepare_spawn_command:803` 返回 `prep_err`）——
    #   `start_dev_server` / `run_command` 入口在 bash.py:2958 / :3256 消费；
    #   本函数的 `prep_err` 分支（:354）也会按证据归类。
    # 回归直测见 `tests/test_fact_positions_coverage.py::TestDevServerGuardsDirect`。
    preferred = (
        port_hint
        if (port_hint and not is_reserved_port(port_hint))
        else 3000
    )

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

    # 同理，此处原先的 `if is_reserved_port(port)` 守卫也**恒不可达**：
    # `allocate_project_port` / `lookup_by_port` 只产出项目端口（3000+），
    # 不可能返回 RESERVED_PORTS 里的值。已删除（保留端口在更早的
    # `prepare_spawn_command` 就被拦下，见上方注释）。

    try:
        # #1 治本：dev server 的 spawn 走**唯一入口** —— 判定/路由/盖戳都由
        # `entry.spawn_agent_command` 做，本函数只提供两条实现。改造前这里是
        # `if acl_sandbox_active():` 自己判，且与 `dev_server_tools.py` 的同类
        # 功能各写一份（"同一功能两条路"正是本条根因）。
        from hiveweave.services.acl_sandbox.entry import spawn_agent_command
        from hiveweave.services.acl_sandbox.integration import build_confined_argv
        from hiveweave.services.acl_sandbox.service import spawn_confined

        # 受限侧要在 spawn 前准备命令（注入端口 / 检保留端口）。准备结果用局部
        # holder 带回来 —— 不塞进 spawn 结果里是为了不污染它的形状（同一份
        # 结果还要被 `_ConfinedDevProc` 与 process_registry 注册消费）。
        prepared: dict[str, Any] = {}

        async def _native_spawn():
            return spawn_project_process(
                command, cwd=cwd, project_id=project_id, preferred_port=port
            )

        async def _confined(ctx) -> dict[str, Any] | None:
            # E10：传 argv（逐元素引用，修剥引号根因）。
            cmd2, extra_env, prep_err, _inj_meta = prepare_spawn_command(
                command, project_id=project_id, preferred_port=port
            )
            if prep_err:
                # F4/L3 → L6 修正（2026-09-11）：`prepare_spawn_command` 的
                # prep_err **不只有 spawn 自身故障** —— 它内部（process_registry
                # :803）还兜着「命令文本里出现保留端口」的直检，那一条是**调用方
                # 参数错**（换 3000+ 即可）。原先此处无条件声明 runner_failed，
                # 会让 agent 收到「不是你的 bug」而原地重撞同一端口。
                #
                # 判据改为**按证据归类**：签名表命中哪格就是哪格（与
                # `classify_blocked_fact` 同源，顺序也是先 runner 再 bad_args）；
                # 只有两侧都不命中时才落回「spawn 准备失败 = 命令从未执行」
                # 的默认格，且此时是**真·runner 故障**。
                _prep_fact = classify_error_text(prep_err) or "runner_failed"
                # 仍经唯一漏斗收口（`finalize_fact_dict` 幂等；调用方拿到后
                # 也会再收一次 —— 不能因为"外面会收"就在出口裸奔字典）。
                #
                # ⚠ F5（2026-09-17 审计必修 BLOCKING，本批第二处同族落点）：
                # **必须显式声明 `executed=False`** —— 本分支同样「命令根本没
                # 启动」，而它返回的是**普通 dict**（非 None）⇒ `entry` 的
                # `result is not None` 成立 ⇒ `_mark_executed` 按「函数返回了
                # ⇒ 启动过」**补 `executed=True`**。实测复刻（审计探针 K）：
                #     result['executed'] = True
                #     stamp() = {'enforcement': 'confined', …, 'executed': True}
                # ⇒ F5 的病（戳说"在沙箱里"而进程从未启动）**在这条路上原样
                # 复发**，且因为 `executed is True`，连 `streaming.py` 的
                # fail-loud 告警（条件 `is False`）都不触发 —— **谎报且无声**。
                #
                # ⚠ 与 `fact` 的关系是**正交**、不冲突：`fact` 管「**成因**是
                # 哪一类」（runner 故障 / 调用方参数错），`executed` 管
                # 「**进程有没有起来**」。两类的答案都是"没起来" —— 保留
                # 端口是参数错（`bad_args`）不影响这个事实。故此处无条件写
                # `False`，**不改 `fact`**（改判 `fact` 是另一码事，见本仓
                # 「构造器不变式」那条教训）。
                return finalize_fact_dict({
                    "success": False, "output": "",
                    "error": prep_err, "blocked": True,
                    "fact": _prep_fact,
                    "executed": False,
                })
            prepared["command"] = cmd2
            prepared["env_port"] = extra_env.get("PORT") or extra_env.get("VITE_PORT")
            return await spawn_confined(
                argv=build_confined_argv(cmd2),
                long_running=True,
                env_extra=extra_env,
                **ctx.confined_kwargs(),
            )

        routed = await spawn_agent_command(
            entry="dev_server",
            agent_id=agent_id or "unknown",
            workspace_path=cwd,
            workdir=cwd,
            project_id=project_id,
            confined=_confined,
            native=_native_spawn,
        )
        sres = routed.result
        if routed.native:
            proc, spawn_err, meta = sres
        elif sres.get("long_running"):
            proc = _ConfinedDevProc(sres["job"])
            spawn_err = None
            meta = {
                "command": prepared["command"],
                "cwd": cwd,
                "pid": proc.pid,
                "env_port": prepared["env_port"],
            }
        else:
            # 受限侧在 spawn 之前就失败（prep_err）—— 事实位已按证据归类，
            # 原样回执，不重新组装。
            return finalize_fact_dict(sres)
    except SandboxUnavailableError as e:
        # fail-closed：沙箱不可用 → 直接干净拒绝，不重复 spawn / 不落原生。
        # F5：带上 `executed` 执行面事实（异常由 `spawn_agent_command` 打上
        # `executed=False` = 命令从未启动）—— 只有「被拒绝」而没有「没跑过」
        # 这两个正交事实，下游会把 confined 读成"在沙箱里跑过"。
        #
        # ⚠⚠ 2026-09-17 第二轮审计必修（与 `dev_server_tools` 同族）：
        # `SandboxUnavailableError` 是**一切异常的容器** ——
        # `service.py:1105-1107` 把意外异常（含真代码 bug）都包成它
        # ⇒ `fact` **不能**由类型硬编码推出（旧形态恒判 `runner_failed`，
        # 会把 `TypeError` 之类的真缺陷说成"平台故障"，让 agent 放弃自查）。
        # ⇒ 按证据表态：`errors.is_platform_side` 查异常链里的 Win32/pwsh 亲笔签名。
        log.warning(
            "bash.dev_server_sandbox_unavailable",
            error=str(e), command=command[:120], cwd=cwd[:120],
        )
        from hiveweave.services.acl_sandbox.errors import is_platform_side

        return e.to_tool_dict(
            platform_side=is_platform_side(e), **_executed_stamp(e)
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
        # #19（收紧后的 AST 网扫出的**第三处**："助手作为值传递"形态 ——
        # `to_thread(hidden_run, ["git", …])`）。改走接信任锚的 `_git`；
        # ⚠ 不传 project_root：`cwd` 可能是 agent 的 worktree。
        from hiveweave.services.git_worktree.git_cmd import _git as _anchored_git

        _ok, _out = await _anchored_git(
            ["rev-parse", "--short", "HEAD"], cwd, timeout=5,
        )
        if _ok:
            commit = (_out or "").strip()
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
            return finalize_fact_dict({
                "success": False, "output": "",
                "error": (
                    f"Refusing reserved LISTEN port(s) {raw_listen} "
                    f"(pid={proc.pid}). Use a project port (3000+)."
                ),
                "blocked": True,
                "fact": "runner_failed",
            })

    if is_reserved_port(registered_port):
        await asyncio.to_thread(terminate_spawned, proc)
        return finalize_fact_dict({
            "success": False, "output": "",
            "error": (
                f"Refusing to register reserved platform port "
                f"{registered_port}."
            ),
            "blocked": True,
            "fact": "runner_failed",
        })

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
        return finalize_fact_dict({
            "success": False, "output": "",
            "error": f"Failed to register dev server: {e}",
            "blocked": True,
            "fact": "runner_failed",
        })

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


def _maybe_append_test_anchor_hint(command: str, error_msg: str) -> str:
    """B-1 P1-1 ②：测试类命令 + 权限类失败 → 追加一次"可写锚点"hint。

    命中（是测试运行器命令 && 失败输出带权限/拒绝类特征）才追加；未命中
    原样返回（fail-open，零扰动）。P0（2026-09-05）后锚点下 pytest tmp_path
    原生可用，hint 聚焦两类残余墙：复用旧 temp 目录（fresh 子目录自救）与
    工作区共享缓存（no:cacheprovider / cache-dir）。
    """
    if not command or not error_msg:
        return error_msg
    if not _TEST_CMD_RE.search(command):
        return error_msg
    if not _ACCESS_DENIED_RE.search(error_msg):
        return error_msg
    return error_msg + (
        "\n\n[Sandbox anchor hint] This test command likely hit a permission "
        "wall. Your $env:TEMP points to a private writable dir "
        "`<workspace>/.hiveweave/sandbox-temp/<agent_id>` — pytest tmp_path "
        "works there natively; if a REUSED temp dir still denies access, use "
        "a fresh subdir: pytest \"--basetemp=$env:TEMP/pt-<run>\". For shared "
        "caches in the workspace (.pytest_cache/__pycache__/.cache), disable "
        "them instead: -p no:cacheprovider, or vitest "
        "--cache-dir=$env:TEMP/vitest-cache."
    )


# ── #18（2026-09-16）：node 测试运行器撞**沙箱的管道边界** ⇒ 给可用姿势 ──
#
# 实测（本机 node v22.22.2，受限令牌；复现装置见
# `tests/test_acl_sandbox_grandchild_spawn.py`，与 DSH 的同名测试逐点同形）：
#   三态孙进程 spawn：`inherit` OK / `ignore` OK / **`pipe` DENIED (EPERM)**；
#   `node --test` → exit 1 + `error: 'spawn EPERM'`（堆栈落在 `ChildProcess.spawn`）；
#   `node --test --experimental-test-isolation=none` → **TAP 全绿**（同进程 ⇒ 无子进程、无管道）。
#
# 机制（与 DSH 的结论一致，属 WRITE_RESTRICTED 令牌的**固有边界**）：
#   libuv 的 pipe-stdio 用**命名**管道，其默认安全描述符来自 Win32 用户态默认 SD
#   模板（Everyone/ANONYMOUS 只读），**不是**令牌默认 DACL ⇒ 客户端开写时没有任何
#   restricting SID 被授权 ⇒ `ERROR_ACCESS_DENIED`，**以 spawn EPERM 呈现**。
#   ⇒ **给管道 DACL 授受限 SID 治不了它** —— 我们 #18 原定的那个修法方向据此作废。
#
# ⚠ 这是**失败提示（advisory）**，不是门禁：判据只用两个**观测**事实
#   （命令里有 node 测试运行器 + 输出里有 EPERM），不推断意图、不 gate 任何东西。
_NODE_TEST_CMD_RE = re.compile(
    # `node --test` / `node -test`，**加词尾边界**（否则 `node x --test-mode` 误命中）；
    # 以及**转发到 node --test 的常见入口**（`npm test` / `pnpm test` / `yarn test`）
    # —— 它们撞的是同一条边界，之前**一条提示都没有**。
    r"\bnode\b[^\n|;&]*\s--?test(?![\w-])"
    r"|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b",
    re.IGNORECASE,
)
#: ⚠ **必须同时看到 spawn 证据**（F1，审计实测的误诊）：受限沙箱里 `EPERM` 的**主频**
#: 是"文件锁 / 共享缓存 unlink"那类（仓内有 **46 分钟税**的先例：
#: `acl_sandbox/service.py` 的 npm 互锁、`tests/test_acl_sandbox_env.py` 的 TEST_DSH_35），
#: 而那类 EPERM 的出路是**换 cache 目录 / 换 fresh temp**（锚点提示），
#: **不是**关测试隔离。只认裸 `\bEPERM\b` 会把药方给错、还把锚点提示挤掉。
_SPAWN_EPERM_RE = re.compile(
    r"spawn\w*[\s\S]{0,80}?EPERM"
    r"|EPERM[\s\S]{0,80}?(?:spawn|child_process)"
    r"|operation\s+not\s+permitted[\s\S]{0,80}?spawn",
    re.IGNORECASE,
)
NODE_TEST_ISOLATION_NOTE = (
    "\n\n[Sandbox pipe boundary] A child-process spawn with **piped** stdio "
    "cannot work under this Windows restricted token — that is a platform "
    "boundary, not a defect in your code. `node --test` hits it because the "
    "runner spawns one child per test file and reads TAP over a pipe. "
    "Fix: run the test files **in-process** — `node --test "
    "--experimental-test-isolation=none <files>`. That flag name is "
    "**version-dependent**: if you get `bad option`, retry with "
    "`--test-isolation=none` (check with `node --help` which one exists) — "
    "`bad option` is NOT a new failure. "
    "Two caveats: (1) isolation-off means all files share one process, so a "
    "green run is **weaker evidence** (cross-file global state, one crash "
    "takes the run down) — use it only because the default form cannot run "
    "here; (2) this removes the **runner's own** pipes; if your tests "
    "themselves spawn children with `stdio: 'pipe'`, those still fail — "
    "switch them to `stdio: 'inherit'` or write results to files."
)


def _maybe_append_node_isolation_hint(command: str, error_msg: str) -> str:
    """node 测试运行器 + EPERM ⇒ 追加"改用同进程隔离"的可操作提示。

    为什么值得单独一条：撞墙的 agent 第一反应是**换 flag 重试**（本仓实测过
    "19 分钟自救马拉松"那类），而这条边界的 flag 空间里**只有隔离开关**能绕开
    ⇒ 不指路就是让它白烧轮次。
    """
    if not command or not error_msg:
        return error_msg
    if not _NODE_TEST_CMD_RE.search(command):
        return error_msg
    if not _SPAWN_EPERM_RE.search(error_msg):
        return error_msg
    return error_msg + NODE_TEST_ISOLATION_NOTE


def _maybe_append_test_hints(command: str, error_msg: str) -> str:
    """测试类失败的提示**唯一链**（顺序：可写锚点 → node 管道边界）。

    ⚠ 链只能有一份：两个调用点（`execute_bash` / `run_command` 的错误出口）都走
    这里 —— 否则加第三条提示时又会长成"每处各列一份清单"（本仓在事实位白名单上
    栽过两次）。
    """
    return _maybe_append_node_isolation_hint(
        command, _maybe_append_test_anchor_hint(command, error_msg)
    )


#: shell 工具结果里**必须**透传到 ToolResult 的事实位/观测键 —— **单一清单**。
#:
#: ⚠ 两处出口（`_shell_tool_impl` 与 `run_command_tool`）曾**各列一份**，而
#: `dialect_failed` 只在其中一份里 ⇒ `run_command` 的方言门失败会退化成通用
#: "命令未运行（执行器/方言/权限/审批）"文案，而生产者
#: （`execute_run_command` → `bash.py` 的 `"dialect_failed": True`）明明写了位。
#: 这正是本仓自陈的复发形态（"每处各列一份清单"，事实位白名单上栽过两次），
#: 故抽成常量：**新增位只改这里**（审计 T1，2026-09-16）。
#:
#: ⚠⚠ 2026-09-17 补正：上面那句"只改这里"当时**不成立**，因为 spawn 面戳
#: （`enforcement*` / `git_hardened`）还有**第二个登记点**
#: （`tools/result.py::ToolResult.to_dict` 的透传白名单）。而 0-3 只把
#: `git_hardened` 加进了**本清单**、没加进 `to_dict` 那份；`enforcement*`
#: 则**两份都没有** —— 于是本清单就是 `enforcement` 的**唯一丢失点**：
#: 八处出口都正确调用了 `_enforcement_stamp(result)` 并把戳展开进裸 dict，
#: 但 `_shell_tool_impl` / `run_command_tool` 用它做 `_ff` 过滤时把 4 个键
#: 全滤掉 ⇒ 最终 ToolResult 里没有该键 ⇒ `streaming.py` 取到 None ⇒
#: `run_steps.enforcement` 恒 NULL。**实证**：58/59/60/61 共 4863 行
#: run_steps 零落库；而 `git_hardened`（已登记进本清单）在 61 有 86 条
#: （工具分布 pwsh 70 / pwsh_main 14 / run_command 2 —— 恰好是走 shell 这条路的）。
#: ⇒ 修法不是"再补一次词表"（那正是本仓反复栽的形态），而是把
#: `ALL_SPAWN_STAMP_KEYS` 当**唯一登记点**并进本清单，让新增一种 spawn 面事实
#: 只需要在 `policy.py` 登记一次。
#:
#: ⚠ 2026-09-17 二次补正（F5）：登记点当时仍**不完整** —— `SPAWN_STAMP_KEYS`
#: 只覆盖「决策面 + 加固面」，而「命令到底有没有启动」（执行面 `executed`）
#: 谁都不管。于是 `enforcement="confined"` 在"沙箱判定成立、而 pwsh 缺失
#: 导致进程根本没起来"时照样宣告，回执说"被约束"、事实是"没有进程"。
#: ⇒ 登记点扩为 `ALL_SPAWN_STAMP_KEYS`（+ 执行面 1 键），仍只登记一处。
_SHELL_FACT_FLAG_KEYS: tuple[str, ...] = (
    "fact", "runner_failed", "command_failed", "injection_applied",
    "timeout_kind", "timeout_ms", "dialect_failed",
    # 0-3 + #1 + F5：spawn 面戳（决策面 4 键 + 加固面 git_hardened +
    # 执行面 executed）。从 policy 派生而非再列一遍 —— 见上方补正。
    *ALL_SPAWN_STAMP_KEYS,
)


def _enforcement_stamp(result: dict) -> dict[str, Any]:
    """把唯一入口盖的 spawn 面戳原样搬到最终结果（键名以 policy 为准，不另起名）。

    用 `ALL_SPAWN_STAMP_KEYS`（= 决策面 4 键 + 加固面 `git_hardened` + 执行面
    `executed`）：新增一种 spawn 面事实只在 policy 那里登记一次 —— 这里的
    `if k in result` 决定「没这条信息」时**不补默认值**（NULL/缺键 ≠ 说了否，
    与 run_steps 的 `COALESCE` 语义对齐）。

    ⚠ 本函数产出的戳**必须同时**落在 `_SHELL_FACT_FLAG_KEYS` 里才到得了
    消费端（`run_steps.enforcement`）—— 二者现已同源（都派生自
    `ALL_SPAWN_STAMP_KEYS`），见该常量的 2026-09-17 补正。
    """
    return {k: result[k] for k in ALL_SPAWN_STAMP_KEYS if k in result}


def _executed_stamp(exc: BaseException) -> dict[str, Any]:
    """F5：从**异常**上取「命令从未启动」这个事实位（缺属性 ⇒ 空，不猜）。

    与 `_enforcement_stamp` 的分工：那个从**结果 dict** 里取（执行已经发生），
    这个从**异常**上取（执行没有发生）。两条路都要能把「有没有进程」上报，
    否则 `enforcement="confined"` 会被下游一律读成"在沙箱里跑过"。

    ⚠ 不改异常类型/文案 —— 本函数只读属性，异常往上抛的那条契约一字不动。
    """
    value = getattr(exc, "executed", None)
    return {"executed": value} if value is not None else {}


def _native_shaped(result: dict) -> dict[str, Any]:
    """把 spawn_confined 的 {exit_code,stdout,stderr,timed_out} 归一为 native 形态。

    ⚠ 两件事必须**保留**，归一化不该把它们抹掉：
      · `enforcement*` 戳 —— 「这次到底有没有沙箱」是 #1 要让人看得见的事实，
        在归一化处丢掉等于又回到"静默默认值"；
      · `error` —— 受限 shell 不可用（pwsh 缺失）等**在受限侧就失败**的情形，
        归一化把它抹成 None 会让失败变成"空输出成功"。
    """
    stdout = result.get("stdout", "") or ""
    stderr = result.get("stderr", "") or ""
    combined = stdout + ("\n" + stderr if stdout and stderr else stderr)
    out: dict[str, Any] = {
        "output": combined,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out", False)),
        "error": result.get("error"),
    }
    # 构造点声明的 fact 必须随归一化活下来（M2，2026-09-16）：spawn 失败那两条
    # 出口现在显式声明 `runner_failed`，而本函数是**重建**一个新 dict ⇒
    # 不带过来就等于位又被这一层悄悄吃掉（本仓在"归一化抹掉事实位"上栽过）。
    if result.get("fact") is not None:
        out["fact"] = result["fact"]
    out.update(_enforcement_stamp(result))
    return out


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
    dialect: str = "bash",
    decision=None,
) -> dict[str, Any]:
    """经**唯一入口**（`entry.spawn_agent_command`）执行并回传执行面戳。

    与改造前的区别（#1 治本）：本函数**不再自己判沙箱、也不再返回 None**。
    此前它读 `acl_sandbox_active()` 自判，返回 None 让**每个调用方各自**回落
    native —— 同一判定被五处各自解释，其中 `python_script` 把"判定为原生"
    读成了"沙箱坏"并拒绝执行。现在判定与回落都在入口内部，调用方拿到的
    永远是结果本体（含 `enforcement*`）。
    """
    from hiveweave.services.acl_sandbox.entry import spawn_agent_command
    from hiveweave.services.acl_sandbox.integration import (
        PwshUnavailableError,
        build_confined_argv,
    )
    from hiveweave.services.acl_sandbox.service import spawn_confined

    async def _native() -> dict[str, Any]:
        return await _run_native(command, cwd, int(timeout_s or 0), dialect=dialect)

    async def _confined(ctx) -> dict[str, Any] | None:
        try:
            argv = build_confined_argv(command, dialect=dialect)
        except PwshUnavailableError as exc:
            # 受限 shell 不可用 ⇒ 可操作错误（≠「沙箱没开」，后者会走 native）。
            # ⚠ F5（2026-09-17）：返回**普通 dict 而非 None** ⇒ `entry` 照常盖
            # `enforcement="confined"`，而进程根本**没启动**（exit_code=None）
            # ⇒ 戳说"在沙箱里"、事实是"没有进程"。本批只加观测（不改失败形态），
            # 故显式声明 `executed=False` 让两个事实并排存在。
            #
            # ⚠⚠ 必须经 `finalize_fact_dict` 收口（AST 守卫
            # `test_bash_dict_returns_with_fact_go_through_funnel` 把关）：
            # 裸字典声明 `fact` 而不展开派生键 ⇒ 派生键**缺失**。
            #
            # ⚠ 真实后果是**静默误归因**，不是崩溃（2026-09-17 第三轮审计
            # 更正：早先五处写作 "下游 `result['runner_failed']` 直接 KeyError"
            # 是**误述** —— 实际消费者全用 `.get()`，KeyError 在仓库内不可
            # 复现）。实测差别：
            #   · 漏斗后：`{'runner_failed': True, ...}` ⇒ `tool_loop.py:1388`
            #     的 `if _rf.get("runner_failed")` 成立 ⇒ agent 得到「命令未执行」
            #     的归因提示；
            #   · 裸字典：`{'runner_failed': False, ...}` ⇒ 该提示**不发**，
            #     且 `streaming.py:340/420` 落库 `None`（"未判定"）而非
            #     `False`（"已判定为非"）—— 与 `result.py` 的 COALESCE 语义冲突。
            # 本处是**本批（F5）新引入**的裸字典出口 ——
            # 原实现（返回 None）没有这个问题，加 `fact` 时才需要漏斗。
            return finalize_fact_dict({
                "output": "", "stdout": "", "stderr": "",
                "exit_code": None, "timed_out": False, "error": str(exc),
                "executed": False, "fact": "runner_failed",
            })
        return await spawn_confined(
            argv=argv,
            timeout_s=timeout_s or 0,
            long_running=long_running,
            env_extra=env_extra,
            **ctx.confined_kwargs(),
        )

    routed = await spawn_agent_command(
        entry=entry,
        agent_id=agent_id or "unknown",
        workspace_path=workspace_path,
        workdir=cwd,
        project_id=project_id,
        confined=_confined,
        native=_native,
        decision=decision,
    )
    result = routed.result
    if result.get("long_running"):
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

    def poll(self) -> int | None:
        """Popen-like 存活探测：仍在跑 ⇒ ``None``；已退出 ⇒ ``0``。

        ⚠ 为什么补在这里而不是让调用方各写一份：`dev_server_tools` 的
        `start_dev_server` 需要 `poll()`（判"起来后是否立刻退出"），而 bash 路不需要
        —— 于是本 shim 此前没有它。**但同一个 shim 出现两份就会各自演化**
        （这正是 #1 的根因：同一个 dev-server 功能两条路各写一份 spawn）。
        ⇒ 需求差异用**加方法**吸收，不复制类型。

        退出码不可得（沙箱 job 只给存活位）⇒ 已退出一律返回 ``0``；调用方只判
        `is not None`（"是否已退出"），不要拿这个 ``0`` 当"成功"。
        """
        try:
            return 0 if self.job.is_exited() else None
        except Exception:  # noqa: BLE001 — 探测失败按"仍在跑"处理，不误杀
            return None

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
# 2026-09-01 补 PowerShell cmdlet：本平台强制走 pwsh（unix-only 一律拒绝），
# 而原表只有 POSIX/DOS 动词 —— 于是 `Remove-Item .hiveweave/data.db`、
# `Out-File .hiveweave/data.db` 这类用平台指定方言写的破坏命令**整条绕过**
# 护栏（实测确认）。方言既然是强制的，护栏就必须覆盖该方言的动词。
# 只读 cmdlet（Get-Content / Get-ChildItem / Select-String）**不**入表，
# 否则 .hiveweave/logs 的只读放行会被自己关掉。
_HIVEWEAVE_FILE_OPS = re.compile(
    r"\b(?:rm|del|erase|cat|type|cp|copy|mv|move|xcopy|robocopy|"
    r"echo|printf|tee|dd|truncate|strings|xxd|hexdump|od|base64|"
    r"touch|mkdir|rmdir|rd|ln|link|chmod|chown|attrib|cacls|"
    r"sqlite3|\.sqlite3|open|export|tar|zip|unzip|gzip|gunzip|"
    r"7z|rar|dump|backup|restore|import|load|"
    r"remove-item|ri\b|erase-item|clear-content|clc|"
    r"out-file|set-content|sc\b|add-content|ac\b|new-item|ni\b|"
    r"move-item|mi\b|rename-item|ren\b|rni|copy-item|ci\b|cpi|"
    r"tee-object|export-clixml|import-clixml)\b",
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
# ⚠ 两份清单的一致性**由测试兜底**（不是靠这段注释）：
# tests/test_hiveweave_dir_protection.py::TestHiveweaveAllowlistConsistency
# 逐子目录断言 file.py 与 bash.py 判定一致 —— 只改一处即转红。
# 另有**只读**子目录（不在本清单内，见下方 logs / merge-quarantine 两个例外）。
# (?![\w.-]) 精确拒绝「路径名续字符」：放行 `git -C .hiveweave/worktrees`（尾随空格/结尾），
# 拦 `.hiveweave/shared-evil/`、`.hiveweave/worktrees2/` 这类前缀目录（\b 会被 d- / s2 击穿）
_ALLOWED_HW_SUBDIRS = re.compile(
    r"\.hiveweave[\\/]+(?:shared|reports|drafts|worktrees|handoffs|sandbox-temp)(?![\w.-])",
    re.IGNORECASE,
)

# s3-clone_06 P1-7：`.hiveweave/logs` 是 **只读** 例外——agent 唯一的诊断
# 出口（dev-server 崩没崩只能看这里）。此前被一刀切禁触，霁岚/汐然/栖迟
# 三人共撞 8 次，每次都拿不到失败原因，只能盲重试 start_dev_server。
# 仅放行「不产生写入」的读取：任何重定向 / 删除 / 复制 / 写入动词仍拦。
_HW_LOGS_REF = re.compile(
    r"\.hiveweave[\\/]+logs(?![\w.-])", re.IGNORECASE
)
# fd 复制（`2>&1` / `1>&2`：`>` 后紧跟 `&`）不是写入，必须排除——否则无害的
# stderr 合并也会被当成写操作，把 `cat .hiveweave/logs/a.log 2>&1 | head` 拦掉。
# 真正的写重定向（`> file` / `>> file`）后跟的不是 `&`，照常命中。
# 2026-09-12（审计）：补 `dd` / `ln` / `sqlite3` —— 三者在 `_HIVEWEAVE_FILE_OPS`
# 里（会触发 .hiveweave 门），却不在本写入词表里，于是 `dd of=.hiveweave/logs/x`
# 或 `dd of=.hiveweave/merge-quarantine/x` 会被下方的**只读例外**放行。只读例外
# 是本文件里唯一"靠写入词表来证明是读"的地方，所以词表漏一个动词 = 该目录的写
# 也漏了。宁可多拦（误拦一条含裸 `ln` 的读命令）也不能少拦。
_HW_WRITE_MARKERS = re.compile(
    r"(?:>>|(?<![<=!>-])>)(?!&)"
    r"|\b(?:rm|del|erase|rmdir|rd|mv|move|cp|copy|"
    r"xcopy|robocopy|echo|tee|truncate|mkdir|touch|"
    r"dd|ln|sqlite3|"
    r"remove-item|clear-content|out-file|set-content|add-content|new-item|"
    r"move-item|rename-item|copy-item|tee-object|"
    # 别名必须与 _HIVEWEAVE_FILE_OPS 对齐（审计 [3]）：`… | ri` 能命中
    # FILE_OPS + LOGS_REF，若此处缺别名就会被只读门放行而真删除。
    r"ri|mi|cpi)\b",
    re.IGNORECASE,
)

# report TEST_DSH_54 #5（v2 收窄版）：`merge-quarantine` 是平台自管的隔离区
# —— 平台在 `services/platform_state.py` T2.5 主动回报"有隔离文件待处理"，
# 却因该目录不在 `_ALLOWED_HW_SUBDIRS` 里而拒绝 agent 读取（实测 18/18 次
# 拒绝全部指向它）。与 `logs` 同形态处理：**只读放行**（诊断需要），
# 任何写入/删除/搬移动词仍拦（隔离区不许 agent 改写）。
_HW_MERGE_QUARANTINE_REF = re.compile(
    r"\.hiveweave[\\/]+merge-quarantine(?![\w.-])", re.IGNORECASE
)


def _check_hiveweave_command(command: str) -> bool:
    """Return True if the command targets `.hiveweave` with a file operation.

    拦截 agent 通过 bash 读写/删除/复制 .hiveweave 内系统文件（data.db 等）。
    `cd .hiveweave` 和 `ls .hiveweave` 这类无害命令不拦。
    放行指向 shared/reports/drafts/worktrees/handoffs 子目录的文件操作（团队共享/工作文件）。
    放行 .hiveweave/logs 下的**只读**操作（诊断出口，P1-7）；写/删仍拦。
    放行 .hiveweave/merge-quarantine 下的**只读**操作（隔离区诊断，TEST_DSH_54 #5）；
    写/删仍拦 —— 该目录由平台自管，不是 agent 工作目录。
    """
    command = _strip_hiveweave_test_excludes(command)
    if not _HIVEWEAVE_REF.search(command):
        return False
    if not _HIVEWEAVE_FILE_OPS.search(command):
        return False
    # 放行明确指向允许子目录的操作
    if _ALLOWED_HW_SUBDIRS.search(command):
        return False
    # 只读诊断日志：无写入标记才放行
    if _HW_LOGS_REF.search(command) and not _HW_WRITE_MARKERS.search(command):
        return False
    # 只读隔离区诊断：无写入标记才放行（同 logs 形态）
    if (
        _HW_MERGE_QUARANTINE_REF.search(command)
        and not _HW_WRITE_MARKERS.search(command)
    ):
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
        # #15 补齐：`remove-item` 此前**不在** file_cmds ⇒ PowerShell 删除
        # 命令的目标路径完全不被提取，敏感路径/.hiveweave 护栏对它**静默
        # no-op**（fixplan §6 #15 的「实现坑」）。一并补 PowerShell 别名族。
        file_cmds = {'cat', 'cp', 'mv', 'rm', 'touch', 'mkdir', 'chmod',
                     'chown', 'source', 'head', 'tail', 'less', 'more',
                     # D-5（审计）：`wc` 与 head/tail 同族（都从文件读），漏了它
                     # ⇒ `wc -l .env` 的路径不进敏感路径检查（`head -5 .env` 会进）
                     'wc',
                     'tee', 'dd', 'ln',
                     'remove-item', 'remove_item', 'del', 'erase', 'rd',
                     'rmdir', 'move-item', 'move_item', 'copy-item',
                     'copy_item', 'get-item', 'get_item', 'set-content',
                     'set_content', 'out-file', 'out_file'}
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
    from hiveweave.services.command_guard import degrade_ask

    verdict = _validate_command_safety_verdict(command)
    if verdict.action == "ask":
        # 非交互路径（game_time 定时器等）：ask 降级，文案保持历史形态
        # 「Command blocked: <hint> [ask→deny: …]」
        verdict = degrade_ask(verdict)
        return True, f"Command blocked: {verdict.reason}"
    if verdict.blocked:
        return True, verdict.reason
    return False, ""


def _validate_command_safety_verdict(
    command: str, *, delete_in_boundary: bool = False
) -> "GuardVerdict":
    """统一命令安全校验的 verdict 形式（T2.2）。

    与 :func:`_validate_command_safety` 同一条链，reason 文本与历史完全
    一致；区别仅在 ask 判定**原样上浮**不降级 —— 交互式执行入口
    （execute_bash / execute_run_command / _bash_background）拿到 ask 后走
    ``resolve_ask_with_approval``；非交互路径（game_time 定时器）经兼容
    包装 :func:`_validate_command_safety` 自动降级，行为与历史一致。

    ``delete_in_boundary``（#15）：删除族命令的**授权树落点判定**已在 async
    包装里做完（``resolve_delete_landing_for_agent``），结果为「全在授权树内」
    时置 True —— 此处直接 allow，不再落到 ``rm``/``remove-item`` 的 ask 规则，
    从而自建路径删除**零审批行**。判定与 ``acl_sandbox`` 授权事实同源。
    """
    from hiveweave.services.command_guard import GuardVerdict, evaluate_command

    blocked, reason = check_self_destructive(command)
    if blocked:
        return GuardVerdict(True, "deny", f"Command blocked: {reason}",
                            "__self_destructive__")
    from hiveweave.services.process_registry import check_platform_process_kill

    plat_err = check_platform_process_kill(command)
    if plat_err:
        return GuardVerdict(True, "deny", plat_err, "__platform_process_kill__")
    from hiveweave.tools.security import is_sensitive_path
    # Bug C fix: 只检查命令中的文件路径参数，不检查整个命令字符串
    # 目标型护栏（敏感文件 / .hiveweave）先于命令模式护栏：同一命令多重命中时
    # 报更具体、更可行动的原因（如 rm -rf .hiveweave 报系统目录而非 rm-rf 提示）。
    file_paths = _extract_file_paths_from_command(command)
    for fp in file_paths:
        if is_sensitive_path(fp):
            return GuardVerdict(
                True, "deny",
                (f"Command references a sensitive file: {fp} "
                 f"(e.g. .env, *.pem, id_rsa, credentials). "
                 f"Use read_file with explicit approval instead."),
                "__sensitive_path__",
            )
    if _check_hiveweave_command(command):
        return GuardVerdict(
            True, "deny",
            ("Command targets `.hiveweave` system directory. "
             "System files (data.db, tool_outputs/) are managed by "
             "HiveWeave internals."),
            "__hiveweave_dir__",
        )
    # slack-clone_01 P0: 命令模式护栏（taskkill //IM / rm -rf / pkill …）
    # + 受保护 PID 硬层。ask 判定原样上浮（T2.2），由调用方决定审批或降级。
    # #15：删除族命令的授权树落点判定已在 async 包装里完成；全在授权树内
    # → 直接 allow，跳过 `rm`/`remove-item` 的 ask 规则（自建路径删除零审批行）。
    if delete_in_boundary:
        return GuardVerdict(False, "allow", "", "__delete_in_boundary__")
    verdict = evaluate_command(command)
    if verdict.blocked and verdict.action != "ask":
        # 历史文案：模式护栏拒绝统一加 "Command blocked: " 前缀
        return GuardVerdict(True, "deny",
                            f"Command blocked: {verdict.reason}", verdict.rule)
    return verdict


async def _validate_command_safety_resolved(
    command: str,
    *,
    agent_id: str,
    tool_name: str,
    tool_args: dict | None = None,
    ask_already_resolved: bool = False,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """交互式执行入口用：校验 + ask 在线审批解析（T2.2）。

    返回 (blocked, reason)。ask 判定先走
    :func:`resolve_ask_with_approval`（批准 → 放行 / 拒绝·超时 → 拒绝 /
    通道故障 → 降级）；``ask_already_resolved=True`` 表示上游
    （``_bash_background`` → ``execute_bash`` 链）已解析过同一命令的 ask
    并获批 —— 直接放行，避免二次弹审批。

    #15：删除族命令先做**授权树落点判定**（``resolve_delete_landing_for_agent``）
    —— 全落在 ``boundary_root ∪ temp_dir ∪ extra_dirs`` 内 → allow（自建路径
    自己删，零审批行）；越界 → 直接 deny（fail-closed，**不挂起等审批**）。

    ``cwd``（P0-1 修复）：**命令的实际执行目录**，用于解析相对路径删除目标。
    必须由调用方按 bash 的真实解析口径传（``workspace_path / workdir``，见
    ``execute_bash`` 的 ``cwd`` 计算）—— 否则相对目标会按**后端进程 CWD**
    解析 ⇒ 一律判越界（比不做落点判定更糟）。
    """
    from hiveweave.services.command_guard import (
        resolve_ask_with_approval,
        resolve_delete_landing_for_agent,
    )

    # #15：删除命令落点判定（越界 → deny，不进入 ask 等待）
    landing = await resolve_delete_landing_for_agent(
        command, agent_id=agent_id, cwd=cwd
    )
    if landing is not None and landing.blocked:
        return True, landing.reason
    delete_in_boundary = landing is not None and not landing.blocked

    verdict = _validate_command_safety_verdict(
        command, delete_in_boundary=delete_in_boundary
    )
    if verdict.action == "ask" and not ask_already_resolved:
        verdict = await resolve_ask_with_approval(
            verdict,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_args=tool_args,
        )
        if not verdict.blocked:
            return False, ""
        # 保持历史 ask 文案形态：「Command blocked: <hint> [ask→deny/审批结果]」
        return True, f"Command blocked: {verdict.reason}"
    if verdict.action == "ask":
        # 上游已获批（同一命令同一判定），放行。
        return False, ""
    if verdict.blocked:
        return True, verdict.reason
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
        # 行数不多但单行超长（如 minified JS），按字符截断。A-2 (P1-4) 确认：
        # 1MB 层 2 截断不能吞真实错误尾部 —— 保留 head（前 1MB）的同时以
        # _error_tail 语义补上最后 ERROR_TAIL_BYTES 作为 tail 预览，让失败输出
        # 末尾的真实报错（minified 堆栈末尾）仍可见。
        head = encoded[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
        tail_start = max(MAX_CAPTURE_BYTES, len(encoded) - ERROR_TAIL_BYTES)
        tail = encoded[tail_start:].decode("utf-8", errors="replace")
        return (
            f"{head}\n"
            f"\n... [{len(encoded) - max(MAX_CAPTURE_BYTES, len(encoded) - ERROR_TAIL_BYTES)} bytes omitted; "
            f"output truncated at 1MB, {len(encoded)} bytes total. See tool output file for full content] ...\n\n"
            f"{tail}"
        )
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
    """[退役 P1-3 B 结构解] bash 惯用法 → pwsh 的词典翻译层。

    P1-3（B 结构解，对齐 deepseek-harness"不翻译、原生双工具"）后不再被
    调用：受限 bash 命令 verbatim 交给 pwsh，unix-only 由
    :func:`detect_untranslated_unix` 前置拒绝并给等价。保留 stub 仅为兼容
    引用方测试与回滚审计 —— 生产路径不经过本函数。
    """
    return cmd


# ── unix-only 命令 fast-fail（DSH_33 P0：126 次方言失败 41.9%）─────
# 翻译表只覆盖能 1:1 对上的形态。剩下的**必须**在这里拦住并给等价写法 ——
# 直接丢给 pwsh 只会得到「不是内部或外部命令」，模型无法从中反推方言，
# 于是同一条命令换着 flag 重试到跨 422 分钟不收敛。
#
# 每条建议都在 pwsh 7.6 实测过（见 commit 说明）；不确定的不写建议，
# 只报「pwsh 下不可用」并指向 pwsh 工具。
#
# ⚠ 2026-09-12：两张表已**移出本文件**到 `tools/shell_dialect.py` —— 因为
# `prompts/executor.py` 的方言段此前手抄了一份「禁用清单」且只列 11 项，
# 而两张表合计 67 条（去重 66，`find` 两表都有），两边漂移。
# 现在提示词从同一模块生成。**改词表请改 shell_dialect.py，别在这里加。**
_UNIX_ONLY_HINTS: dict[str, str] = UNIX_ONLY_HINTS

# 类 2：pwsh 有同名别名/同名 exe，但 unix flag 语义对不上 —— 会报参数错误
# 或（更糟）静默做别的事。仅当带 unix 短 flag 时才拦。
_ALIAS_FLAG_HINTS: dict[str, str] = ALIAS_FLAG_HINTS

# unix 短 flag：`-l` / `-rf` / `-9`（kill -9）。长名（`-Force`/`-Recurse`）不算
# —— 那是 pwsh 自己的参数；`--long` 也不算（pwsh cmdlet 不用双横线）。
_UNIX_SHORT_FLAG_RE = re.compile(r"(?<![\w-])-(?!-)(?:[A-Za-z]{1,4}|\d+)(?=\s|$)")

# 类 2 里少数命令的 unix 用法不带 flag（`ps aux`），靠首参数字面量识别。
_ALIAS_BARE_ARG_HINTS: dict[str, tuple[frozenset[str], str]] = {
    "ps": (frozenset({"aux", "ax", "-ef", "auxww"}),
           "Get-Process（ps aux 会把 aux 当进程名）"),
}

# 命令段分隔符：; | || && & 换行。段首 token 才算「命令」。引号内的分隔符
# **不**分段 —— 否则 `git commit -m "fix: parse; sed edge case"` 会被切出
# 一个假的 `sed` 段而误拦（审计实测）。
#
# ⚠️ 单 `&`（后台/PS 分隔符）此前**不切** ⇒ `sleep 1 & tail -f log` 整串的
# head 是 `sleep`，管道尾的 `tail` 永远查不到 —— 这是**切分器的缺陷**，不只
# 影响 unix 检测（任何基于 segments 的判定都踩）。已补切。
#
# 但 `&` 有多个**非**分段语义，必须排除（否则把 `2>&1` / `&>` 切坏）：
#   - `2>&1` / `>&2` ：文件描述符重定向，`&` 紧跟在 `>`/`<` 之后或数字之后；
#   - `&>` / `&>>`   ：bash 的 stdout+stderr 重定向，`&` 后紧跟 `>`；
#   - `&&`           ：已在上方单独处理（逻辑与）。
#   判据：`&` 前一个非空字符是 `>`/`<`，或 `&` 后紧跟 `>`；以及 `&&`。
_AMPERSAND_RE = re.compile(r"[><]&|&[<>]|&&")

# 复合语句关键字：`for … ; do CMD; done` / `if … ; then CMD; fi` 这类切段后，
# 段首 token 是 `do`/`then`/`fi` 等**关键字**而非命令名 ⇒ head-token 判定
# 永远匹配不上 unix 词表（`for i in 1 2; do head -1 f; done` 曾整串漏网）。
# 这些关键字本身不是命令，取 head 时应**跳过**它们继续看下一个 token。
_SHELL_KEYWORDS = frozenset({
    "do", "then", "else", "elif", "fi", "done", "esac", "in",
    "{", "}", "(", ")", "!",
})


def _split_command_segments(command: str) -> list[str]:
    """按未被引号包裹的 ; | || && & 换行 切分命令段（`&` 的 fd 重定向不切）。"""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in (";", "|", "\n", "&"):
            # `&` 只在**不是** fd 重定向（2>&1 / &> out）时才是段分隔符
            if ch == "&":
                prev = command[i - 1] if i > 0 else ""
                nxt = command[i + 1] if i + 1 < n else ""
                if prev in (">", "<") or nxt in (">", "<"):
                    buf.append(ch)
                    i += 1
                    continue
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def _cmd_substitution_bodies(segment: str) -> list[str]:
    """抽出段内 `$( … )` / 反引号命令替换的**内层命令体**（best-effort）。

    为什么必须抽（P2-4.1 根因）：`x=$(head -3 f)` 这类写法，段首是
    `x=$(head` —— 既不是纯 unix-only 命令名，也让 `_segment_head_token`
    把首 token 认成 `-3`。于是**内层真正的病根命令从未被检查**。
    若不抽，要么误归因到「环境变量前缀」（现状 bug），要么直接放行
    （改坏的形态）—— 两条路都是错的。

    只做**平衡括号**的单层扫描，不追求 shell 完整语义：找不到配对就丢弃。
    嵌套 `$( … $( … ) … )` 由深度计数覆盖。
    """
    bodies: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch == "\\":
            i += 2
            continue
        if segment.startswith("$(", i):
            depth = 1
            j = i + 2
            start = j
            while j < n and depth:
                c = segment[j]
                if c == "\\":
                    j += 2
                    continue
                if segment.startswith("$(", j):
                    depth += 1
                    j += 2
                    continue
                if c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth == 0:
                bodies.append(segment[start:j])
                i = j + 1
                continue
            i += 2
            continue
        if ch == "`":
            j = segment.find("`", i + 1)
            if j != -1:
                bodies.append(segment[i + 1 : j])
                i = j + 1
                continue
            i += 1
            continue
        i += 1
    return bodies


def _segment_head_token(segment: str) -> str:
    """取命令段的首个 token（跳过 VAR=val 前缀、前导空白/括号、复合语句关键字）。

    ``for …; do CMD; done`` 切段后 ``do`` 自成一段，首 token 是关键字而非命令名
    ⇒ 必须跳过 ``do``/``then``/``fi`` 等继续看下一个 token，否则整串 unix-only
    命令永远漏网（`for i in 1 2; do head -1 f; done` 是实测漏网形态）。
    """
    seg = segment.strip().lstrip("(").strip()
    while True:
        m = re.match(r"^[A-Za-z_][\w]*=\S*\s+", seg)
        if not m:
            break
        seg = seg[m.end():]
    # 跳过复合语句关键字（可连续，如 `} else {`）
    while True:
        m = re.match(r"^([\w{}()!]+)\s*", seg)
        if not m or m.group(1).lower() not in _SHELL_KEYWORDS:
            break
        seg = seg[m.end():]
        if not seg:
            return ""
    m = re.match(r"^[\"']?([\w./\\:-]+)", seg)
    if not m:
        return ""
    head = m.group(1)
    # 去掉路径前缀与 .exe 后缀：/usr/bin/sed → sed
    head = head.replace("\\", "/").rsplit("/", 1)[-1]
    if head.lower().endswith(".exe"):
        head = head[:-4]
    return head


def detect_untranslated_unix(command: str) -> str | None:
    """残留 unix-only 命令 → 可操作错误文案；干净则 None。

    P1-3（B 结构解）词典翻译退役后，本检测直接作用于**原命令**：unix-only
    / 带 unix flag 的别名命令（``ls -la`` / ``cat -n`` / ``echo -e``）前置
    拒绝。返回的文案给出实测可用的 pwsh 等价写法 + pwsh 工具指引, 把
    「不是内部或外部命令」的困惑型失败变成一次性可纠正的明确指引。
    """
    if not command or not command.strip():
        return None

    # 40 轮 P0-3（方言税 98.2min）：语法层门禁——heredoc（<< / <<<）在 pwsh
    # 是 ParserError，词典层拦不住（它不是命令名）。12 次 heredoc 失败连提示
    # 都收不到。here-string 等价写法直接给出。
    if re.search(r"<<", command):
        return (
            "  << / <<< (bash heredoc) → pwsh 无 heredoc（直接 ParserError）。"
            "用 here-string：@'\n  …多行内容…\n  '@ | python -\n"
            "  或先把内容写临时文件（write_file 工具）再按文件处理。"
        )

    # 45 轮 P0：bash 环境变量前缀 `VAR=val cmd`。pwsh 命令位不认赋值
    # （要 $env:VAR='val'），executor 提示词已写明「必然失败」，但 gate
    # 此前放行 → 白烧一轮才收到 pwsh 原生报错。语法层前置拒绝。
    # 锚定段首 token，段内引号串不受影响（`python -c "a=1"` 首 token
    # 是 python，不命中；$env:/& 开头的 pwsh 原生形式也不命中）。
    #
    # P2-4.1（2026-09-12）：**赋值右侧含命令替换时不得走本分支**。
    # `x=$(head -3 f)` 的真实病根是 `head`（就算改成 `$env:X=$(head -3 f)`
    # 也照样不存在），而旧实现把它归因成「环境变量前缀写法错」并让 agent
    # 改成 `$env:X=…` —— **改完仍然失败**，白烧一轮，正是本 gate 要避免的。
    # 判据：文案指出的原因必须是**真原因**（借 DSH `packages/AGENTS.md:117`
    # 「never silently skip a missing referent」同族思路；DSH 自身不做方言
    # 翻译、连这个 gate 都没有，故只借判据不借实现）。
    # ⇒ 交给下面的 unix-only 分支，由 `_cmd_substitution_bodies` 把内层命令
    #   喂进同一套检测（那段文案给的处方是对的）。
    for segment in _split_command_segments(command):
        m = re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S", segment)
        if not m:
            continue
        if _cmd_substitution_bodies(segment):
            # 右值里有命令替换 → 真病根在**内层**，不在此处归因
            continue
        return (
            "Error: bash environment-prefix idiom fails in pwsh — "
            "assignment is not a command there:\n"
            "  VAR=val cmd → $env:VAR='val'; cmd（或 $env:VAR='val' "
            "换行后再跑命令）\n"
            "Rewrite with $env: assignments, then rerun."
        )

    hits: list[str] = []
    seen: set[str] = set()
    # 先扫命令替换的**内层**：`x=$(head -3 f)` 的病根在内层，而外层的
    # 首 token 是 `-3`（被 `x=$(head` 吃掉），不展开就整条漏网。
    inner_segments: list[str] = []
    for segment in _split_command_segments(command):
        inner_segments.extend(_cmd_substitution_bodies(segment))
    for segment in [*inner_segments, *_split_command_segments(command)]:
        head = _segment_head_token(segment)
        if not head or head in seen:
            continue
        low = head.lower()
        hint = _UNIX_ONLY_HINTS.get(low)
        if hint is None:
            args = segment.strip()[len(head):]
            bare = _ALIAS_BARE_ARG_HINTS.get(low)
            if bare is not None and args.strip().split(" ")[0] in bare[0]:
                hint = bare[1]
            elif low in _ALIAS_FLAG_HINTS and _UNIX_SHORT_FLAG_RE.search(args):
                # 类 2 只在带 unix 短 flag 时才判失败（裸 ls/cat/rm 走 pwsh 别名 OK）
                hint = _ALIAS_FLAG_HINTS[low]
            else:
                continue
        seen.add(head)
        hits.append(f"  {head} → {hint}")
    if not hits:
        return None
    return (
        "Error: unix-only command(s) not available in this shell — "
        "on Windows the sandbox executes bash via pwsh, and these have no "
        "POSIX equivalent there (running them yields "
        "\"not recognized as ... cmdlet\" or wrong results):\n"
        + "\n".join(hits)
        + "\n\nRewrite using the pwsh form above, or call the `pwsh` tool "
        "with PowerShell syntax directly (same permissions as bash). "
        "Do not retry the same unix command with different flags."
    )


# ── 46 轮 #1：封闭集管道尾自动翻译 ─────────────────────────────
# 打地鼠自查：拒答+给等价写法的「教学」路线三度复现（07 翻译层退役→
# 45 补 19 动词→46 `git log｜head` 5 小时照抄），证明模型不消费错误文案。
# 本函数只做**语义无歧义**的 3 种管道尾映射，集合封闭、承诺不再扩：
#   `| head -N` → `| Select-Object -First N`（行语义；-c 字节语义不翻）
#   `| tail -N` → `| Select-Object -Last N`
#   `| wc -l`   → `| Measure-Object -Line`
# 仅当**前段无其他 unix-only** 时才翻译（翻译了中段照样炸的场景维持
# gate 拒绝+教学）。改写命令以 `# [auto-translated from: …]` 尾注
# 保留原文（tool_args 展示/日志可核对），成功回执不另附标注。
_CLOSED_PIPE_TAIL_RE = re.compile(
    r"(?P<tail>\|\s*(?:head|tail)(?:\s+-n?\s*|\s+)(?P<n>\d+)\s*"
    r"|\|\s*wc\s+-l\s*)$"
)


def try_closed_pipe_translation(command: str) -> tuple[str, str] | None:
    """管道尾 head/tail/wc → Select-Object/Measure-Object 封闭翻译。

    Returns:
        (改写命令, 原命令)——可安全改写；None——不属封闭集/前段仍有
        unix-only/无管道尾（维持 gate 拒绝教学）。
    """
    stripped = command.rstrip()
    m = _CLOSED_PIPE_TAIL_RE.search(stripped)
    if not m:
        return None
    tail = m.group("tail").strip()
    n = (m.group("n") or "").strip()
    if tail.startswith("| wc"):
        ps_tail = "| Measure-Object -Line"
    elif tail.startswith("| head"):
        ps_tail = f"| Select-Object -First {n}"
    else:
        ps_tail = f"| Select-Object -Last {n}"
    head_part = stripped[: m.start("tail")].rstrip()
    if not head_part:
        return None
    if detect_untranslated_unix(head_part):
        return None
    return (head_part + " " + ps_tail, command)


#: 「只读限流词**直接形态**」的封闭集：`head -N f` / `tail -N f`（含 `-n N` 变体）。
#: 与 `_CLOSED_PIPE_TAIL_RE` **互补**：后者只管**管道尾**，不管直接写的那一半 ——
#: 而直接形态恰好是 agent 最常用的写法。实测（2026-09-16）：`head -5 f` /
#: `head -n 5 f` / `tail -20 f` 三条**全部整条被拒**（管道尾那两条却能自动转译）。
_READONLY_LIMIT_RE = re.compile(
    r"^(?P<cmd>head|tail)\s+(?:-n\s*|-)?(?P<n>\d+)\s+(?P<file>[^-\s]\S*)$"
)
#: `wc -l f`（**只认 `-l`**：`wc -c/-w` 的等价物不同，宁可留给 gate 教学）。
_READONLY_WC_RE = re.compile(r"^wc\s+-l\s+(?P<file>[^-\s]\S*)$")
#: 出现任一即**不译**：复合命令 / 重定向 / 命令替换 / 通配。翻译器只在能**完整
#: 确认**整条命令形状时才动手 —— 猜错 = 把 agent 的命令改坏（比拒绝更糟）。
_SHELL_META_RE = re.compile(r"[|&;<>`$()\[\]{}*?!]")


def _hw_read_target_allowed(path: str) -> bool:
    """`.hiveweave` 下的目标是否属**允许只读**的那几类（M-1 守卫）。

    **复用**既有的三个谓词（`_ALLOWED_HW_SUBDIRS` / `_HW_LOGS_REF` /
    `_HW_MERGE_QUARANTINE_REF`）—— `.hiveweave` 的读放行规则**只此一份**，
    不新增词表也不新增模式（本仓在"清单双写"上栽过两次）。

    不落在读放行面时返回 False ⇒ 调用方**不译**、退回"拒 + 教学"。
    ⚠ 这里只判"路径面"，不判动词 —— 因为本翻译器产出的形态**本身就是只读**的
    （`Get-Content` / `.Count`）。
    """
    if not _HIVEWEAVE_REF.search(path):
        return True                       # 与 `.hiveweave` 无关
    if _ALLOWED_HW_SUBDIRS.search(path):
        return True                       # shared/reports/drafts/worktrees/handoffs/sandbox-temp
    if _HW_LOGS_REF.search(path) or _HW_MERGE_QUARANTINE_REF.search(path):
        return True                       # 两个"只读例外"（诊断出口 / 隔离区）
    return False


def try_readonly_limit_translation(command: str) -> tuple[str, str] | None:
    """**直接形态**的只读限流词 → PowerShell 等价（并行池 · 三条「路不通」①）。

    修的是「**路在、走不通**」：`head`/`tail`/`wc` 的**管道尾**形态早就自动转译了
    （:func:`try_closed_pipe_translation`），而**直接形态**（`head -5 f`）仍然整条
    被拒 —— 偏偏那是 agent 最常用的写法。判据从"拦了几个"改成"**译了几个**"。

    映射（与 `shell_dialect.UNIX_ONLY_HINTS` 给 agent 的**教学文案逐字同源**，
    只是这里真的**执行**它，而不是让 agent 自己抄一遍）::

        head -N f   → Get-Content f -TotalCount N
        tail -N f   → Get-Content f -Tail N
        wc -l f     → (Get-Content f).Count

    ⚠ **只译"本来就会被拒的"**：入口先跑 :func:`detect_untranslated_unix`，
    返回 None（本来就放行）⇒ 直接 ``return None``。⇒ 本函数**不可能**改变任何
    "本来就合法"命令的行为，只把「拒 + 教学」换成「译 + 执行」。
    ⚠ 复合/重定向/替换（管道 `&&` 重定向 反引号 `$()` 通配）一律**不译**，
    交给 gate 教学；`wc -c/-w` 也不译（等价物不同）。
    """
    stripped = (command or "").strip()
    if not stripped or _SHELL_META_RE.search(stripped):
        return None
    # 只处理"不译就会被拒"的形态（本函数的改动面因此严格是"+译"）
    if detect_untranslated_unix(stripped) is None:
        return None
    m = _READONLY_LIMIT_RE.match(stripped)
    if m:
        cmd, n, f = m.group("cmd"), m.group("n"), m.group("file")
        if not _hw_read_target_allowed(f):
            # ⚠ **M-1（审计必修）**：不加这条守卫，本翻译器会**新开一条读
            # `.hiveweave` 受保护文件的路径** —— `.hiveweave` 护栏
            # （`_check_hiveweave_command`）是按**动词**匹配的（`cat`/`rm`…），
            # `head`/`tail`/`wc` **不在**动词表里，而 `Get-Content` 是被**刻意
            # 排除**的只读 cmdlet（否则 `.hiveweave/logs` 的只读放行会被关掉）。
            # 于是 `head -5 .hiveweave/data.db` 在**改动前**被方言门拦下（顺带
            # 保住了这条策略）、在**改动后**会译成 `Get-Content … -TotalCount 5`
            # 并**执行**。⇒ 目标落在 `.hiveweave` 且不属既有读放行面时**不译**，
            # 退回"拒 + 教学"（= 改动前行为）。
            log.info(
                "bash.dialect_translate_refused_hiveweave",
                file=f,
                note="目标在 .hiveweave 且不属读放行面 ⇒ 维持拒绝（不译）",
            )
            return None
        ps = (
            f"Get-Content {f} -TotalCount {n}"
            if cmd == "head"
            else f"Get-Content {f} -Tail {n}"
        )
        return (ps, command)
    m = _READONLY_WC_RE.match(stripped)
    if m:
        # 同一个守卫（`wc -l .hiveweave/data.db` 同样是"新开一条读路径"）
        if not _hw_read_target_allowed(m.group("file")):
            log.info(
                "bash.dialect_translate_refused_hiveweave",
                file=m.group("file"),
                note="目标在 .hiveweave 且不属读放行面 ⇒ 维持拒绝（不译）",
            )
            return None
        return (f"(Get-Content {m.group('file')}).Count", command)
    return None


def try_dialect_translation(command: str) -> tuple[str, str] | None:
    """方言自动转译的**唯一链入口**（顺序：管道尾 → 直接形态）。

    ⚠ 为什么要有这个函数：链本身只能有**一份**。原先两个调用点各自只调
    `try_closed_pipe_translation`；加第二个翻译器时若各自再加一行，就又长成
    "每处各列一份清单"（本仓在事实位白名单上栽过两次）。
    """
    got = try_closed_pipe_translation(command)
    if got is not None:
        return got
    return try_readonly_limit_translation(command)


def _normalize_for_pwsh(command: str) -> str:
    """[退役 P1-3 B 结构解] bash 惯用法 → pwsh 的词典翻译层。

    P1-3（B 结构解，对齐 deepseek-harness"不翻译、原生双工具"）后不再被
    调用：受限 bash 命令 verbatim 交给 pwsh，unix-only 由
    :func:`detect_untranslated_unix` 前置拒绝并给等价。保留 stub 仅为兼容
    引用方测试与回滚审计 —— 生产路径不经过本函数。
    """
    return command


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
            from hiveweave.util.win_subprocess import hidden_exec

            killer = await hidden_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
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


async def _run_native(
    command: str, cwd: str, timeout_s: int | None, *, dialect: str = "bash"
) -> dict[str, Any]:
    """Execute command via the OS native shell.

    P1 fix(TEST11-R3): Windows 下优先使用 Git Bash（bash -c），
    根治 cmd 不支持管道/变量赋值/&&复合/bash script.sh 的固有限制。
    无 Git Bash 时降级为 cmd /s /c + 命令映射兜底。

    ``dialect="pwsh"``（pwsh 工具）：命令已是 PowerShell 方言 —— 直接交给
    pwsh，不做 unix 规范化（沙箱 off 时也要与受限路径同语义）。
    """
    is_windows = sys.platform.startswith("win")
    if dialect == "pwsh":
        import shutil as _shutil

        pwsh_exe = _shutil.which("pwsh")
        if not pwsh_exe:
            return {"output": "", "stdout": "", "stderr": "",
                    "exit_code": None, "timed_out": False,
                    "error": "pwsh (PowerShell 7+) not found on PATH — "
                             "the pwsh tool requires it. Use bash instead."}
        shell_args = [pwsh_exe, "-NoProfile", "-NonInteractive",
                      "-Command", f"{PWSH_ENCODING_PREAMBLE}{command}"]
    elif is_windows:
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

    # 0-3：**这里就把 git 加固落进 env，并把它读成事实**（而不是等下层的漏斗
    # 顺手注入、事后靠推断）。`apply_git_hardening` 是幂等的（带自证标记，
    # 见 `win_subprocess`），所以下层 `hidden_exec` 再调一次是 no-op —— 子进程
    # 拿到的环境逐字节不变，换来的是一条**可测的**事实位：这次命令到底有没有
    # 带加固配置。没有消费者的事实位就是日志，这正是本条要治的病。
    from hiveweave.util.win_subprocess import (
        apply_git_hardening,
        git_hardened as _git_hardened_env,
    )

    env = apply_git_hardening(env)
    _git_hardened = _git_hardened_env(env)

    try:
        from hiveweave.util.win_subprocess import hidden_exec

        proc = await hidden_exec(
            *shell_args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        # M2（2026-09-16）：**spawn 失败 = 命令从未执行** ⇒ 构造点直接声明位。
        # 以前靠文本表兜（而文本表只在 `blocked` 分支被咨询、这两条出口
        # `blocked=False` ⇒ **永不生效**），结果落 `outcome_unknown`（"结果未知、
        # 别盲目重试"）而不是正确的 `runner_failed`。判据由**状态**给出：
        # `exit_code is None` = 进程根本没起来。
        return finalize_fact_dict({
            "output": "", "stdout": "", "stderr": "",
            "exit_code": None, "timed_out": False,
            "fact": "runner_failed",
            "error": f"Failed to spawn shell: {exc}"})
    except OSError as exc:
        return finalize_fact_dict({
            "output": "", "stdout": "", "stderr": "",
            "exit_code": None, "timed_out": False,
            "fact": "runner_failed",
            "error": f"Failed to spawn shell: {exc}"})

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
                "exit_code": None, "timed_out": True, "error": None,
                "git_hardened": _git_hardened}
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
        "git_hardened": _git_hardened,
    }


def _cwd_style_hint(cwd: str, relative: str | None = None) -> str:
    """MAIN vs worktree label — relative path only, never dump D:\\ or /d/."""
    return (
        f"{cwd_display(cwd, relative)} "
        f"— relative paths; never invent /workspace"
    )


# ── #6 · 失败出口统一 cwd_display 头（多树归因的必要条件）─────────────
#
# 判据（**多树特有**，fixplan §10.5）：DSH 是单 checkout，"命令在哪跑的"
# 无歧义；我们有多棵树 + per-agent 授权树根（``acl_sandbox/policy.py:54``）
# ⇒ **缺 ``cwd_display`` 就是归因错位**（L17/L20 同族）——agent 看到
# "Working directory does not exist" 却不知道是哪棵树的哪个相对路径。
#
# 实测缺口：``bash.py`` 内 ``"blocked": True`` 出口 16 处，而
# ``cwd_display(`` 仅 3 处（含定义）⇒ 仅 2 处带回执头。逐个手改会复发
# （约束写在调用方看得见的地方）⇒ 用**统一装饰器**在出口处施加，
# 与 L3「单一漏斗」同一思路。
def with_cwd_display(fn):
    """把失败出口的 ``error`` 统一补上 ``cwd_display`` 头（幂等）。

    被装饰的 async 函数返回裸 dict（bash.py 既有形态）。装饰器只在
    **失败**结果上追加 ``_cwd_style_hint``，且：
    - 已有 ``[MAIN``/``[worktree `` 头（成功路径或已手加）**不重复追加**；
    - 成功结果、无 ``error`` 的结果**原样返回**（零行为变化）。

    与 ``finalize_fact_dict`` 正交：那个管**事实位**，这个管**归因头**。
    """
    import functools

    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if not isinstance(result, dict) or result.get("success"):
            return result
        err = result.get("error")
        if not isinstance(err, str) or not err:
            return result
        # 幂等：已带回执头就不再叠加
        if "[MAIN " in err or "[worktree " in err or "[cwd unknown]" in err:
            return result
        cwd = ""
        # 位置/关键字两种取法都试（各出口签名不同：workdir / cwd / workspace_path）
        for key in ("workdir", "cwd"):
            if key in kwargs and kwargs[key]:
                cwd = str(kwargs[key])
                break
        if not cwd:
            ws = kwargs.get("workspace_path") or ""
            cwd = str(ws)
        if not cwd:
            return result
        result["error"] = f"{err}\n{_cwd_style_hint(cwd)}"
        return result

    return _wrapped


# ── #6 · spawn 前命令串预检（越出授权树）────────────────────────────
#
# **一件事**，在**发车前**判定（fail-fast，不是等沙箱报错才归因）：
#
# ① **越出授权树**：命令里出现 `.hiveweave/worktrees/<非本树 id>/` ⇒ 效果落点
#    越出 ``boundary_root``（``acl_sandbox/policy.py:54``）。判据复用
#    ``util/path_guard``，与 file 侧**同一函数、同一处方**（fixplan §10.3）。
#    注意共用的是"越出授权树"的判定，**不是** DSH 那种单树内 sandbox。
#
# ② unix-only 命令**不在此处**：``detect_untranslated_unix`` 已覆盖——
#    ``_split_command_segments`` 会切开 `|` / `;` / `&&`，每段再查 head token，
#    故**管道尾同样命中**（逐例实测：`git log | head -5` / `ls | wc -l` 均被拦）。
#    曾在此另写一份正则 + "扫全 token"循环，实测**不改变任何行为** ⇒ 属死代码
#    已删，真正的缺口只是 ``find`` 不在方言门词表里（已补）。
#    在此再判一次还会引入「翻译 vs 拒绝」的顺序陷阱
#    （`git log | head -3` 属封闭集，要**翻译**不要**拒绝**）。
#
# 平台纪律：**不猜译、不改写**用户命令 —— 拒发 + 同款中文处方。


def precheck_command_string(command: str, workspace_path: str = "") -> str | None:
    """spawn 前命令串预检：越出授权树则返回拒绝文案（含处方），否则 None。

    判据来自**我们自己的模型**（fixplan §10.3）：``boundary_root`` 已界定
    「本 agent 的授权树」（``acl_sandbox/policy.py:54``）。命令串里出现
    **别的** worktree 落点 = 效果落点越出授权树 —— 这不是新规则，是既有
    边界在 shell 侧的 enforcement（此前只有 file 侧在看，而 ``run_command``
    是 bash 的逃生口，守卫只在 file 侧不算 enforcement）。

    **只做越界一维**：unix-only 交给 ``detect_untranslated_unix``（同文件，
    位置无关补齐后已覆盖管道尾/参数位）。在此再判一次会与封闭集管道尾翻译
    抢跑（`git log | head -3` 属封闭集，要**翻译**不要**拒绝**）。

    跨树判定与 shell 方言**正交** ⇒ 无条件生效（native Git Bash 下同样越界）。
    """
    if not command or not command.strip():
        return None
    for seg in _split_command_segments(command):
        for tok in seg.split():
            t = tok.strip("\"'")
            if not t or t.startswith("-"):
                continue
            if path_guard.is_foreign_worktree_ref(t, workspace_path):
                # 点名越出的是**哪棵树**（多树归因的最小事实，fixplan §10.5）
                raw_tid = path_guard.worktree_id_in_path(t)
                # 未展开的 shell 变量（$sid / ${x}）不是树 id：原样回显
                # 「worktree $sid」会让模型把**模板变量**读成**结论**
                # （TEST_DSH_55 §3 第 4 条实测）。此时改为明说「请先展开」。
                if "$" in t or (raw_tid and "$" in raw_tid):
                    shown = raw_tid or t
                    return (
                        "Command blocked: 命令里的 worktree 引用含未展开的 "
                        f"shell 变量（{shown}）—— 平台比对的是字面量，"
                        "展开后同样越界（那棵树不属于你）。"
                        f"{path_guard.OUT_OF_BOUNDARY_HINT}"
                    )
                tid = raw_tid or "?"
                return (
                    f"Command blocked: 命令指向 worktree {tid}（不是你所在的树）。"
                    f"{path_guard.OUT_OF_BOUNDARY_HINT}"
                )
    return None


def _pwsh_effective_shell(*, confined: bool) -> bool:
    """受限/原生 + 平台 + pwsh 存在 ⇒ 该命令**实际**由 pwsh 解释。

    `confined` 必须来自**唯一判定点**（`policy.resolve_spawn_decision`）。
    改造前这里有第二处判定（读 `acl_sandbox_active()`）：同一个事实被两处判，
    于是项目级逃生门（`danger-full-access`）下 —— 路由走 native（Git Bash），
    方言门却仍按 pwsh 拒掉 unix-only 命令，**把合法命令误拒**。
    """
    if not sys.platform.startswith("win"):
        return False
    import shutil as _shutil

    if not _shutil.which("pwsh"):
        return False  # cmd 兜底路径走 _map_unix_to_cmd，不是 pwsh 方言
    return confined


def _pwsh_is_effective_shell() -> bool:
    """⚠ **legacy 零参版**（保留给 `subagent.py` 的方言提示与既有测试）。

    它是「平台模式近似」—— 只看 `acl_sandbox` 配置，**不含**项目级逃生门，
    因此与真实路由可能不一致（见 `_pwsh_effective_shell` 的说明）。
    `execute_bash` / `execute_run_command` 已改为传入真实判定，不再走本函数。
    """
    from hiveweave.services.acl_sandbox.integration import acl_sandbox_active

    return _pwsh_effective_shell(confined=acl_sandbox_active())


def _pwsh_dialect_gate(command: str, *, confined: bool | None = None) -> str | None:
    """受限模式（pwsh 生效）下 unix-only 命令 → 可操作错误；否则 None（放行）。

    P1-3（B 结构解）：词典翻译层退役后，受限 bash 命令以 PowerShell 语义
    **verbatim** 交给 pwsh；此处对原生命令做 unix-only 前置拒绝并给 pwsh
    等价（兑现 bash 工具 description 的承诺 —— rejected up front with the
    pwsh equivalent，而非静默透传造成 head/--ignore 混血参数）。

    ``confined`` = 本次 spawn 的真实判定；缺省 None ⇒ 回退平台模式近似
    （既有测试与调用方）。**新调用方一律显式传**，否则会重现
    「同一事实两处判」的偏差。
    """
    if confined is None:
        if not _pwsh_is_effective_shell():
            return None
    elif not _pwsh_effective_shell(confined=confined):
        return None
    return detect_untranslated_unix(command)


@with_cwd_display
async def execute_bash(
    command: str,
    workdir: str,
    workspace_path: str,
    timeout_ms: int | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    unbounded: bool = False,
    dialect: str = "bash",
    guard_ask_resolved: bool = False,
) -> dict[str, Any]:
    """Execute a bash command and return {success, output, error}.

    Performs:
      1. Self-destructive command check (7 patterns)
      2. Sandbox validation (workdir must be within workspace)
      3. Timeout: foreground clamped 5s..600s; unbounded/timeout_ms=0 waits
         until the process exits (background bash / job_kill).
      4. Execute (ACL sandbox > native)
      5. Truncate output at 1MB (layer 2, bash-specific)

    ``dialect="pwsh"`` (the ``pwsh`` tool) keeps bash tool semantics; the
    unix-only gate deliberately NO LONGER short-circuits it (R3 P0-2: models
    write unix idioms inside the pwsh tool too — `| head -n 100` reached
    execution and garbled). pwsh-native commands do not match the unix-only
    fingerprint, so gate passes them.
    """
    if not command or not command.strip():
        return {"success": False, "output": "",
                "error": "Error: command is required"}

    # #6 · spawn 前命令串预检（**只做越出授权树**这一维；unix-only 见既有
    # ``detect_untranslated_unix``，已位置无关）。在安全校验**之前**：
    # 越界问题不需要先弹审批（fail-fast 省一轮墙钟）。
    precheck = precheck_command_string(command, workspace_path)
    if precheck:
        log.warning("bash.precheck_blocked", command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {precheck}", "blocked": True,
                "fact": "runner_failed"})

    # 1. 统一命令安全校验 — 自毁命令 + 敏感路径 + .hiveweave 系统目录
    # T2.2: ask 判定走在线审批（agent_id 在场时）；guard_ask_resolved=上游
    # 已解析放行，不重复弹审批。
    # P0-1: cwd 传**实际执行目录**（与下方 `cwd = ws / workdir` 同一口径）——
    # 相对路径删除目标的落点判定必须以此为准，否则一律误判越界。
    blocked, reason = await _validate_command_safety_resolved(
        command,
        agent_id=agent_id or "",
        tool_name="bash" if dialect != "pwsh" else "pwsh",
        tool_args={"command": command[:200]},
        ask_already_resolved=guard_ask_resolved,
        cwd=str(Path(workspace_path or os.getcwd()) / workdir)
        if workdir
        else (workspace_path or os.getcwd()),
    )
    if blocked:
        log.warning("bash.blocked", reason=reason, command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {reason}", "blocked": True,
                "fact": "runner_failed"})

    from hiveweave.services.eval_seal import sealed_bash_deny_for_workspace

    seal_reason = sealed_bash_deny_for_workspace(workspace_path, command)
    if seal_reason:
        log.warning("bash.eval_sealed", command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {seal_reason}", "blocked": True,
                "fact": "runner_failed"})

    # 1.5. Auto-source .hiveweave/env.sh if the project has one.
    # The project declares its own environment setup.
    hw_dir = str(Path(workspace_path) / ".hiveweave")
    # #1 治本：本次命令的执行面**在这里判定一次**，本函数后续每个分支（env.sh
    # 前缀、封闭集翻译、方言门）与 spawn 路由**共用这一份**。改造前这些点各自
    # 读 `acl_sandbox_active()` —— 与真实路由可以不一致（项目级
    # `danger-full-access` 下路由已判 native，方言门却仍按 pwsh 拒），
    # 结果是**合法命令被误拒**：同一事实两处判的典型代价。
    from hiveweave.services.acl_sandbox.policy import resolve_spawn_decision

    spawn_decision = await resolve_spawn_decision(project_id)
    # P1：受限 shell 是 pwsh/cmd，无法 source bash 语法的 env.sh ——
    # 跳过前缀（否则所有命令被 `source` 掐死），项目环境由 .hiveweave 之外
    # 的机制声明（见 spec §18.3 受限 shell 方言适配）。
    if not spawn_decision.confined:
        command = _source_env_sh(command, hw_dir)

    # 1.6. 方言 fast-fail（DSH_33 P0 / R3 P0-2）：受限 shell = pwsh 时，
    # unix-only 命令直接给等价写法，而不是让 pwsh 回「不是内部或外部命令」。
    # 对 bash 与 pwsh 两个 dialect **一视同仁**（R3 实测模型在 pwsh 工具里照写
    # `| head`/unix flag —— 短路被当侧门利用）。pwsh 原生命令不命中指纹，放行。
    # 46 轮 #1：封闭集管道尾自动翻译（拒答式教学三度复现的让步——
    # 翻译集合封闭承诺见 try_closed_pipe_translation docstring）。
    # 仅 pwsh 宿主生效（审计 H2）：Linux/Git Bash 下原生命令合法，
    # 无条件改写会弄坏可直接执行的 bash。
    translated_pair = None
    if _pwsh_effective_shell(confined=spawn_decision.confined):
        # 唯一链：管道尾 → 直接形态（`try_dialect_translation` 是单点）
        translated_pair = try_dialect_translation(command)
        if translated_pair is not None:
            command, _orig_cmd = translated_pair
            log.info(
                "bash.dialect_auto_translated",
                translated_preview=command[:180],
            )

    dialect_err = _pwsh_dialect_gate(command, confined=spawn_decision.confined)
    if dialect_err:
        log.info("bash.dialect_gate", command_preview=command[:120])
        # s3-clone_06 P0-3/P0-4：命令从未执行（方言不认）→ runner_failed=1。
        # 此前只有 blocked=1，被 F10 归因成「平台护栏拒绝（权限/沙箱/安全）」，
        # 把"方言不兼容"错指为"安全拦截"，撞坑 Agent 拿到的是错的排查方向。
        return finalize_fact_dict({"success": False, "output": "",
                "error": dialect_err, "blocked": True,
                "fact": "runner_failed", "dialect_failed": True})

    # 尾注在 gate 之后追加（gate 对注释里的原文词条会误抓 head/tail）
    if translated_pair is not None:
        command = f"{command}  # [auto-translated from: {_orig_cmd}]"

    # 2. Resolve cwd and validate sandbox
    ws = workspace_path or os.getcwd()
    if workdir:
        cwd = str(Path(ws) / workdir)
    else:
        cwd = ws

    if not _is_within_workspace(cwd, ws):
        # F4 补接线（TEST_DSH_50/51 实测 runner_failed 仅 38.5%/20%）：
        # schema.py 里 F4 的定义域明写「命令未执行（参数注入破坏 / 方言不支持 /
        # **权限** / 审批 / runner 自身故障）」——沙箱拒绝属「权限」，命令
        # 从未执行，必须置 runner_failed，否则归因链缺事实位。
        return finalize_fact_dict({"success": False, "output": "",
                "error": "Error: Sandbox violation - workdir must be within workspace",
                "blocked": True, "fact": "runner_failed"})

    if not Path(cwd).exists():
        # F4 补接线：cwd 不存在 = 命令从未执行（runner_failed）。
        # 该签名在 TEST_DSH_50/51 各出现 2~3 次（幽灵 worktree 前缀路径），
        # 全部因未置位而落在观测盲区里。
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: Working directory does not exist: "
                         f"{cwd_display(cwd, workdir)}",
                "blocked": True, "fact": "runner_failed"})

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
            # A-2 (P1-4): 未显式给超时时按工具声明取默认（bash 500s / pwsh 500s）。
            _tool_key = "pwsh" if dialect == "pwsh" else "bash"
            timeout_ms = TOOL_DEFAULT_TIMEOUT_MS.get(
                _tool_key, DEFAULT_TIMEOUT_S * 1000
            )
        timeout_ms = int(timeout_ms)
        # Heuristic: values 1-600 are likely seconds, not milliseconds
        if 1 <= timeout_ms <= 600:
            timeout_ms = timeout_ms * 1000
        timeout_ms = max(5000, min(timeout_ms, MAX_TIMEOUT_S * 1000))
        timeout_s = timeout_ms / 1000

    # 4. Choose execution backend
    # P1 (spec §5.7) + #1 治本：spawn 经**唯一入口**取判定并路由（受限/原生），
    # 工具不再判断沙箱 —— 改造前这里是 `_run_sandboxed()` 返回 None 再由本函数
    # 回落 native，判定含义因此散在五个调用点上（python_script 读成"沙箱坏"）。
    result = await _run_sandboxed(
        command, cwd, timeout_s,
        workspace_path=ws, agent_id=agent_id, project_id=project_id,
        entry="bash", dialect=dialect, decision=spawn_decision,
    )

    if result.get("error"):
        # F4：runner 失败（命令没跑起来）—— spawn 失败 / 方言 / 沙箱拒绝。
        # 错误已带原因，标记事实位供收纳（stall 归因先于
        # denial，对齐 DSH RunnerFailureRule 顺序）。
        return finalize_fact_dict({
            "success": False, "output": "",
            "error": f"Error: {result['error']}\n{cwd_hint}",
            "fact": "runner_failed",
            **_enforcement_stamp(result),
        })

    if result["timed_out"]:
        # F7：超时统一分类 —— command 超时（命令跑起来但没按时完成）。
        return {
            "success": False, "output": "",
            "error": "Error: Command timed out after "
                     f"{int(timeout_s or 0)} seconds\n{cwd_hint}",
            "timeout_kind": "command",
            "timeout_ms": int((timeout_s or 0) * 1000),
            **_enforcement_stamp(result),
        }

    output = _truncate_output(result["output"])
    exit_code = result["exit_code"]

    if exit_code == 0:
        body = output if output.strip() else "(no output)"
        return {"success": True,
                "output": f"{body}\n\n{cwd_hint}\nExit code: 0",
                "error": None,
                "exit_code": 0,
                **_enforcement_stamp(result)}

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
    error_msg = _maybe_append_test_hints(command, error_msg)
    # ⚠️ 必须经漏斗：`command_failed` 已是**派生键**，裸写它会被
    # `finalize_fact_dict` 当作陈旧值 pop 掉（权威是 `fact`）。这是全平台
    # **流量最大**的失败出口 —— 漏了它等于「普通非零退出」永远拿不到事实位。
    return finalize_fact_dict({
        "success": False,  # non-zero exit is not success
        "output": f"{body}\n\n{cwd_hint}\nExit code: {exit_code}",
        "error": error_msg,
        "exit_code": exit_code,
        # F4：命令执行了但失败（非零退出 = command_failed，不是 runner 失败）
        "fact": "command_failed",
        **_enforcement_stamp(result),
    })


@with_cwd_display
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

    # #6 · spawn 前命令串预检（**只做越出授权树**；unix-only 见既有方言门）。
    # 与 execute_bash 同一条链，不可旁路 ——
    # 守卫只在 file.py 不算 enforcement：run_command 是 bash 的逃生口。
    precheck = precheck_command_string(command, workspace_path)
    if precheck:
        log.warning("run_command.precheck_blocked", command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {precheck}", "blocked": True,
                "fact": "runner_failed"})

    # 统一命令安全校验 — 自毁命令 + 敏感路径 + .hiveweave 系统目录（A3 + 旁路修复）
    # T2.2: ask 判定走在线审批
    # P0-1: cwd 传实际执行目录（run_command 的 cwd 参数已是绝对目录）。
    blocked, reason = await _validate_command_safety_resolved(
        command,
        agent_id=agent_id or "",
        tool_name="run_command",
        tool_args={"command": command[:200]},
        cwd=cwd or workspace_path,
    )
    if blocked:
        log.warning("run_command.blocked", reason=reason,
                    command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {reason}", "blocked": True,
                "fact": "runner_failed"})

    from hiveweave.services.eval_seal import sealed_bash_deny_for_workspace

    seal_reason = sealed_bash_deny_for_workspace(workspace_path, command)
    if seal_reason:
        log.warning("run_command.eval_sealed", command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {seal_reason}", "blocked": True, "fact": "runner_failed"})

    # R3 P0-2：run_command 同样是被方言混血走的后门（attestation 测试步）。
    # 与 bash/pwsh 工具同一 unix-only gate；native 环境（无 pwsh）gate 闭口。
    # 仅 pwsh 宿主生效（审计 H2，同 execute_bash）。
    # #1 治本：判定在此**取一次**，翻译/方言门/路由共用（同 execute_bash）。
    from hiveweave.services.acl_sandbox.policy import resolve_spawn_decision

    spawn_decision = await resolve_spawn_decision(project_id)
    translated_pair_rc = None
    if _pwsh_effective_shell(confined=spawn_decision.confined):
        translated_pair_rc = try_dialect_translation(command)
        if translated_pair_rc is not None:
            command, _orig_cmd_rc = translated_pair_rc
            log.info(
                "run_command.dialect_auto_translated",
                translated_preview=command[:180],
            )

    run_dialect_err = _pwsh_dialect_gate(command, confined=spawn_decision.confined)
    if run_dialect_err:
        log.info("run_command.dialect_gate", command_preview=command[:120])
        return finalize_fact_dict({"success": False, "output": "",
                "error": run_dialect_err, "blocked": True,
                "fact": "runner_failed", "dialect_failed": True})

    ws = workspace_path or os.getcwd()
    if cwd:
        full_cwd = str(Path(ws) / cwd)
    else:
        full_cwd = ws

    if not _is_within_workspace(full_cwd, ws):
        # F4 补接线：同 workdir 侧，权限拒绝 = 命令从未执行。
        return finalize_fact_dict({"success": False, "output": "",
                "error": "Error: Sandbox violation - cwd must be within workspace",
                "blocked": True, "fact": "runner_failed"})

    if not Path(full_cwd).exists():
        # F4 补接线：run_command 侧的同一签名（与上面 pwsh 侧对称）。
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: Working directory does not exist: "
                         f"{cwd_display(full_cwd, cwd)}",
                "blocked": True, "fact": "runner_failed"})

    # A-2 (P1-4): 未显式给超时时按工具声明取默认（run_command 保持 120s）。
    safe_timeout = int(timeout_ms or TOOL_DEFAULT_TIMEOUT_MS["run_command"])
    if 1 <= safe_timeout <= 600:
        safe_timeout = safe_timeout * 1000
    safe_timeout = max(5000, min(safe_timeout, MAX_TIMEOUT_S * 1000))
    timeout_s = safe_timeout // 1000

    log.info("run_command.execute", cwd=full_cwd, timeout_s=timeout_s,
             command_preview=command[:120])

    # P1 (spec §5.7) + #1 治本：run_command 同 bash —— 判定与回落都在唯一入口内。
    result = await _run_sandboxed(
        command, full_cwd, timeout_s,
        workspace_path=ws, agent_id=agent_id, project_id=project_id,
        entry="run_command", decision=spawn_decision,
    )

    if result.get("error"):
        # F4：runner 失败（命令没跑起来）—— spawn 失败 / 沙箱拒绝。
        return finalize_fact_dict({"success": False, "output": "",
                "error": f"Error: {result['error']}",
                "fact": "runner_failed",
                **_enforcement_stamp(result)})

    if result["timed_out"]:
        # F7：command 超时（命令跑起来但未按时完成）。
        return {"success": False, "output": "",
                "error": f"Error: Command timed out after {timeout_s} seconds",
                "timeout_kind": "command",
                "timeout_ms": int((timeout_s or 0) * 1000),
                **_enforcement_stamp(result)}

    output = _truncate_output(result["output"])
    exit_code = result["exit_code"]

    if exit_code == 0:
        body = output if output.strip() else "(no output)"
        return {"success": True, "output": f"{body}\n\nExit code: 0",
                "error": None, "exit_code": 0,
                **_enforcement_stamp(result)}

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
    error_msg = _maybe_append_test_hints(command, error_msg)
    # 同上：`run_command` 的普通非零退出，同样必须经漏斗。
    return finalize_fact_dict({
        "success": False,
        "output": f"{body}\n\nExit code: {exit_code}",
        "error": error_msg,
        "exit_code": exit_code,
        # F4：命令执行了但失败（非零退出 = command_failed）
        "fact": "command_failed",
        **_enforcement_stamp(result),
    })


# ── Pydantic models + @tool registration (Phase 2 migration) ──────

from pydantic import BaseModel, Field, ConfigDict, field_validator

from .base import tool
from .result import ToolResult


def _coerce_bool_flag(v: Any) -> bool:
    """宽容布尔转换(LLM 常传 "true"/"yes"/1)。"""
    if v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


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
        default=TOOL_DEFAULT_TIMEOUT_MS["bash"],
        ge=5000,
        le=600000,
        description=(
            "Foreground only (5s–10min). Ignored when background=true. "
            "Default: 600000 (10min). Max: 600000 (10 min). Values 1-600 "
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
    test_evidence: bool = Field(
        default=False,
        alias="testEvidence",
        description=(
            "Declare this command as test evidence: ALWAYS issue a "
            "test_run attestation (exit 0 = green) regardless of command "
            "text. Use when running custom validation scripts whose names "
            "don't match test_/verify_/check_ patterns (e.g. "
            "validate-suite.mjs). The full command+output is still "
            "recorded for reviewer inspection."
        ),
        json_schema_extra={"aliases": ["testEvidence", "test_evidence"]},
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

    @field_validator("test_evidence", mode="before")
    @classmethod
    def _coerce_test_evidence(cls, v: Any) -> bool:
        return _coerce_bool_flag(v)


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
        default=TOOL_DEFAULT_TIMEOUT_MS["run_command"],
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
    test_evidence: bool = Field(
        default=False,
        alias="testEvidence",
        description=(
            "Declare this command as test evidence: ALWAYS issue a "
            "test_run attestation (exit 0 = green) regardless of command "
            "text. Use when running custom validation scripts whose names "
            "don't match test_/verify_/check_ patterns. The full "
            "command+output is still recorded for reviewer inspection."
        ),
        json_schema_extra={"aliases": ["testEvidence", "test_evidence"]},
    )

    @field_validator("test_evidence", mode="before")
    @classmethod
    def _coerce_test_evidence_rc(cls, v: Any) -> bool:
        return _coerce_bool_flag(v)


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
    declared_test: bool = False,
) -> str:
    """Create test_run attestation (success or failure). Return note fragment.

    ``workspace`` is the stamp/root workspace. ``exec_cwd`` (optional) is the
    actual directory the command ran in (e.g. workspace/params.cwd) — used for
    VERIFY under-main checks.

    ``declared_test`` (bash testEvidence=true): 意图声明式凭证通道 —
    无条件落凭证，不依赖 is_test_command 正则猜测。正则对 agent 是
    不可见的命名暗号（TEST_DSH_31: validate-suite.mjs 每次全绿却永不
    落凭证，agent 循环 1h 找"免派生校验"）。防伪不靠文件名（伪造
    verify_fake.py 照样过正则），靠凭证记录的 command+stdout+exit_code
    交 reviewer 审。
    """
    from hiveweave.services.attestation import (
        attestation_service,
        is_test_command,
    )
    from hiveweave.services.task import TaskService

    if not declared_test and not is_test_command(command or ""):
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
            # #19（AST 网扫出来的**清单外落点**）：原先是裸
            # `hidden_exec("git", "rev-parse", "HEAD", …)` —— 走通用漏斗但不接
            # 信任锚。改走 anchored `_git`（`stamp_workspace` 就是那棵树本身）。
            from hiveweave.services.git_worktree.git_cmd import _git as _anchored_git

            # ⚠ **不传 project_root**（审计必修 2）：`stamp_workspace` 在
            # executor 分支就是 agent 的 worktree，传它会撞 `AnchorRefusal`
            # ⇒ 拒跑 ⇒ `commit_hash=None`，且 `attestation` 对 commit 缺失是
            # **fail-open** ⇒ "提交必须是当前 worktree HEAD 或祖先"这一维度被**静默关掉**。
            _ok, _out = await _anchored_git(
                ["rev-parse", "HEAD"], stamp_workspace, timeout=5,
            )
            if _ok and _out:
                commit_hash = _out.strip()[:40] or None
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
    # ⚠ D-4（#12 审计带出）：原为 `int(exit_code or 1) == 0` —— `0 or 1` ⇒ **1**
    # ⇒ 条件**恒不成立**，其内的 `test_attestation` 事件**永不触发**（观测缺失）。
    # 同函数另两处（`:3115`/`:3122`）用的正是本条这种写法，一文件内两种口径。
    if resolved and int(exit_code) == 0:
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


def _wrap_routed_background_result(routed: dict, agent_id: str) -> ToolResult:
    """把 dev-server 路由侧的结果重新包成 ``ToolResult``（**事实位原样透传**）。

    L3 收口：``routed`` 已带权威 fact（保留端口=``bad_args`` / spawn 故障=
    ``runner_failed``），重包时**必须原样透传** —— 丢掉它会退回「无证据的默认
    分类」，正是本批要消灭的形态。

    ⚠ **非法组合守卫**（2026-09-14，与 #17 同源）：``blocked=True`` 配**调用方成因**
    的 fact（``bad_args`` / ``command_failed``）会撞 ``_BLOCKED_FACT_KINDS``
    不变式抛 ``ValueError``。本函数上方注释自己写着「保留端口=bad_args」⇒
    **形态可达**（`#17` 已在 `:3230`/`:3532` 两处修过同款）。
    守卫方式必须是「**保留 fact、去掉 blocked**」—— 保留端口是**调用方参数错**，
    不是平台护栏拒绝；**绝不能**悄悄改成 ``runner_failed``（那是把 L6 撤销，
    会让 agent 收到「不是你的 bug」并对同一组参数原地重撞）。
    """
    if routed.get("success"):
        return ToolResult.ok(routed.get("output") or "")
    err_msg = routed.get("error") or "Dev server spawn failed"
    fact = routed.get("fact")
    if routed.get("blocked"):
        if fact in ("runner_failed", "outcome_unknown") or fact is None:
            return ToolResult.blocked_err(err_msg, fact=fact or "runner_failed")
        # 非法组合：调用方成因的格不能被标成「平台护栏拒绝」（L6/L19）。
        log.warning(
            "routed_fact_not_blockable",
            agent_id=agent_id,
            fact=fact,
            action=(
                "路由侧给了 blocked=True + 调用方成因的 fact —— 已按「保留事实位、"
                "去掉 blocked」归一（与保留端口同款语义），**没有**改成 runner_failed"
            ),
        )
        return ToolResult.err(err_msg, fact=fact)
    return ToolResult.err(err_msg, fact=fact)


async def _bash_background(
    *,
    params: BashParams,
    agent_id: str,
    cmd: str,
    exec_ws: str,
    project_id: str | None,
    verify_tid: str | None,
    dialect: str = "bash",
    injection_meta: dict | None = None,
    workdir: str = "",
) -> ToolResult:
    """Run bash off the org turn; attestations still issue when the job finishes."""
    from hiveweave.services.offturn import (
        build_waiting_on,
        next_action_waiting,
        resolve_assignee_task_id,
        start_offturn_job,
    )

    cmd = _strip_trailing_ampersand(cmd)
    # T2.2: ask 判定走在线审批；获批后传 guard_ask_resolved 给 execute_bash，
    # 同一命令不重复弹审批。
    # P0-1: cwd 传实际执行目录（与 execute_bash 的 `exec_ws / workdir` 同口径）。
    blocked, reason = await _validate_command_safety_resolved(
        cmd,
        agent_id=agent_id,
        tool_name="bash" if dialect != "pwsh" else "pwsh",
        tool_args={"command": cmd[:200]},
        cwd=str(Path(exec_ws) / workdir) if workdir else exec_ws,
    )
    if blocked:
        log.warning("bash.blocked", reason=reason, command_preview=cmd[:120])
        return ToolResult.blocked_err(f"Error: {reason}")
    ask_resolved = True

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
            return _wrap_routed_background_result(routed, agent_id)

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
            dialect=dialect,
            guard_ask_resolved=ask_resolved,
        )
        # F2：off-turn 后台同前台——平台改写命令一并回显给 Agent
        if injection_meta:
            _inote = injection_meta.get("note") or ""
            if result.get("output"):
                result["output"] = f"{result['output']}\n\n{_inote}"
            elif result.get("error"):
                result["error"] = f"{result['error']}\n\n{_inote}"
            result["injection_applied"] = bool(injection_meta.get("injected"))
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
                    declared_test=bool(getattr(params, "test_evidence", False)),
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
    "Test runs matching test_/verify_/check_ patterns auto-issue a "
    "test_run attestation; for custom validation scripts with other "
    "names pass testEvidence=true to force the attestation (exit 0 = "
    "green). Long scripts: background=true returns waiting_on — then "
    "commit_turn(waiting); woken with [BASH DONE] / [BASH FAILED]. "
    "Stop with job_kill. Windows: under the sandbox your command is actually "
    "run by pwsh, with a **narrow auto-translation** applied first for two "
    "closed sets (a trailing `| head/-n N` or `| tail/-n N` or `| wc -l` pipe "
    "tail, and a whole-command `head -N f` / `tail -N f` / `wc -l f`). "
    "Everything else unix-only is **not** translated. "
    "unix-only commands (head/tail/grep/wc/sed/awk/xargs/cut/find/touch/which/"
    "sort -u/echo -e…) fast-fail with the pwsh equivalent; rewrite as "
    "suggested or call the `pwsh` tool for PowerShell semantics. Plain "
    "non-unix commands (git, python, uv, node, npm, pip) run fine. "
    "Prefer uv run python. Do not background=true for vite / npm run dev / "
    "uvicorn / http.server — servers never finish, so waiting_on never "
    "fires; prefer start_dev_server. Do not append & on a foreground "
    "command."
    + SANDBOX_TEMP_GUIDE,
    requires_workspace=True,
    security_level="shell",
)
async def bash_tool(params: BashParams, agent_id: str, workspace: str) -> ToolResult:
    """Execute a bash command."""
    return await _shell_tool_impl(params, agent_id, workspace, dialect="bash")


async def _shell_tool_impl(
    params: BashParams, agent_id: str, workspace: str, *, dialect: str
) -> ToolResult:
    """bash / pwsh 共用执行体 —— 唯一差别是交给哪种 shell 方言。

    两个工具共享同一条管线（保留端口守卫 / 尾部 & 收编 / offturn / 截断 /
    test_run attestation / cwd 失败连击提示），避免出现第二套 subprocess 逻辑。
    """
    from hiveweave.services.process_registry import prepare_spawn_command
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    raw_cmd = params.command or ""
    cmd, _env, reserved_err, inj_meta = prepare_spawn_command(
        raw_cmd, project_id=project_id
    )
    if reserved_err:
        # L6（2026-09-11）：改判 **bad_args** —— 模型换个 3000+ 端口即可通过，
        # 判 runner_failed 会让它收到「不是你的 bug」并原地重撞同一端口。
        # ⚠ 2026-09-14 修**真崩溃**：L6 改判时只把 fact 换成 bad_args、**没换构造器**，
        # 而 `blocked_err` 的 `__post_init__` 不变式会抛
        # `ValueError: blocked=True cannot carry fact='bad_args'`（bad_args 是调用方
        # 成因，被 `_BLOCKED_FACT_KINDS` 明确排除 —— 标 blocked 会向 agent 发
        # 「不是你的 bug」信号并原地重撞，正是 L6 要治的病）。
        # 后果：agent 跑 `--port 4000` 拿到的是 Python traceback 而不是端口指引；
        # 且 executor 的 `except` 会早退成**无 fact 的裸 dict**（漏斗旁路）。
        # ⇒ `ToolResult.err` 的 docstring 本来就写明「bad_args 走这里而**不是**
        # blocked_err」：这次只是让代码回到它自己声明的契约上。
        return ToolResult.err(reserved_err, fact="bad_args")
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
            dialect=dialect,
            injection_meta=inj_meta,
            workdir="",
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
            dialect=dialect,
            injection_meta=inj_meta,
            workdir="",
        )

    result = await execute_bash(
        command=cmd,
        workdir="",
        workspace_path=exec_ws,
        timeout_ms=params.timeout,
        project_id=project_id,
        agent_id=agent_id,
        dialect=dialect,
    )
    # F2：平台改写后的最终命令回显 —— 让 Agent 看得见这双手
    # （既有 result_excerpt 落的是 stdout，命令改写只能显式拼接）。
    if inj_meta:
        note = inj_meta.get("note") or ""
        if result.get("output"):
            result["output"] = f"{result['output']}\n\n{note}"
        elif result.get("error"):
            result["error"] = f"{result['error']}\n\n{note}"
        result["injection_applied"] = bool(inj_meta.get("injected"))
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
                declared_test=bool(getattr(params, "test_evidence", False)),
            )
    except Exception as _att_err:
        log.warning("bash_attest_issue_failed", error=str(_att_err))
    meta = _attestation_fields_from_note(attest_note)
    banner = meta.get("banner") or ""
    public = _attestation_public_extra(meta)
    # F4/F7：工具执行事实位透传到 ToolResult（runner/command/注入/超时/方言）
    # **新增事实位必须同时登记进这个白名单**（2026-09-01 实战抓到两次）：
    # 只加进 execute_bash 的返回字典而不登记此处，字段会在这一层被过滤掉，
    # 结果是单测全绿、生产里字段恒 None —— runner_failed 与 dialect_failed
    # 都踩过（后者直接导致 F10 方言归因回落到通用文案）。
    _ff = {
        k: result.get(k)
        for k in _SHELL_FACT_FLAG_KEYS
        if result.get(k) is not None
    }
    return _shell_tool_result(
        success=bool(result.get("success")),
        blocked=bool(result.get("blocked")),
        output=out,
        error=result.get("error") or "Command failed",
        banner=banner,
        suffix=attest_note,
        public=public,
        streak_hint=_streak_hint,
        fact_flags=_ff or None,
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
    fact_flags: dict[str, Any] | None = None,
) -> ToolResult:
    """Command ok + VERIFY belt reject must fail the tool, not look like a pass.

    ⚠ **观测位不随成败消失**（0-3 审计实测的真缺口）：成功分支原先只传
    `public`，把 `fact_flags` 整个丢掉 ⇒ `injection_applied` / `git_hardened`
    在**每一条成功命令**上都落 NULL，而这两列的文档写的是
    「NULL = 不适用/未判定」—— 把"适用且成立"记成"不适用"就是 NULL 说谎。
    ⇒ 成功分支改为**排除归因位后**原样透传：
      · `fact` 是权威事实位，成功结果上必须是 None（不传）；
      · `runner_failed` / `command_failed` 在成功结果上自相矛盾
        （`result.py` 不变式 2 会直接抛）。
    用**排除法**而不是再列一份白名单：新增一种观测位时不必回来登记第二处
    （本仓栽在"每处各列一份清单"上不止一次）。
    """
    if success:
        combined = _combine_attestation_output(output, banner, suffix)
        if _note_is_attest_rejected(suffix) or _note_is_attest_rejected(combined):
            return ToolResult.err(combined.strip(), **public)
        _ok_extra = (
            {
                k: v
                for k, v in fact_flags.items()
                if k not in ("fact", "runner_failed", "command_failed")
            }
            if fact_flags
            else {}
        )
        return ToolResult.ok(combined, **{**public, **_ok_extra})
    err_msg = error or "Command failed"
    if streak_hint:
        err_msg = f"{err_msg}{streak_hint}"
    err_msg = _combine_attestation_output(err_msg, banner, suffix)
    # F4/F7/L3：事实位透传（fact / runner_failed / command_failed /
    # injection_applied / timeout_kind / timeout_ms）—— 供 run_steps 收纳，
    # stall 归因不再靠猜退出码。**blocked 分支也必须带上**（2026-09-01 实战
    # 抓到）：方言门与护栏拒绝都是 blocked=True，此前只传 public 把
    # runner_failed 整个丢掉 —— 报告 #4「runner_failed 恒 0」在 blocked
    # 类失败上原样残留。
    _kw: dict[str, Any] = dict(public)
    if fact_flags:
        _kw.update(fact_flags)
    # L3：fact 走具名参数（不再是 extra 里的裸键），避免与派生属性打架。
    _fact = _kw.pop("fact", None)
    # 兼容旧调用方残留的裸键 → 归一为 fact。
    # ⚠ **E20：两个位都要归一，且顺序确定** —— 原来只认 `runner_failed`
    # （`command_failed=True` 被无条件 pop 掉 ⇒ **位永久丢失**：没有 fact 就
    # 派生不出位，下游只看到"没有位"，于是回落到文本层兜底，而文本层在
    # 有 stdout 的命令回执上是不可靠的）。对称化后两条路等价。
    # `runner_failed` 优先于 `command_failed`：与 `_BIT_FACTS` /
    # `attribution_of` 的既有顺序一致（**顺序单一实现**）。
    # ⚠ 理由订正（审计 D3）：`runner_failed` 不是"更保守"，它是**乐观**信号 ——
    # 下游把它读作「无副作用、可放心重试」，而保守格是 `outcome_unknown`
    # （见 `fact_positions.finalize_tool_result` 的三处长注释）。
    # ⚠ 两位同时为真本身是**矛盾声明**，这里静默吸收了（与 E21 要治的"静默"
    # 同族）；实测 58/59 中 rf∧cf 的行数为 0 ⇒ 本批只留注释，不加告警。
    _legacy_runner = _kw.pop("runner_failed", None)
    _legacy_command = _kw.pop("command_failed", None)
    if _fact is None:
        if _legacy_runner:
            _fact = "runner_failed"
        elif _legacy_command:
            _fact = "command_failed"
    if blocked:
        # H3: 平台护栏拒绝（Command blocked）≠ 模型空转 —— 标 blocked 供
        # stall 检测分流，文本/exit code 语义与 err 一致。
        # blocked 只接受**平台侧成因**的格（`result.py::_BLOCKED_FACT_KINDS`）。
        if _fact in ("runner_failed", "outcome_unknown"):
            return ToolResult.blocked_err(err_msg, fact=_fact, **_kw)
        if _fact is None:
            # 护栏出口没声明格 ⇒ 回落 runner_failed 是**对的**（平台护栏出口
            # 默认就是「命令从未执行」）。已有测试钉住这条
            # （test_fact_positions_coverage.py:181/263）。
            return ToolResult.blocked_err(err_msg, fact="runner_failed", **_kw)
        # ⚠ **非法组合**：blocked=True 配**调用方成因**的格（bad_args /
        # command_failed）。此处**原先**是静默改写成 runner_failed —— 那是**反向**
        # 的（把「你的参数错」说成「命令从未执行」），正是 L6/L19 要治的方向，
        # 而且没有任何日志。
        # 2026-09-14 收敛：与 `_wrap_routed_background_result` 用**同一条策略** ——
        # **保留事实位、去掉 blocked**（参数错不是平台护栏拒绝），绝不改写 fact。
        # 今天该路径不可达（动态 blocked_err 构造点均已守卫），但三条处置曾经各不
        # 相同、其中一条静默反向 —— 这正是"看似有守卫比没有守卫更危险"的形态。
        log.warning(
            "blocked_fact_not_blockable",
            fact=_fact,
            action=(
                "blocked=True 配了调用方成因的 fact ⇒ 保留 fact、去掉 blocked"
                "（不再静默改写成 runner_failed）。护栏出口确实没声明格时才回落。"
            ),
        )
        return ToolResult.err(err_msg, fact=_fact, **_kw)
    return ToolResult.err(err_msg, fact=_fact, **_kw)


# ── pwsh 工具（DSH_33 P0：声明式双 Consumer，不做方言转译）──────────
# 描述是**方言契约**本身：说清「无状态 / workdir 而非 cd / 原生 Windows 路径 /
# $env:NAME / 非零退出如何呈现 / 输出截断与落盘」，模型据此一次写对，而不是
# 靠平台猜译 unix 惯用法（猜译 = 静默错译，见 _map_unix_to_pwsh docstring）。
PWSH_TOOL_DESCRIPTION = (
    "Execute a PowerShell command (pwsh -Command) in YOUR workspace "
    "(worktree if you have one) and return stdout/stderr. "
    "This is the PowerShell-dialect sibling of bash — same permissions, "
    "same sandbox, same truncation. Use it whenever you want PowerShell "
    "semantics (cmdlets, objects, $env:, -replace) instead of writing unix "
    "commands that this platform would have to translate.\n"
    "Dialect contract:\n"
    "- Each call is a FRESH pwsh process: no state (cwd, variables, "
    "functions, imported modules) persists between calls. Do not `cd` "
    "expecting the next call to stay there — the command already starts in "
    "your workspace.\n"
    "- Paths use native Windows form (D:\\proj\\src\\app.py) or "
    "workspace-relative form. Never invent /workspace and never strip "
    "backslashes (D:PC_AI... is invalid).\n"
    "- Read environment variables with $env:NAME (not $NAME). Set them with "
    "$env:NAME='value' — export is bash syntax and does not exist here.\n"
    "- Your command reaches pwsh with a **narrow auto-translation** applied "
    "first for two closed sets: a trailing `| head/-n N` or `| tail/-n N` or "
    "`| wc -l` pipe tail, and a whole-command `head -N f` / `tail -N f` / "
    "`wc -l f`. Everything else is **verbatim** — sed/awk/grep/xargs do not "
    "exist in pwsh: use -replace, Select-String, ForEach-Object.\n"
    "- Call an external program with the & call operator when the name or "
    "path is quoted: & \"python\" \"script.py\" (bare \"python\" \"x.py\" is "
    "a pwsh ParserError).\n"
    "- Non-zero exits are reported as `Exit code: N` with the stdout/stderr "
    "tails; check it on every result before moving on. A blocked=true result "
    "is a platform denial (self-destructive command, sensitive file, "
    ".hiveweave system dir) — read the reason and change approach, do not "
    "retry variations.\n"
    "- Long output is truncated to head+tail; the full text is saved and the "
    "path is reported.\n"
    "Long scripts: background=true returns waiting_on — then "
    "commit_turn(waiting); woken with [BASH DONE] / [BASH FAILED]; stop with "
    "job_kill. Do not use background=true for vite / npm run dev / uvicorn / "
    "http.server (servers never finish, so waiting_on never fires) — prefer "
    "start_dev_server. Do not append & on a foreground command. "
    "Project-root tests / MAIN QA: use pwsh_main."
    + SANDBOX_TEMP_GUIDE
)


@tool(
    "pwsh",
    PWSH_TOOL_DESCRIPTION,
    requires_workspace=True,
    security_level="shell",
)
async def pwsh_tool(
    params: BashParams, agent_id: str, workspace: str
) -> ToolResult:
    """First-class PowerShell tool — verbatim pwsh, no dialect translation."""
    return await _shell_tool_impl(params, agent_id, workspace, dialect="pwsh")


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
    "pwsh_main",
    "Execute a PowerShell command at the PROJECT ROOT (shared MAIN), not "
    "your worktree. Same params as pwsh. Use this for milestone VERIFY "
    "tests, MAIN git/log, or anything that must see merged HEAD. Your own "
    "slice unit tests stay on pwsh (worktree). Platform does not rewrite "
    "pwsh cwd.",
    requires_workspace=True,
    security_level="shell",
)
async def pwsh_main_tool(
    params: BashParams, agent_id: str, workspace: str
) -> ToolResult:
    """pwsh at project root — agent chose MAIN explicitly (T3.2)."""
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    main_ws, err = await resolve_project_main_cwd(project_id)
    if not main_ws:
        return ToolResult.err(err)
    note = "\n\n[cwd=project root]"
    result = await pwsh_tool(params, agent_id, main_ws)
    return _with_cwd_note(result, note)


@tool(
    "run_command",
    "Executes a command and returns the output. Similar to bash but with "
    "explicit working directory support. Use for running scripts, builds, "
    "tests, or any system command. For reviewer test_run binding pass "
    "taskId; for custom validation scripts whose names don't match "
    "test_/verify_/check_ patterns pass testEvidence=true to force the "
    "attestation (exit 0 = green).",
    requires_workspace=True,
    security_level="shell",
)
async def run_command_tool(params: RunCommandParams, agent_id: str, workspace: str) -> ToolResult:
    """Execute a command with explicit cwd."""
    from hiveweave.services.process_registry import prepare_spawn_command
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    cmd, _env, reserved_err, inj_meta = prepare_spawn_command(
        params.command, project_id=project_id
    )
    if reserved_err:
        # L6（2026-09-11）：改判 **bad_args** —— 模型换个 3000+ 端口即可通过，
        # 判 runner_failed 会让它收到「不是你的 bug」并原地重撞同一端口。
        # ⚠ 2026-09-14 修**真崩溃**：L6 改判时只把 fact 换成 bad_args、**没换构造器**，
        # 而 `blocked_err` 的 `__post_init__` 不变式会抛
        # `ValueError: blocked=True cannot carry fact='bad_args'`（bad_args 是调用方
        # 成因，被 `_BLOCKED_FACT_KINDS` 明确排除 —— 标 blocked 会向 agent 发
        # 「不是你的 bug」信号并原地重撞，正是 L6 要治的病）。
        # 后果：agent 跑 `--port 4000` 拿到的是 Python traceback 而不是端口指引；
        # 且 executor 的 `except` 会早退成**无 fact 的裸 dict**（漏斗旁路）。
        # ⇒ `ToolResult.err` 的 docstring 本来就写明「bad_args 走这里而**不是**
        # blocked_err」：这次只是让代码回到它自己声明的契约上。
        return ToolResult.err(reserved_err, fact="bad_args")

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
    # F2：平台改写后的最终命令回显 —— 让 Agent 看得见这双手
    if inj_meta:
        note = inj_meta.get("note") or ""
        if result.get("output"):
            result["output"] = f"{result['output']}\n\n{note}"
        elif result.get("error"):
            result["error"] = f"{result['error']}\n\n{note}"
        result["injection_applied"] = bool(inj_meta.get("injected"))
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
                declared_test=bool(getattr(params, "test_evidence", False)),
            )
    except Exception as _att_err:
        log.warning("bash_attest_issue_failed", error=str(_att_err))
    meta = _attestation_fields_from_note(attest_note)
    banner = meta.get("banner") or ""
    public = _attestation_public_extra(meta)
    # F4/F7：工具执行事实位透传到 ToolResult
    _ff = {
        k: result.get(k)
        for k in _SHELL_FACT_FLAG_KEYS
        if result.get(k) is not None
    }
    return _shell_tool_result(
        success=bool(result.get("success")),
        blocked=bool(result.get("blocked")),
        output=out,
        error=result.get("error") or "Command failed",
        banner=banner,
        suffix=attest_note,
        public=public,
        streak_hint=_streak_hint,
        fact_flags=_ff or None,
    )

