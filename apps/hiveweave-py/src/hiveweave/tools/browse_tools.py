"""browse — agent-browser (Vercel Labs) Chromium CLI adapter for agent UI/E2E testing.

The tool-facing contract is "browse subcommand argv" (goto/snapshot/click/…).
``_map_ab_argv`` translates that gstack-style surface onto the agent-browser
CLI; only the subcommand layer changes, so both the dev (node_modules) and
packaged (Electron resources) states share one code path.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.config import resolve_browse_bin, settings
from hiveweave.conversation.token_utils import TOOL_OUTPUT_MAX_BYTES
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# Minimum structured observation length for assert_visual (not free-text scrape —
# the field itself is the structured assertion evidence).
_VISUAL_OBSERVED_MIN = 40


async def agent_is_look_only_browser(agent_id: str) -> bool:
    """CEO (BROWSE, no test duty) looks; do not nudge assert_visual / stamp."""
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.policy import has_visual_test_duty

        agent = await meta_db.get_agent_by_id(agent_id)
        if not agent:
            return False
        return not has_visual_test_duty(agent)
    except Exception:
        return False


def screenshot_followup_text(shot_display: str, *, look_only: bool) -> str:
    """Post-screenshot next-step copy. Look-only must not tell CEO to attest."""
    if look_only:
        return (
            "[VISION] Screenshot pixels are attached for inspection "
            f"(path={shot_display}). You are looking, not testing. "
            "Do not call assert_visual / game_run_case. Formal VERIFY "
            "stays with QA; use look_at_image on their evidence if needed."
        )
    return (
        "[VISION] Screenshot pixels are attached to this tool result "
        "for your next turn. Inspect the image (not the path). "
        f"Screenshot saved at: {shot_display}\n"
        "Then call "
        f"assert_visual(screenshotPath=\"{shot_display}\", "
        "observed=\"describe what you see: labels/layout/errors, "
        "40+ chars\", "
        "verdict=\"pass\"|\"fail\") — UI submit requires visual_check; "
        "a bare screenshot file path is NOT enough."
    )

# agent-browser `eval <js>` is a direct argv expression. Direct form is used
# for short snippets; base64 (-b) avoids shell/argv escaping for the rest;
# --stdin carries scripts too large for the Windows argv limit (32767).
# The size guards compare UTF-8 byte length: base64 inflates by 4/3, and
# multibyte sources would otherwise blow the argv cap unnoticed.
_EVAL_DIRECT_MAX = 1024
_EVAL_B64_MAX = 24000  # 24k UTF-8 bytes → ~32k base64, clear of the argv cap

# 快照短契约 —— snapshot ≥50KB 时只把结构化摘要返回给 agent，
# 完整快照落盘（镜像 executor 的 .hiveweave/tool_outputs/ 约定）并回传句柄。
_SNAPSHOT_CONTRACT_TEXT_CHARS = 1_500  # 可见内容摘要预算（1-2K 字符内）
_SNAPSHOT_LINE_MAX_CHARS = 500  # 单行超长（JSON 等）不能击穿摘要预算


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
            "browse CLI argv, e.g. [\"goto\", \"http://127.0.0.1:3000\"] "
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
    """Extract explicit output path from ``screenshot [path]`` argv.

    Supports both ``screenshot path.png``, ``screenshot --selector canvas path.png``
    and the agent-browser positional form ``screenshot canvas path.png``
    (also under the ``shoot`` alias). Returns None when the caller omitted a
    path — callers must inject an explicit workspace-relative path before spawn.
    """
    if not argv:
        return None
    if (argv[0] or "").lower().replace("-", "_") not in ("screenshot", "shoot"):
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
    return None


def _screenshot_agent_dir(agent_id: str | None) -> str:
    raw = str(agent_id or "agent").strip() or "agent"
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")
    if not safe:
        return "agent"
    compact = safe.replace("-", "")
    if len(compact) >= 32:
        return compact[:8]
    return safe[:40]


def default_screenshot_relpath(
    agent_id: str | None, *, now_ms: int | None = None
) -> str:
    """Workspace-relative default for a screenshot with no caller path."""
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    return (
        f".hiveweave/reports/{_screenshot_agent_dir(agent_id)}/shot-{stamp}.png"
    )


def ensure_screenshot_argv(
    argv: list[str],
    agent_id: str | None = None,
    *,
    now_ms: int | None = None,
) -> list[str]:
    """Always pass an explicit workspace-relative path to the screenshot CLI.

    HiveWeave must not rely on CLI cwd/tmp defaults. If the agent omitted a
    path, inject ``.hiveweave/reports/<agent>/shot-<ts>.png``.
    """
    if not argv:
        return list(argv)
    head = (argv[0] or "").lower().replace("-", "_")
    if head not in ("screenshot", "shoot"):
        return list(argv)
    if _screenshot_path_from_argv(argv):
        return list(argv)
    return [*argv, default_screenshot_relpath(agent_id, now_ms=now_ms)]


def _screenshot_missing_diagnostic(
    workspace: str, shot_rel: str
) -> str:
    """Locate where the screenshot actually landed instead of a vague error.

    With paths pinned absolute this should be an edge case, but when the file
    still isn't where we expect, search likely roots (workspace, OS temp, the
    agent-browser daemon cwd) by basename so the agent isn't left guessing.
    """
    name = Path(shot_rel).name
    import tempfile

    roots: list[str] = []
    if workspace and Path(workspace).is_dir():
        roots.append(workspace)
    tmp = tempfile.gettempdir()
    if tmp and Path(tmp).is_dir():
        roots.append(tmp)
    if os.getcwd():
        roots.append(os.getcwd())
    hits: list[str] = []
    checked = {r for r in roots if r}
    for root in checked:
        try:
            for p in Path(root).rglob(name):
                if p.is_file():
                    hits.append(str(p))
        except OSError:
            continue
    if hits:
        return (
            "Screenshot was written to: "
            f"{hits[0]} (agent-browser daemon cwd drifted from the "
            "workspace). Do not copy from there — re-take pinned to the "
            f"workspace: browse(args=[\"screenshot\", \"{shot_rel}\"])."
        )
    return (
        "The screenshot command exited 0 but no PNG was found under the "
        f"workspace ({shot_rel}) or the OS temp dir. The daemon may not "
        "have flushed the file — restart the session and re-take: "
        'browse(args=["restart"]), then '
        f'browse(args=["screenshot", "{shot_rel}"]).'
    )


def _workspace_rel_shot_display(
    workspace: str, shot_rel: str, shot_path: Path | None
) -> str:
    """Receipt path: workspace-relative posix, never a CLI tmp guess."""
    rel = (shot_rel or "").strip().replace("\\", "/")
    if rel and not Path(rel).is_absolute():
        return rel
    if shot_path and workspace:
        try:
            return (
                shot_path.resolve()
                .relative_to(Path(workspace).resolve())
                .as_posix()
            )
        except ValueError:
            pass
    return rel or (str(shot_path or "").replace("\\", "/"))


# gstack-style head → agent-browser head. Commands absent from this table
# pass through unchanged (agent-browser is a superset: click/fill/press/wait/
# snapshot/console/network/get/read/tab/frame/close/…).
_HEAD_ALIASES = {
    "goto": "open",
    "navigate": "open",
    "evaluate": "eval",
    "wait_for": "wait",
    "waitfor": "wait",
    "quit": "close",
    "exit": "close",
    "restart": "close",
    "reset": "close",
    "shoot": "screenshot",
}

# Session recycle: close the current agent-browser daemon; the next command
# respawns via AGENT_BROWSER_SESSION=hiveweave-<agent_id>.
BROWSE_RESTART_OK = (
    "browser session closed; next browse command starts fresh. "
    "If goto/eval keep timing out, call browse restart first."
)
BROWSE_RESTART_HINT = (
    'If goto/eval keep timing out, call browse(["restart"]).'
)

# Desktop default. goto always applies this so a leftover mobile session
# cannot stamp "desktop" screenshots (TEST_DSH_05). Narrow checks: viewport
# AFTER goto.
DEFAULT_VIEWPORT = (1280, 900)
_GOTO_HEADS = frozenset({"goto", "navigate", "open"})
_VIEWPORT_HEADS = frozenset({"viewport", "set_viewport"})
_VIEWPORT_MIN, _VIEWPORT_MAX = 32, 4096
VIEWPORT_USAGE = (
    'viewport needs width height. Example: args=["viewport","390","844"] '
    'or args=["viewport","390x844"]. Optional scale: '
    'args=["viewport","1280","900","2"].'
)


def parse_viewport_args(rest: list[str]) -> tuple[int, int, int | None] | None:
    """Parse ``390 844 [scale]`` or ``390x844 [scale]``. None if invalid."""
    if not rest:
        return None
    tokens = [str(t).strip() for t in rest if str(t).strip()]
    if not tokens:
        return None
    first = tokens[0].lower().replace("*", "x")
    w = h = None
    extra: list[str] = []
    if "x" in first:
        parts = first.split("x")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        w, h = int(parts[0]), int(parts[1])
        extra = tokens[1:]
    elif len(tokens) >= 2 and tokens[0].lstrip("-").isdigit() and tokens[1].lstrip("-").isdigit():
        w, h = int(tokens[0]), int(tokens[1])
        extra = tokens[2:]
    else:
        return None
    if w is None or h is None:
        return None
    if not (_VIEWPORT_MIN <= w <= _VIEWPORT_MAX and _VIEWPORT_MIN <= h <= _VIEWPORT_MAX):
        return None
    scale = None
    if extra:
        if not extra[0].isdigit():
            return None
        scale = int(extra[0])
        if scale < 1 or scale > 4:
            return None
    return w, h, scale


def _viewport_cli_argv(dims: tuple[int, int, int | None]) -> list[str]:
    w, h, scale = dims
    out = ["set", "viewport", str(w), str(h)]
    if scale is not None:
        out.append(str(scale))
    return out


def _is_viewport_command(argv: list[str]) -> bool:
    if not argv:
        return False
    head = (argv[0] or "").lower().replace("-", "_")
    if head in _VIEWPORT_HEADS:
        return True
    if head == "set" and len(argv) > 1 and str(argv[1]).lower() == "viewport":
        return True
    return False


def _viewport_rest(argv: list[str]) -> list[str]:
    head = (argv[0] or "").lower().replace("-", "_")
    if head in _VIEWPORT_HEADS:
        return [str(a) for a in argv[1:]]
    return [str(a) for a in argv[2:]]


def _map_ab_argv(argv: list[str], workspace: str) -> tuple[list[str], str | None]:
    """Translate browse (gstack-style) argv to agent-browser argv.

    Returns ``(mapped_argv, stdin_payload)`` — ``stdin_payload`` is set only
    for ``eval --stdin`` (script too large for argv). The tool-facing
    contract is unchanged: ``BrowseParams.args`` still carries browse
    subcommand argv; only the CLI underneath is swapped.
    """
    if not argv:
        return [], None
    head = (argv[0] or "").lower().replace("-", "_")
    rest = [str(a) for a in argv[1:]]

    if head in ("js", "eval", "evaluate"):
        return _eval_argv(head, rest, workspace)
    if head in ("screenshot", "shoot"):
        return _screenshot_argv(rest), None
    if _is_viewport_command(argv):
        dims = parse_viewport_args(_viewport_rest(argv))
        if dims is None:
            return ["set", "viewport"], None
        return _viewport_cli_argv(dims), None

    head = _HEAD_ALIASES.get(head, head)
    return [head, *rest], None


def _eval_argv(head: str, rest: list[str], workspace: str) -> tuple[list[str], str | None]:
    """agent-browser eval semantics: inline JS only (direct / -b / --stdin).

    gstack split js (inline) vs eval (file path). agent-browser evaluates
    expressions; file-based usage keeps working by reading the file content
    here. Small snippets go direct, medium ones base64-encoded, oversized
    ones via stdin.
    """
    if not rest:
        return ["eval"], None
    src = rest[0]
    if head in ("js", "eval", "evaluate"):
        candidates = [Path(src)]
        if workspace:
            candidates.append(Path(workspace) / src)
        for p in candidates:
            try:
                if p.is_file():
                    src = p.read_text(encoding="utf-8", errors="replace")
                    break
            except OSError:
                continue
    src = src or ""
    if len(src.encode("utf-8")) <= _EVAL_DIRECT_MAX and not src.lstrip().startswith("-"):
        return ["eval", src], None
    if len(src.encode("utf-8")) <= _EVAL_B64_MAX:
        b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
        return ["eval", "-b", b64], None
    return ["eval", "--stdin"], src


def _screenshot_argv(rest: list[str]) -> list[str]:
    """screenshot — agent-browser takes ``[selector] [path]`` positionals.

    gstack used ``--selector <sel>``; translate the flag into the first
    positional (``screenshot canvas evidence/x.png``). Other flags
    (--full/-f, --annotate) pass through unchanged.
    """
    out: list[str] = ["screenshot"]
    selector: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--selector", "-s") and i + 1 < len(rest):
            selector = rest[i + 1]
            i += 2
            continue
        out.append(tok)
        i += 1
    if selector is not None:
        # Insert the selector right after the subcommand; the path remains
        # the last positional (flags may appear in between).
        out.insert(1, selector)
    return out


def _inject_shot_abs_path(workspace: str, shot_rel: str) -> str:
    """Resolve a workspace-relative screenshot path to absolute.

    agent-browser persists screenshots relative to its own cwd. The CLI is
    spawned with ``cwd=workspace``, but the long-lived daemon
    (AGENT_BROWSER_SESSION) reuses a session whose working directory can
    drift from the workspace — a relative path then lands in the daemon's
    cwd/tmp and browse_tool's is_file() check misses it. Pin the output to
    an absolute workspace path so the file always lands under the workspace.
    """
    if Path(shot_rel).is_absolute():
        return str(Path(shot_rel))
    if workspace and Path(workspace).is_dir():
        return str(Path(workspace).resolve() / shot_rel)
    return shot_rel


def _pin_shot_path(
    mapped: list[str], workspace: str, shot_rel: str
) -> list[str]:
    """Pin the CLI screenshot output path to an absolute workspace path.

    ``mapped`` is the agent-browser argv; agent-browser takes the output
    path as the last positional (see _screenshot_argv, which keeps the path
    last even with ``--selector``/flags). If the tail already equals the raw
    relative path, replace it; otherwise append the absolute path. Pinning
    abs makes the is_file() check in browse_tool trustworthy even when the
    daemon cwd drifts.
    """
    out = list(mapped)
    if not out:
        return out
    abs_shot = _inject_shot_abs_path(workspace, shot_rel)
    if out[-1] == shot_rel:
        out[-1] = abs_shot
    else:
        out.append(abs_shot)
    return out


def _browse_child_env(agent_id: str | None = None) -> dict[str, str]:
    """Env for agent-browser — per-agent headless daemon session.

    agent-browser keeps one Chromium daemon per ``--session``; pinning each
    agent to its own session reuses the same browser instance across calls
    (the gstack BROWSE_STATE_FILE equivalent). The daemon idles out after
    ``AGENT_BROWSER_IDLE_TIMEOUT_MS`` so Chromium does not leak. Chrome
    itself is agent-browser's own concern: it auto-detects system
    Chrome/Brave/Playwright/Puppeteer, and `agent-browser install` fetches
    Chrome for Testing when none exists (no HIVE-side download logic).
    """
    env = {**os.environ}
    if agent_id:
        env["AGENT_BROWSER_SESSION"] = f"hiveweave-{agent_id[:40]}"
        env.setdefault("AGENT_BROWSER_IDLE_TIMEOUT_MS", str(2 * 60 * 60 * 1000))
    return env


async def _drain_pipe(stream: Any, buf: bytearray) -> None:
    """Read chunks from a subprocess pipe until EOF or cancellation."""
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            buf.extend(chunk)
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError):
        pass


async def _run_and_drain(
    proc: asyncio.subprocess.Process,
    stdin_payload: str | None,
    timeout: int,
) -> tuple[int, bytes, bytes]:
    """Wait for CLI exit, then grace-drain buffered stdout/stderr.

    agent-browser's daemon inherits the CLI's stdout/stderr pipe handles and
    stays alive after the CLI exits, so EOF never arrives — waiting for EOF
    (``communicate``) hangs until the outer timeout. Instead: wait for the
    CLI process to exit, then drain whatever the daemon-hold pipe still
    buffers (bounded by a short grace period). The outer timeout still
    guards genuinely hung commands.
    """
    out, err = bytearray(), bytearray()
    rt = asyncio.create_task(_drain_pipe(proc.stdout, out))
    rt_err = asyncio.create_task(_drain_pipe(proc.stderr, err))

    if stdin_payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_payload.encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), timeout=10)
        except (BrokenPipeError, OSError, ValueError):
            pass
        except asyncio.TimeoutError:
            pass
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    try:
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return -1, b"", b""

        # CLI exited; drain the tail concurrently (daemon may keep the pipes
        # open forever) under a single 2s grace cap.
        try:
            await asyncio.wait_for(asyncio.gather(rt, rt_err), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        return rc, bytes(out), bytes(err)
    finally:
        # Cleanup also on outer-task cancellation: kill a still-running CLI
        # and cancel any drain tasks still blocked on the daemon-held pipes.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        for t in (rt, rt_err):
            if not t.done():
                t.cancel()
        await asyncio.gather(rt, rt_err, return_exceptions=True)


async def browse_exec(
    argv: list[str],
    workspace: str,
    *,
    timeout_sec: int = 60,
    agent_id: str | None = None,
) -> tuple[int, str, str]:
    """Run agent-browser CLI. Returns ``(exit_code, stdout, stderr)``.

    Raises ``FileNotFoundError`` / ``OSError`` on spawn failure.
    On timeout returns exit_code=-1 and an error message in stderr.

    ``agent_id`` pins the agent-browser daemon to an agent-scoped session so
    the agent reuses one browser instance across calls instead of spawning a
    fresh Chromium per call (see _browse_child_env).
    """
    bin_path = resolve_browse_bin()
    if not bin_path:
        raise FileNotFoundError("agent-browser binary not found")

    # Native binaries ship without the exec bit on some unix installs.
    if os.name != "nt":
        try:
            bin_path.chmod(bin_path.stat().st_mode | 0o111)
        except OSError:
            pass

    argv = [str(a) for a in argv]
    argv = ensure_screenshot_argv(argv, agent_id)
    mapped, stdin_payload = _map_ab_argv(argv, workspace)

    timeout = max(5, min(int(timeout_sec or 60), 300))
    head = (mapped[0] or "").lower().replace("-", "_") if mapped else ""
    if head in (
        "click", "wait", "fill", "press", "type", "select", "eval",
        "close", "reload",
    ):
        timeout = max(30, timeout)

    # Screenshot paths land under the workspace; make sure the parent dir
    # exists (agents write to evidence/… which may not exist yet). Pin the
    # CLI output to an absolute path so the daemon's cwd drift cannot
    # misplace the file (see _inject_shot_abs_path).
    if head == "screenshot":
        shot_rel = _screenshot_path_from_argv(argv)
        if shot_rel:
            parent = Path(shot_rel).parent
            if workspace and parent and not parent.is_absolute():
                try:
                    (Path(workspace) / parent).mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            elif parent:
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            mapped = _pin_shot_path(mapped, workspace, shot_rel)

    cmd = [str(bin_path), *mapped]
    cwd = workspace if workspace and Path(workspace).is_dir() else None
    from hiveweave.util.win_subprocess import windows_no_window_kwargs

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
            cwd=cwd,
            env=_browse_child_env(agent_id),
            **windows_no_window_kwargs(),
        )
    except OSError:
        raise

    rc, stdout_b, stderr_b = await _run_and_drain(proc, stdin_payload, timeout)
    if rc == -1:
        return -1, "", (
            f"browse timed out after {timeout}s: {' '.join(argv)}. "
            f"{BROWSE_RESTART_HINT}"
        )

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    return rc, stdout, stderr


async def _hard_recycle_browser() -> str:
    """Hard-recycle the agent-browser session (kill daemon + orphan Chromium).

    A wedged agent-browser daemon makes subsequent browse commands hang even
    after the agent-browser ``close`` — the CLI cannot reach a wedged daemon,
    so ``restart`` (→close) alone never recovers, and the agent loops on
    timeouts until the official VERIFY visual gate gets waived. Killing the
    daemon process tree (``taskkill /T`` recurses its Chromium children) plus
    any orphaned agent-browser Chromium (identified by the
    ``agent-browser-chrome-`` user-data-dir marker) forces the next browse
    command to spawn a fresh session.

    Only agent-browser-owned processes are touched — never the operator's own
    Chrome (different user-data-dir) or the platform host. Sessions are
    per-agent (AGENT_BROWSER_SESSION) and respawn on the next browse call, so
    recycling is safe even if another agent's session is torn down too.
    """
    from hiveweave.config import agent_browser_bin_name
    from hiveweave.util.win_subprocess import windows_no_window_kwargs

    bin_name = agent_browser_bin_name()
    try:
        if os.name == "nt":
            # 1) Daemon + its Chromium tree (taskkill /T is recursive).
            p = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/IM", bin_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **windows_no_window_kwargs(),
            )
            try:
                await asyncio.wait_for(p.wait(), timeout=20)
            except asyncio.TimeoutError:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
            # 2) Reap orphaned agent-browser Chromium (daemon already gone).
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -match 'agent-browser-chrome-' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId "
                "-Force -ErrorAction SilentlyContinue }"
            )
            q = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **windows_no_window_kwargs(),
            )
            try:
                await asyncio.wait_for(q.wait(), timeout=20)
            except asyncio.TimeoutError:
                try:
                    q.kill()
                except ProcessLookupError:
                    pass
        else:
            # POSIX: pkill the daemon binary, then orphaned agent-browser
            # Chromium (user-data-dir marker) — SIGKILL on the daemon does not
            # propagate to reparented Chromium, mirroring the Windows step.
            for pat in (bin_name, "agent-browser-chrome-"):
                p = await asyncio.create_subprocess_exec(
                    "pkill", "-9", "-f", pat,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(p.wait(), timeout=20)
                except asyncio.TimeoutError:
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass
        # 3) Reap leaked Chromium profile dirs. agent-browser mints a fresh
        #    `agent-browser-chrome-<uuid>` user-data-dir per session and never
        #    cleans them (37 dirs / ~2.2GB after a month on TEST machines).
        #    After killing every daemon + Chromium above, any remaining profile
        #    is orphaned garbage — safe to delete here.
        import tempfile as _tempfile

        try:
            _profiles = list(
                Path(_tempfile.gettempdir()).glob("agent-browser-chrome-*")
            )
        except Exception:
            _profiles = []
        for _d in _profiles:
            try:
                import shutil as _shutil

                _shutil.rmtree(str(_d), ignore_errors=True)
            except Exception:
                pass
        return "browser session hard-recycled"
    except Exception as e:  # pragma: no cover - defensive
        log.warning("browse_hard_recycle_failed", error=str(e))
        return f"browser hard-recycle attempted ({e})"


async def _apply_default_viewport(
    workspace: str, agent_id: str | None, timeout_sec: int
) -> tuple[bool, str]:
    """Force desktop viewport after goto. Fail-open: goto still succeeds."""
    w, h = DEFAULT_VIEWPORT
    code, stdout, stderr = await browse_exec(
        ["set", "viewport", str(w), str(h)],
        workspace,
        timeout_sec=min(30, max(5, timeout_sec)),
        agent_id=agent_id,
    )
    if code != 0:
        err = (stderr or stdout or "").strip()[:200]
        return False, (
            f"viewport reset to {w}×{h} failed ({err or f'exit={code}'}). "
            f'Call browse(args=["viewport","{w}","{h}"]) before screenshot.'
        )
    return True, (
        f"viewport reset to {w}×{h} (desktop default). "
        "For narrow/mobile: "
        'browse(args=["viewport","390","844"]) then screenshot — '
        "do not set viewport before goto; goto always resets."
    )


def browse_missing_bin_hint() -> str:
    hint = (
        "agent-browser CLI binary not found. Install it once:\n"
        "  cd apps/web && pnpm add agent-browser\n"
        "(or `npm i -g agent-browser` for a global install)\n"
        "Or set HIVEWEAVE_BROWSE_BIN to the agent-browser binary path."
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
    screenshot_path: str | None = None,
) -> str:
    """Create browse_e2e attestation; return note fragment (may be empty).

    ``screenshot_path`` (abs) is merged into ``artifact_hashes`` so a
    reviewer can load the PNG via ``look_at_image(attestation_id=...)``.
    """
    needs_main = False
    try:
        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if not project_id:
            return ""
        from hiveweave.db import meta as meta_db
        from hiveweave.services.policy import has_visual_test_duty

        agent_row = await meta_db.get_agent_by_id(agent_id)
        if agent_row and not has_visual_test_duty(agent_row):
            return (
                "\n\n[look-only] No browse_e2e stamp — looking at the "
                "product is not test evidence. Formal VERIFY stays with QA.\n"
            )
        from hiveweave.tools.bash import _is_same_workspace

        resolved_task, bind_note = await _resolve_task_id(
            project_id, agent_id, task_id, command=" ".join(argv)
        )
        if not resolved_task:
            return bind_note or ""
        # TEST18 P0-2: VERIFY 任务的 browse 证据必须在 main 工作区执行 —
        # worktree 嵌在项目根下，「在根下面」会放行，必须目录等值（同 bash）。
        # Fail-closed: bound task that cannot be loaded / MAIN unresolved
        # must not stamp worktree evidence as MAIN.
        from hiveweave.services.task import TaskService
        from hiveweave.services.worktree_review import project_main_workspace
        from hiveweave.tools.bash import _task_needs_main_workspace

        try:
            task = await TaskService().get_task(project_id, resolved_task)
        except Exception as e:
            return (
                "\n\n[browse_e2e REJECTED] cannot load bound task for "
                f"MAIN check: {e}"
                + bind_note
            )
        if task is None:
            return (
                "\n\n[browse_e2e REJECTED] bound task not found — "
                "no attestation issued."
                + bind_note
            )
        needs_main = _task_needs_main_workspace(task)
        if needs_main:
            try:
                main_ws = await project_main_workspace(project_id)
            except Exception as e:
                return (
                    "\n\n[browse_e2e REJECTED] cannot resolve MAIN workspace: "
                    f"{e}"
                    + bind_note
                )
            if not main_ws:
                return (
                    "\n\n[browse_e2e REJECTED] cannot resolve MAIN workspace. "
                    "Use browse_main (project root)."
                    + bind_note
                )
            if not workspace or not _is_same_workspace(workspace, main_ws):
                return (
                    "\n\n[browse_e2e REJECTED] VERIFY UI "
                    f"evidence must run on MAIN (workspace={workspace!r} "
                    f"main={main_ws!r}). Use browse_main (project root), "
                    "not browse (your worktree)."
                    + bind_note
                )
        commit = await _maybe_git_commit(workspace or "")
        cmd_url = " ".join(argv)[:500]
        if core_interaction:
            cmd_url = f"[core_interaction=1] {cmd_url}"
        artifact_hashes: dict[str, str] | None = None
        shot = (screenshot_path or "").strip()
        if shot:
            artifact_hashes = {"screenshot_path": shot}
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
            artifact_hashes=artifact_hashes,
            console_errors=0,
        )
        extra = " core_interaction=1" if core_interaction else ""
        return f"\n[attestation_id={att_id} kind=browse_e2e{extra}]{bind_note}"
    except Exception as e:
        if needs_main:
            return (
                "\n\n[browse_e2e REJECTED] attestation failed after MAIN "
                f"check: {e}"
            )
        return ""


def _contract_snapshot_output(
    output: str, agent_id: str, workspace: str
) -> str:
    """大 snapshot 输出收成短契约返回。

    阈值复用 TOOL_OUTPUT_MAX_BYTES（50KB）：未超限原样返回。超限时返回
    标题/URL/元素数 + 可见内容摘要，完整快照落盘（executor 同款
    .hiveweave/tool_outputs/ 约定，7 天保留）并回传句柄。agent-browser
    snapshot 是 a11y 树（Page:/URL: 头 + @eN refs），树本身即结构化文本，
    无需 HTML 剥离；--json 模式按 "role" 字段计数兜底。
    """
    byte_len = len(output.encode("utf-8", errors="replace"))
    if byte_len <= TOOL_OUTPUT_MAX_BYTES:
        return output

    import re as _re

    title = ""
    url = ""
    for line in output.splitlines():
        if not title and line.startswith("Page: "):
            title = line[len("Page: "):].strip()
        elif not url and line.startswith("URL: "):
            url = line[len("URL: "):].strip()
        if title and url:
            break

    element_count = len(_re.findall(r"@e\d+", output))
    if not element_count:
        element_count = len(_re.findall(r'"role"\s*:', output))

    lines = output.splitlines()
    drop = 0
    while drop < min(2, len(lines)) and (
        lines[drop].startswith("Page: ") or lines[drop].startswith("URL: ")
    ):
        drop += 1
    summary_bits: list[str] = []
    used = 0
    for line in lines[drop:]:
        capped = line if len(line) <= _SNAPSHOT_LINE_MAX_CHARS else (
            f"{line[:_SNAPSHOT_LINE_MAX_CHARS - 1]}…"
        )
        if used + len(capped) + 1 > _SNAPSHOT_CONTRACT_TEXT_CHARS:
            break
        summary_bits.append(capped)
        used += len(capped) + 1
    if len(summary_bits) < len(lines[drop:]):
        summary_bits.append("…")
    summary = "\n".join(summary_bits)

    from hiveweave.tools.executor import ToolExecutor

    file_path = ToolExecutor._save_tool_output_file(
        output, agent_id, "browse", workspace
    )
    return (
        f"[snapshot 已落盘: {file_path}]（{len(lines)} 行，{byte_len} 字节）\n"
        f"页面标题: {title or '(未提取)'}\n"
        f"URL: {url or '(未提取)'}\n"
        f"元素数: {element_count}\n"
        f"可见内容摘要（已截断，完整快照见落盘文件）:\n{summary}"
    )


@tool(
    "browse",
    "Drive a real Chromium browser via agent-browser (goto/click/fill/snapshot/"
    "screenshot/viewport/console/network/js/eval). Use js/eval for canvas MouseEvent "
    "injection when snapshot refs are insufficient. "
    "Prefer lookup_dev_server / start_dev_server for the app URL first. "
    "goto always resets the window to 1280×900. For mobile: goto first, then "
    "browse(args=[\"viewport\",\"390\",\"844\"]), then screenshot. "
    "After screenshot: evidence roles (QA / executor visual gate) MUST call "
    "assert_visual(observed, verdict) on what they SEE (path-only evidence "
    "is rejected for UI submit). Looking-only roles (CEO) inspect the image "
    "or look_at_image — do not stamp. "
    "For H5/canvas games, evidence roles prefer game_run_case after goto "
    "(MAIN VERIFY: game_run_case_main). "
    "Example: browse(args=[\"goto\",\"http://127.0.0.1:3000\"]) then "
    "browse(args=[\"snapshot\",\"-i\"]). If goto/eval keep timing out, "
    "call browse(args=[\"restart\"]) to close the session first. "
    "On success, evidence roles issue a browse_e2e attestation. Your "
    "worktree UI checks use this tool. Milestone VERIFY / full-site MAIN "
    "QA: use browse_main. CEO looking at MAIN may also use browse_main.",
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
    argv = ensure_screenshot_argv(argv, agent_id)

    if _is_viewport_command(argv):
        dims = parse_viewport_args(_viewport_rest(argv))
        if dims is None:
            return ToolResult.err(VIEWPORT_USAGE)

    # Soft guard: discourage attaching to the operator's daily profile URLs
    # that look like credential harvesting — still allow localhost / file / http(s).
    joined = " ".join(argv).lower()
    cred_tokens = ("cookies set", "cookies import", "--restore", "--profile", "--state")
    if any(tok in joined for tok in cred_tokens) and "--domain" not in joined:
        return ToolResult.err(
            "Cookie/profile attach commands (cookies set/import, --restore, "
            "--profile, --state) are blocked for agents without an explicit "
            "--domain. Import cookies manually, or pass an explicit --domain."
        )

    head = (argv[0] or "").lower().replace("-", "_")
    # Normalize through the alias table so `wait_for`/`waitfor` count as the
    # intentionally long-lived `wait` (the raw head is used elsewhere).
    head_norm = _HEAD_ALIASES.get(head, head)
    is_restart = head in ("restart", "reset")
    if is_restart:
        # Hard-recycle instead of the CLI `close` — a wedged daemon ignores or
        # hangs on close, so restart must not depend on it. The next browse
        # command respawns a fresh session.
        recycle_note = await _hard_recycle_browser()
        return ToolResult.ok(f"{BROWSE_RESTART_OK} [{recycle_note}]")
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
        err = "\n".join(parts)
        if code == -1 and head_norm != "wait":
            # Timeout usually means a wedged daemon — recycle so the next
            # command starts fresh instead of looping on timeouts. `wait` is
            # intentionally long-lived, so it keeps the plain hint.
            await _hard_recycle_browser()
            err = (
                f"{err}\nBrowser session hard-recycled. "
                "Retry the command — the next call starts a fresh browser."
            )
        elif code == -1 and BROWSE_RESTART_HINT not in err:
            err = f"{err}\n{BROWSE_RESTART_HINT}"
        return ToolResult.err(err)

    viewport_note = ""
    if head in _GOTO_HEADS:
        _ok, viewport_note = await _apply_default_viewport(
            workspace, agent_id, params.timeout_sec or 60
        )
    elif _is_viewport_command(argv):
        dims = parse_viewport_args(_viewport_rest(argv))
        if dims:
            w, h, scale = dims
            scale_bit = f" scale={scale}" if scale else ""
            viewport_note = (
                f"viewport is now {w}×{h}{scale_bit}. "
                f"The next goto resets to {DEFAULT_VIEWPORT[0]}×{DEFAULT_VIEWPORT[1]}; "
                "set viewport AFTER goto for mobile checks."
            )

    out = stdout or "(no output)"
    if stderr:
        out = f"{out}\n--- stderr ---\n{stderr}"
    if viewport_note:
        out = f"{out}\n[{viewport_note}]"

    if _is_viewport_command(argv):
        return ToolResult.ok(out)

    extra_fields: dict[str, Any] = {}
    screenshot_abs: str | None = None
    img = None
    shot_path = None
    shot_rel = _screenshot_path_from_argv(argv)
    if shot_rel:
        from hiveweave.services.vision import (
            load_image_for_llm,
            resolve_screenshot_path,
        )

        shot_path = resolve_screenshot_path(workspace, shot_rel)
        # Only trust a path that actually exists on disk — do not hand the
        # attestation a guessed absolute path before is_file() has passed
        # (that used to mint "fake success" browse_e2e stamps on missing PNGs).
        shot_exists = shot_path is not None and shot_path.is_file()
        img = load_image_for_llm(shot_path) if shot_exists else None
        if shot_path and shot_exists:
            screenshot_abs = str(shot_path)

    core_interaction = head in ("js", "eval", "evaluate")
    look_only = await agent_is_look_only_browser(agent_id)
    if look_only:
        attest_note = (
            "\n\n[look-only] No browse_e2e stamp — looking at the product "
            "is not test evidence. Formal VERIFY stays with QA.\n"
        )
    else:
        attest_note = await issue_browse_e2e_attestation(
            agent_id=agent_id,
            workspace=workspace,
            argv=argv,
            stdout=out,
            task_id=params.task_id,
            core_interaction=core_interaction,
            screenshot_path=screenshot_abs,
        )
        if "[browse_e2e REJECTED]" in attest_note:
            return ToolResult.err(attest_note.strip())

    # 大快照短契约化 —— attestation 先按全量 stdout 出证（stdout_hash
    # 完整性），返回文本再收短契约；<50KB 的快照原样返回。
    if head == "snapshot":
        out = _contract_snapshot_output(out, agent_id, workspace)

    if shot_rel:
        if img and screenshot_abs:
            extra_fields["images"] = [img]
            extra_fields["screenshot_path"] = screenshot_abs
            # NOTE: tool_exec drops extra fields before the next LLM round —
            # the path MUST be in the text for assert_visual(screenshotPath=...).
            shot_display = _workspace_rel_shot_display(
                workspace, shot_rel, shot_path
            )
            out = (
                f"{out}{attest_note}\n"
                + screenshot_followup_text(shot_display, look_only=look_only)
            )
            return ToolResult.ok(out, **extra_fields)

        shot_display = _workspace_rel_shot_display(
            workspace, shot_rel, shot_path
        )
        missing = shot_path is None or not Path(shot_path).is_file()
        if missing:
            return ToolResult.err(
                f"Screenshot file missing at {shot_display}. "
                + _screenshot_missing_diagnostic(workspace, shot_rel)
            )
        fail_hint = (
            "inspect the image with look_at_image if this is a review."
            if look_only
            else "assert_visual still required for UI evidence."
        )
        out = (
            f"{out}{attest_note}\n"
            "[VISION] Screenshot file could not be loaded into multimodal "
            f"context (path={shot_display}). Re-take screenshot using "
            f"{shot_display}; {fail_hint}"
        )
        return ToolResult.ok(out)

    return ToolResult.ok(out + attest_note)


@tool(
    "browse_main",
    "Drive Chromium at the PROJECT ROOT (shared MAIN), not your worktree. "
    "Same params as browse. QA: milestone VERIFY / full-site so browse_e2e "
    "stamps MAIN HEAD. CEO: look at the shipped product on MAIN (not a "
    "test duty). goto resets viewport to 1280×900; viewport AFTER goto "
    "for mobile. Module visual in your slice stays on browse. "
    "Platform does not rewrite browse cwd.",
    requires_workspace=True,
    security_level="shell",
)
async def browse_main_tool(
    params: BrowseParams, agent_id: str, workspace: str
) -> ToolResult:
    from hiveweave.tools.bash import _with_cwd_note, resolve_project_main_cwd
    from hiveweave.tools.helpers import get_project_id

    project_id = await get_project_id(agent_id)
    main_ws, err = await resolve_project_main_cwd(project_id)
    if not main_ws:
        return ToolResult.err(err)
    result = await browse_tool(params, agent_id, main_ws)
    return _with_cwd_note(result, f"\n\n[cwd=project root] {main_ws}")


class AssertVisualParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    screenshot_path: str = Field(
        ...,
        alias="screenshotPath",
        description=(
            "Path to the PNG/JPEG produced by browse screenshot. Copy it from "
            "the browse tool result text after 'Screenshot saved at:' — do not "
            "guess the path."
        ),
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

    orig_ws = workspace
    shot = resolve_screenshot_path(workspace, params.screenshot_path)
    if shot is None:
        try:
            from hiveweave.tools.bash import resolve_project_main_cwd
            from hiveweave.tools.helpers import get_project_id

            project_id = await get_project_id(agent_id)
            main_ws, _err = await resolve_project_main_cwd(project_id)
            if main_ws:
                shot = resolve_screenshot_path(main_ws, params.screenshot_path)
                if shot is not None:
                    workspace = main_ws
        except Exception:
            pass
    if shot is None and orig_ws and orig_ws != workspace:
        shot = resolve_screenshot_path(orig_ws, params.screenshot_path)
    if shot is None:
        return ToolResult.err(
            f"Screenshot path rejected or outside workspace: "
            f"{params.screenshot_path!r}. Use a path under MAIN / your "
            f"worktree (e.g. evidence/flow.png)."
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
                from hiveweave.services.task import TaskService
                from hiveweave.services.worktree_review import (
                    project_main_workspace,
                )
                from hiveweave.tools.bash import (
                    _is_same_workspace,
                    _task_needs_main_workspace,
                )

                try:
                    _task = await TaskService().get_task(project_id, task_id)
                except Exception as e:
                    return ToolResult.err(
                        "[visual_check REJECTED] cannot load bound task "
                        f"for MAIN check: {e}"
                        + (bind_note or "")
                    )
                if _task is None:
                    return ToolResult.err(
                        "[visual_check REJECTED] bound task not found."
                        + (bind_note or "")
                    )
                if _task_needs_main_workspace(_task):
                    try:
                        _main_ws = await project_main_workspace(project_id)
                    except Exception as e:
                        return ToolResult.err(
                            "[visual_check REJECTED] cannot resolve MAIN: "
                            f"{e}"
                            + (bind_note or "")
                        )
                    if not _main_ws:
                        return ToolResult.err(
                            "[visual_check REJECTED] cannot resolve MAIN. "
                            "Take the screenshot with browse_main "
                            "(project root)."
                            + (bind_note or "")
                        )
                    if not workspace or not _is_same_workspace(
                        workspace, _main_ws
                    ):
                        return ToolResult.err(
                            "[visual_check REJECTED] VERIFY "
                            "UI evidence must run on MAIN. Take the "
                            "screenshot with browse_main (project root), "
                            "not browse (your worktree)."
                            + (bind_note or "")
                        )
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
                artifact_hashes={"screenshot_path": str(shot)},
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
