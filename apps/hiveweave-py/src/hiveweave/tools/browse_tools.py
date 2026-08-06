"""browse — gstack Chromium CLI wrapper for agent UI/E2E testing."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.config import resolve_browse_bin, settings
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# Minimum structured observation length for assert_visual (not free-text scrape —
# the field itself is the structured assertion evidence).
_VISUAL_OBSERVED_MIN = 40

# gstack `js` evaluates its arg as an inline expression. Expressions up to this
# length are passed directly; larger ones are materialised to a tempfile and run
# via `eval` to stay clear of Windows argv limits.
_INLINE_JS_DIRECT_MAX = 4000


def _is_path_stub_observed(observed: str, screenshot_path: str) -> bool:
    """True when ``observed`` is basically the path/basename (language-agnostic).

    No NL keyword lists — only structured equality / path-token dominance.
    """
    import re as _re

    o = (observed or "").strip()
    if len(o) < _VISUAL_OBSERVED_MIN:
        return True
    path_norm = screenshot_path.replace("\\", "/").strip().lower()
    o_norm = o.replace("\\", "/").strip().lower()
    basename = Path(screenshot_path).name.lower() if screenshot_path else ""
    if path_norm and (
        o_norm == path_norm or o_norm.rstrip("/") == path_norm.rstrip("/")
    ):
        return True
    if basename and o_norm == basename:
        return True
    if basename and basename in o_norm:
        remainder = o_norm.replace(basename, "")
        if path_norm:
            remainder = remainder.replace(path_norm, "")
        remainder = _re.sub(r"[\s./\\:_\-]+", "", remainder)
        if len(remainder) < 16:
            return True
    return False


class BrowseParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    args: list[str] | None = Field(
        default=None,
        description=(
            "gstack browse CLI argv, e.g. [\"goto\", \"http://127.0.0.1:3000\"] "
            "or [\"snapshot\", \"-i\"] or [\"screenshot\", \"evidence/bug.png\"]. "
            "Prefer this over free-form shell."
        ),
    )
    command: str | None = Field(
        default=None,
        description=(
            "Alternative to args: space-separated browse subcommand "
            '(e.g. \'goto http://127.0.0.1:3000\'). Ignored if args is set.'
        ),
    )
    timeout_sec: int = Field(
        default=60,
        alias="timeoutSec",
        description=(
            "Max seconds to wait for the browse command "
            "(default 60; click/wait floored at 30)."
        ),
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="Optional task id to bind browse_e2e attestation evidence.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


def _parse_argv(params: BrowseParams) -> list[str] | None:
    if params.args:
        return [str(a) for a in params.args if str(a).strip()]
    if params.command and params.command.strip():
        try:
            return shlex.split(params.command.strip(), posix=os.name != "nt")
        except ValueError:
            return params.command.strip().split()
    return None


async def _resolve_task_id(
    project_id: str,
    agent_id: str,
    explicit: str | None,
    command: str | None = None,
) -> tuple[str | None, str]:
    """Bind browse evidence to a task.

    TEST18 P0-2: 与 bash 的 _resolve_test_attestation_task_id 同源 —
    多 open VERIFY 时 refuse 并列出候选（而非错绑第一个 active），
    避免 worktree 上的 browse_e2e 绑定到错误任务。返回 (task_id, note)。
    """
    if explicit:
        return explicit, ""
    try:
        from hiveweave.tools.bash import _resolve_test_attestation_task_id

        return await _resolve_test_attestation_task_id(
            project_id, agent_id, None, command=command
        )
    except Exception:
        return None, ""


async def _maybe_git_commit(workspace: str) -> str | None:
    if not workspace or not Path(workspace).is_dir():
        return None
    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workspace,
            **windows_no_window_kwargs(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0 and out:
            return out.decode("utf-8", errors="replace").strip()[:40] or None
    except Exception:
        pass
    return None


def _screenshot_path_from_argv(argv: list[str]) -> str | None:
    """Extract output path from ``screenshot [path]`` argv.

    Supports both ``screenshot path.png`` and
    ``screenshot --selector canvas path.png``.
    """
    if not argv:
        return None
    if (argv[0] or "").lower().replace("-", "_") != "screenshot":
        return None
    # Last non-flag positional that looks like a path wins.
    candidates: list[str] = []
    i = 1
    while i < len(argv):
        tok = str(argv[i]).strip()
        if tok in ("--selector", "-s") and i + 1 < len(argv):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok:
            candidates.append(tok)
        i += 1
    if candidates:
        return candidates[-1]
    return "screenshot.png"


def _browse_state_dir(agent_id: str | None) -> str | None:
    """Return an agent-scoped gstack state dir, or None if no agent_id.

    gstack keys its long-lived browser daemon (server-node) off
    ``BROWSE_STATE_FILE``: the same state file reuses the same Chromium
    instance, a different one starts a separate daemon. By pinning each
    agent to its own state dir under the Meta DB data path, every browser
    call from one agent hits the *same* server-node — instead of spawning a
    fresh Chromium per call (which leaked processes and caused OOM on the
    small 3.9GB test host).
    """
    if not agent_id:
        return None
    try:
        data_dir = Path(settings.get_meta_db_path()).parent
        state_dir = data_dir / "browse" / agent_id
        state_dir.mkdir(parents=True, exist_ok=True)
        return str(state_dir)
    except Exception as e:
        log.warning("browse_state_dir_failed", agent_id=agent_id, error=str(e))
        return None


def _browse_child_env(agent_id: str | None = None) -> dict[str, str]:
    """Env for gstack browse — headless, no sidebar PTY (no bun console popups).

    gstack's terminal-agent (``bun run .../terminal-agent.ts``) owns the
    interactive sidebar Terminal pane. Agents never use that pane; leaving it
    enabled on Windows repeatedly pops visible Terminal/bun windows, and the
    detached browse daemon keeps respawning them after the agent turn ends.

    ``GSTACK_TERMINAL_AGENT=0`` is the upstream-supported embedder switch
    (see gstack browse cli.ts / server.ts). Default both flags to on/disabled
    unless the operator already set them in the process environment.

    Per-agent reuse (fix OOM): when ``agent_id`` is given, pin ``BROWSE_STATE_FILE``
    to an agent-scoped state dir and set ``BROWSE_PARENT_PID=0`` so the server-node
    survives CLI exit (default watchdog would kill it per call). gstack then
    auto-recycles the daemon after ``BROWSE_IDLE_TIMEOUT`` of inactivity.
    """
    env = {**os.environ}
    env.setdefault("GSTACK_HEADLESS", "1")
    env.setdefault("GSTACK_TERMINAL_AGENT", "0")
    state_dir = _browse_state_dir(agent_id)
    if state_dir:
        env["BROWSE_STATE_FILE"] = str(Path(state_dir) / "browse.json")
        env["BROWSE_PARENT_PID"] = "0"
        # Keep the daemon alive across an agent's work session; idle after
        # this window auto-shuts it down (prevents indefinite Chromium leak).
        env.setdefault("BROWSE_IDLE_TIMEOUT", str(2 * 60 * 60 * 1000))
    return env


_reaped_browse_orphans = False


def _reap_orphan_browse_daemons_once() -> int:
    """Kill leftover gstack browse daemons that still spawn terminal-agent.

    Idempotent per process: first browse call on Windows reaps orphans from
    prior runs that were started *without* ``GSTACK_TERMINAL_AGENT=0`` (their
    watchdog would keep popping bun windows even after we set the env for
    new children). Returns number of processes signaled.

    Only acts when a ``terminal-agent`` process is present — a healthy
    headless ``server-node`` (no PTY agent) is left alone.
    """
    global _reaped_browse_orphans
    if _reaped_browse_orphans or os.name != "nt":
        return 0
    _reaped_browse_orphans = True

    try:
        import subprocess
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        # Only reap when the popup culprit (terminal-agent) is alive.
        ps_agents = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ("
            "$_.CommandLine -match 'gstack[\\\\/]browse[\\\\/]src[\\\\/]terminal-agent\\.ts' "
            "-or ($_.Name -eq 'bun.exe' -and $_.CommandLine -match 'terminal-agent')"
            ") } | Select-Object -ExpandProperty ProcessId"
        )
        listed_agents = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_agents],
            capture_output=True,
            text=True,
            timeout=15,
            **windows_no_window_kwargs(),
        )
        agent_pids = [
            int(line.strip())
            for line in (listed_agents.stdout or "").splitlines()
            if line.strip().isdigit()
        ]
        if not agent_pids:
            return 0

        # Also take down server-node parents — otherwise their watchdog
        # respawns terminal-agent under the old ownsTerminalAgent=true boot.
        ps_all = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ("
            "$_.CommandLine -match 'gstack[\\\\/]browse[\\\\/](dist[\\\\/]server-node\\.mjs|src[\\\\/]terminal-agent\\.ts)' "
            "-or ($_.Name -eq 'bun.exe' -and $_.CommandLine -match 'terminal-agent')"
            ") } | Select-Object -ExpandProperty ProcessId"
        )
        listed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_all],
            capture_output=True,
            text=True,
            timeout=15,
            **windows_no_window_kwargs(),
        )
        pids: list[int] = []
        for line in (listed.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        if not pids:
            return 0

        # taskkill /T to sweep chrome-headless children of server-node.
        killed = 0
        for pid in pids:
            try:
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    **windows_no_window_kwargs(),
                )
                if r.returncode == 0:
                    killed += 1
            except (OSError, subprocess.TimeoutExpired):
                pass
        if killed:
            log.info("browse_orphan_daemons_reaped", count=killed, pids=pids[:20])
        return killed
    except Exception:
        return 0


async def browse_exec(
    argv: list[str],
    workspace: str,
    *,
    timeout_sec: int = 60,
    agent_id: str | None = None,
) -> tuple[int, str, str]:
    """Run gstack browse CLI. Returns ``(exit_code, stdout, stderr)``.

    Raises ``FileNotFoundError`` / ``OSError`` on spawn failure.
    On timeout returns exit_code=-1 and an error message in stderr.

    ``agent_id`` pins the gstack daemon to an agent-scoped state dir so the
    agent reuses one browser instance across calls instead of spawning a
    fresh Chromium per call (see _browse_child_env).
    """
    bin_path = resolve_browse_bin()
    if not bin_path:
        raise FileNotFoundError("gstack browse binary not found")

    # Drop prior-run daemons that still pop bun Terminal windows.
    await asyncio.to_thread(_reap_orphan_browse_daemons_once)

    argv = [str(a) for a in argv]
    # gstack contract: `js`/`evaluate` = inline expression, `eval` = file path.
    # Map `evaluate` → `js`; leave `eval` as-is (file semantics). Do NOT collapse
    # `eval` to `js` — `_materialize_inline_js` routes inline snippets to the
    # correct subcommand, and mapping `eval`→`js` fed a tempfile path to `js`,
    # which evaluated the *path string* instead of the code (evaluate 1+1 crash).
    if argv and (argv[0] or "").lower() == "evaluate":
        argv = ["js", *argv[1:]]

    # TEST6 P0-1: gstack ``js`` evaluates its arg as an inline expression.
    # Materialise only for `eval` (which reads a file), never for `js`.
    # P0/R3: _materialize_inline_js may spawn a tempfile (hw_browse_*.js) for
    # very large inline snippets; track it so browse_exec can unlink it after
    # the subprocess exits — otherwise %TEMP% accumulates one file per large
    # browse call (no GC anywhere in the repo).
    _tmp_path = _materialize_with_tmp(argv, workspace)
    argv = _tmp_path[0]
    _tmp_file = _tmp_path[1]

    timeout = max(5, min(int(timeout_sec or 60), 300))
    head = (argv[0] or "").lower().replace("-", "_") if argv else ""
    if head in (
        "click", "wait", "wait_for", "waitfor", "fill", "press",
        "js", "eval", "evaluate",
    ):
        timeout = max(30, timeout)

    cmd = [str(bin_path), *argv]
    cwd = workspace if workspace and Path(workspace).is_dir() else None
    from hiveweave.util.win_subprocess import windows_no_window_kwargs

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_browse_child_env(agent_id),
            **windows_no_window_kwargs(),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, "", f"browse timed out after {timeout}s: {' '.join(argv)}"

        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        code = proc.returncode if proc.returncode is not None else -1
        return code, stdout, stderr
    finally:
        if _tmp_file:
            try:
                os.unlink(_tmp_file)
            except OSError:
                pass


def _materialize_inline_js(argv: list[str], workspace: str) -> list[str]:
    """Route `js`/`eval`/`evaluate` args to the correct gstack subcommand.

    gstack contract (read-commands.ts):
      js <expr>   -> evaluates the arg as an inline expression
      eval <file> -> reads a file path, then evaluates its contents

    The old code materialised every inline snippet to a tempfile and passed
    `js <tempfile>` — gstack then evaluated the *tempfile path string* instead
    of the code, so `evaluate 1+1` returned the path (and game_run_case's probe
    JS was never executed). Now:
      - js/evaluate: pass the inline expression straight through.
      - eval: keep a real file, or materialise an inline snippet to a tempfile.
    A `js/evaluate` arg that happens to be an existing file is rerouted to
    `eval` so file-based usage still works with the correct semantics.
    """
    return _materialize_with_tmp(argv, workspace)[0]


def _materialize_with_tmp(argv: list[str], workspace: str) -> tuple[list[str], str | None]:
    """Like :func:`_materialize_inline_js` but also returns the tempfile path
    created by ``mkstemp`` (or ``None``), so the caller can unlink it after the
    subprocess exits (fixes the %TEMP% hw_browse_*.js leak — no GC elsewhere).
    """
    import tempfile

    if len(argv) < 2:
        return argv, None
    head = (argv[0] or "").lower()
    if head not in ("js", "eval", "evaluate"):
        return argv, None
    src = argv[1] or ""
    is_inline = head in ("js", "evaluate")

    # Real file (absolute or workspace-relative).
    candidates = [Path(src)]
    if workspace:
        candidates.append(Path(workspace) / src)
    for p in candidates:
        try:
            if p.is_file():
                return ["eval", str(p.resolve())], None
        except OSError:
            continue

    if is_inline:
        # Small expressions go straight through (gstack `js` evaluates them).
        # Very large / multi-line snippets risk Windows argv limits — route
        # them through a tempfile with `eval` (which reads the file).
        if len(src) <= _INLINE_JS_DIRECT_MAX:
            return ["js", src], None
    # `eval` expects a file path — materialise the inline snippet to a tempfile.
    try:
        fd, path = tempfile.mkstemp(prefix="hw_browse_", suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        return ["eval", path], path
    except OSError:
        return ["js", src], None


def browse_missing_bin_hint() -> str:
    hint = (
        "gstack browse binary not found. Build it once:\n"
        "  cd %USERPROFILE%\\.claude\\skills\\gstack && bun install && bun run build\n"
        "Or set HIVEWEAVE_BROWSE_BIN to the browse.exe path."
    )
    if settings.browse_bin:
        hint = (
            f"HIVEWEAVE_BROWSE_BIN={settings.browse_bin!r} is missing or not a file.\n"
            + hint
        )
    return hint


async def issue_browse_e2e_attestation(
    *,
    agent_id: str,
    workspace: str,
    argv: list[str],
    stdout: str,
    task_id: str | None = None,
    core_interaction: bool = False,
) -> str:
    """Create browse_e2e attestation; return note fragment (may be empty)."""
    try:
        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if not project_id:
            return ""
        from hiveweave.tools.bash import _is_under_or_same

        resolved_task, bind_note = await _resolve_task_id(
            project_id, agent_id, task_id, command=" ".join(argv)
        )
        if not resolved_task:
            return bind_note or ""
        # TEST18 P0-2: VERIFY 任务的 browse 证据必须在 main 工作区执行 —
        # dev server 起在哪是运行期事实，这里事后拒发并给出替代路径，
        # 而不是在 approve 时静默拒绝。
        try:
            from hiveweave.services.task import TaskService
            from hiveweave.services.worktree_review import project_main_workspace

            task = await TaskService().get_task(project_id, resolved_task)
            if task and TaskService._is_verify_task(task):
                main_ws = await project_main_workspace(project_id)
                if workspace and main_ws and not _is_under_or_same(workspace, main_ws):
                    return (
                        "\n\n[browse_e2e REJECTED] VERIFY 任务的 UI 证据必须在主"
                        f"工作区执行（当前 workspace={workspace!r} 非 main="
                        f"{main_ws!r}）。请让 coordinator/CEO（项目根==main）"
                        "执行 UI 验收，或由负责人 waive_attestation。"
                        + bind_note
                    )
        except Exception:
            pass
        commit = await _maybe_git_commit(workspace or "")
        cmd_url = " ".join(argv)[:500]
        if core_interaction:
            cmd_url = f"[core_interaction=1] {cmd_url}"
        att_id = await attestation_service.create(
            project_id,
            agent_id=agent_id,
            kind="browse_e2e",
            tool_call_id=str(uuid.uuid4()),
            task_id=resolved_task,
            command_or_url=cmd_url,
            exit_code=0,
            workspace=workspace or None,
            commit=commit,
            stdout_hash=hash_stdout(stdout),
            console_errors=0,
        )
        extra = " core_interaction=1" if core_interaction else ""
        return f"\n[attestation_id={att_id} kind=browse_e2e{extra}]{bind_note}"
    except Exception:
        return ""


@tool(
    "browse",
    "Drive a real Chromium browser via gstack browse (goto/click/fill/snapshot/"
    "screenshot/console/network/js/eval). Use js/eval for canvas MouseEvent "
    "injection when snapshot refs are insufficient. "
    "Prefer lookup_dev_server / start_dev_server for the app URL first. "
    "After screenshot the PNG pixels are injected into your next LLM turn — "
    "you MUST call assert_visual(observed, verdict) based on what you SEE "
    "(path-only evidence is rejected for UI submit). "
    "For H5/canvas games prefer game_run_case after goto. "
    "Example: browse(args=[\"goto\",\"http://127.0.0.1:3000\"]) then "
    "browse(args=[\"snapshot\",\"-i\"]). On success issues a browse_e2e attestation.",
    requires_workspace=True,
    security_level="shell",
)
async def browse_tool(
    params: BrowseParams, agent_id: str, workspace: str
) -> ToolResult:
    if not resolve_browse_bin():
        return ToolResult.err(browse_missing_bin_hint())

    argv = _parse_argv(params)
    if not argv:
        return ToolResult.err(
            'browse requires args or command. Example: '
            'args=["goto","http://127.0.0.1:3000"]'
        )

    # Soft guard: discourage attaching to the operator's daily profile URLs
    # that look like credential harvesting — still allow localhost / file / http(s).
    joined = " ".join(argv).lower()
    if "cookie-import-browser" in joined and "--domain" not in joined:
        return ToolResult.err(
            "cookie-import-browser without --domain is blocked for agents. "
            "Use setup-browser-cookies skill manually, or pass an explicit --domain."
        )

    head = (argv[0] or "").lower().replace("-", "_")
    try:
        code, stdout, stderr = await browse_exec(
            argv, workspace, timeout_sec=params.timeout_sec or 60, agent_id=agent_id
        )
    except FileNotFoundError:
        return ToolResult.err(browse_missing_bin_hint())
    except OSError as e:
        return ToolResult.err(f"browse spawn failed: {e}")

    if code != 0:
        parts = [f"browse exit={code}: {' '.join(argv)}"]
        if stdout:
            parts.append(stdout[-4000:])
        if stderr:
            parts.append(f"stderr:\n{stderr[-2000:]}")
        return ToolResult.err("\n".join(parts))

    out = stdout or "(no output)"
    if stderr:
        out = f"{out}\n--- stderr ---\n{stderr}"

    core_interaction = head in ("js", "eval", "evaluate")
    attest_note = await issue_browse_e2e_attestation(
        agent_id=agent_id,
        workspace=workspace,
        argv=argv,
        stdout=out,
        task_id=params.task_id,
        core_interaction=core_interaction,
    )

    extra_fields: dict[str, Any] = {}
    shot_rel = _screenshot_path_from_argv(argv)
    if shot_rel:
        from hiveweave.services.vision import (
            load_image_for_llm,
            resolve_screenshot_path,
        )

        shot_path = resolve_screenshot_path(workspace, shot_rel)
        img = load_image_for_llm(shot_path) if shot_path else None
        if img:
            extra_fields["images"] = [img]
            extra_fields["screenshot_path"] = img.get("path") or str(shot_path)
            out = (
                f"{out}{attest_note}\n"
                "[VISION] Screenshot pixels are attached to this tool result "
                "for your next turn. Inspect the image (not the path). Then call "
                "assert_visual(screenshotPath=..., observed=\"what you see\", "
                "verdict=\"pass\"|\"fail\") — UI submit requires visual_check; "
                "a bare screenshot file path is NOT enough."
            )
            return ToolResult.ok(out, **extra_fields)

        out = (
            f"{out}{attest_note}\n"
            "[VISION] Screenshot file could not be loaded into multimodal "
            f"context (path={shot_path}). Re-take screenshot or check path; "
            "assert_visual still required for UI evidence."
        )
        return ToolResult.ok(out)

    return ToolResult.ok(out + attest_note)


class AssertVisualParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    screenshot_path: str = Field(
        ...,
        alias="screenshotPath",
        description="Path to the PNG/JPEG produced by browse screenshot.",
        json_schema_extra={"aliases": ["screenshotPath", "screenshot_path", "path"]},
    )
    observed: str = Field(
        ...,
        description=(
            "What you SEE in the image pixels (UI state, text, layout, errors). "
            f"Min {_VISUAL_OBSERVED_MIN} chars. Path-only or 'looks fine' is rejected."
        ),
    )
    verdict: Literal["pass", "fail"] = Field(
        ...,
        description="pass if the screenshot satisfies the criterion; else fail.",
    )
    criteria: str | None = Field(
        default=None,
        description="Optional acceptance criterion / expected UI state this check covers.",
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="Optional task id to bind visual_check attestation.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


@tool(
    "assert_visual",
    "Record a visual assertion AFTER browse(screenshot). The screenshot pixels "
    "were injected into context — describe what you actually see, then "
    "verdict=pass|fail. Creates a visual_check attestation required for "
    "ui_browser_e2e submit. File existence alone is not evidence.",
    requires_workspace=True,
    security_level="standard",
)
async def assert_visual_tool(
    params: AssertVisualParams, agent_id: str, workspace: str
) -> ToolResult:
    from hiveweave.services.vision import load_image_for_llm, resolve_screenshot_path

    observed = (params.observed or "").strip()
    if len(observed) < _VISUAL_OBSERVED_MIN:
        return ToolResult.err(
            f"assert_visual.observed must be >= {_VISUAL_OBSERVED_MIN} chars "
            "describing what you SEE in the image (not the file path). "
            "Example: 'Level select shows 3 cards; Start button bottom-right; "
            "no console-error overlay.'"
        )

    shot = resolve_screenshot_path(workspace, params.screenshot_path)
    if shot is None:
        return ToolResult.err(
            f"Screenshot path rejected or outside workspace: "
            f"{params.screenshot_path!r}. Use a path under your worktree "
            f"(e.g. evidence/flow.png)."
        )
    if not shot.is_file():
        return ToolResult.err(
            f"Screenshot not found: {params.screenshot_path!r}. "
            "Re-run browse(args=[\"screenshot\", \"evidence/...png\"]) first."
        )

    if _is_path_stub_observed(observed, str(shot)) or _is_path_stub_observed(
        observed, params.screenshot_path
    ):
        return ToolResult.err(
            "assert_visual.observed looks like a path stub. Describe "
            "visible UI content (labels, layout, errors) from the image pixels — "
            "not the file path."
        )

    img = load_image_for_llm(shot)
    if img is None:
        return ToolResult.err(
            f"Could not load screenshot for vision context: {shot}. "
            "File missing, not an image, or exceeds size cap."
        )

    attest_note = ""
    att_id = None
    try:
        from hiveweave.services.attestation import (
            VISUAL_CHECK_KIND,
            attestation_service,
            hash_stdout,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if project_id:
            task_id, bind_note = await _resolve_task_id(
                project_id, agent_id, params.task_id
            )
            # TEST18 P0-2: 绑定失败（多 open VERIFY 等）时拼 note 且不创建
            # 未绑定 attestation — 否则 LLM 看到 [attestation_id=...] 以为
            # 证据已录，approve 时 baseline gate 按 task_id 查不到，白浪费一轮。
            if not task_id and bind_note:
                return ToolResult.ok(
                    f"\n[visual_check NOT ISSUED] 未绑定任务，不生成证据。{bind_note}",
                    att_id=None,
                )
            # TEST18 P0-2: VERIFY 任务的 visual_check 证据同 browse_e2e —
            # 必须在 main 工作区执行，否则拒发（baseline gate 的 kind 查询
            # 含 visual_check，留这个旁路等于白堵）。
            if task_id:
                try:
                    from hiveweave.services.task import TaskService
                    from hiveweave.services.worktree_review import (
                        project_main_workspace,
                    )
                    from hiveweave.tools.bash import _is_under_or_same

                    _task = await TaskService().get_task(project_id, task_id)
                    if _task and TaskService._is_verify_task(_task):
                        _main_ws = await project_main_workspace(project_id)
                        if workspace and _main_ws and not _is_under_or_same(
                            workspace, _main_ws
                        ):
                            return ToolResult.ok(
                                "\n[visual_check REJECTED] VERIFY 任务的 UI 证据"
                                "必须在主工作区执行（当前非 main）。请让 "
                                "coordinator/CEO 执行 UI 验收，或由负责人 "
                                "waive_attestation。",
                                att_id=None,
                            )
                except Exception:
                    pass
            commit = await _maybe_git_commit(workspace or "")
            payload = {
                "kind": VISUAL_CHECK_KIND,
                "screenshot_path": str(shot),
                "observed": observed,
                "verdict": params.verdict,
                "criteria": (params.criteria or "").strip() or None,
            }
            import json as _json

            blob = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
            att_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind=VISUAL_CHECK_KIND,
                tool_call_id=str(uuid.uuid4()),
                task_id=task_id,
                command_or_url=(
                    f"assert_visual:{params.verdict}:{shot.name}"
                )[:500],
                exit_code=0 if params.verdict == "pass" else 1,
                workspace=workspace or None,
                commit=commit,
                stdout_hash=hash_stdout(blob),
                stdout=blob,
                console_errors=0,
            )
            attest_note = (
                f"\n[attestation_id={att_id} kind={VISUAL_CHECK_KIND} "
                f"verdict={params.verdict}]"
            )
    except Exception as e:
        return ToolResult.err(f"assert_visual attestation failed: {e}")

    out = (
        f"Visual check recorded: verdict={params.verdict}.\n"
        f"screenshot={shot}\n"
        f"observed={observed[:500]}"
        f"{attest_note}\n"
        "[VISION] Image re-attached — confirm your observation still matches "
        "the pixels before submit_task."
    )
    return ToolResult.ok(
        out,
        images=[img],
        screenshot_path=str(shot),
        attestation_id=att_id,
        verdict=params.verdict,
    )
