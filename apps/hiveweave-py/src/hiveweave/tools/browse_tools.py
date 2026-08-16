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
    """Extract output path from ``screenshot [path]`` argv.

    Supports both ``screenshot path.png``, ``screenshot --selector canvas path.png``
    and the agent-browser positional form ``screenshot canvas path.png``
    (also under the ``shoot`` alias).
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
    return "screenshot.png"


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
    mapped, stdin_payload = _map_ab_argv(argv, workspace)

    timeout = max(5, min(int(timeout_sec or 60), 300))
    head = (mapped[0] or "").lower().replace("-", "_") if mapped else ""
    if head in (
        "click", "wait", "fill", "press", "type", "select", "eval",
        "close", "reload",
    ):
        timeout = max(30, timeout)

    # Screenshot paths land under the workspace; make sure the parent dir
    # exists (agents write to evidence/… which may not exist yet).
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
    try:
        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if not project_id:
            return ""
        from hiveweave.tools.bash import _is_same_workspace

        resolved_task, bind_note = await _resolve_task_id(
            project_id, agent_id, task_id, command=" ".join(argv)
        )
        if not resolved_task:
            return bind_note or ""
        # TEST18 P0-2: VERIFY 任务的 browse 证据必须在 main 工作区执行 —
        # worktree 嵌在项目根下，「在根下面」会放行，必须目录等值（同 bash）。
        try:
            from hiveweave.services.task import TaskService
            from hiveweave.services.worktree_review import project_main_workspace

            task = await TaskService().get_task(project_id, resolved_task)
            if task and TaskService._is_verify_task(task):
                main_ws = await project_main_workspace(project_id)
                if workspace and main_ws and not _is_same_workspace(workspace, main_ws):
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
    except Exception:
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
    "screenshot/console/network/js/eval). Use js/eval for canvas MouseEvent "
    "injection when snapshot refs are insufficient. "
    "Prefer lookup_dev_server / start_dev_server for the app URL first. "
    "After screenshot the PNG pixels are injected into your next LLM turn — "
    "you MUST call assert_visual(observed, verdict) based on what you SEE "
    "(path-only evidence is rejected for UI submit). "
    "For H5/canvas games prefer game_run_case after goto. "
    "Example: browse(args=[\"goto\",\"http://127.0.0.1:3000\"]) then "
    "browse(args=[\"snapshot\",\"-i\"]). If goto/eval keep timing out, "
    "call browse(args=[\"restart\"]) to close the session first. "
    "On success issues a browse_e2e attestation.",
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
    cred_tokens = ("cookies set", "cookies import", "--restore", "--profile", "--state")
    if any(tok in joined for tok in cred_tokens) and "--domain" not in joined:
        return ToolResult.err(
            "Cookie/profile attach commands (cookies set/import, --restore, "
            "--profile, --state) are blocked for agents without an explicit "
            "--domain. Import cookies manually, or pass an explicit --domain."
        )

    head = (argv[0] or "").lower().replace("-", "_")
    is_restart = head in ("restart", "reset")
    try:
        code, stdout, stderr = await browse_exec(
            argv, workspace, timeout_sec=params.timeout_sec or 60, agent_id=agent_id
        )
    except FileNotFoundError:
        return ToolResult.err(browse_missing_bin_hint())
    except OSError as e:
        return ToolResult.err(f"browse spawn failed: {e}")

    if is_restart and code == 0:
        return ToolResult.ok(BROWSE_RESTART_OK)

    if code != 0:
        parts = [f"browse exit={code}: {' '.join(argv)}"]
        if stdout:
            parts.append(stdout[-4000:])
        if stderr:
            parts.append(f"stderr:\n{stderr[-2000:]}")
        err = "\n".join(parts)
        if code == -1 and BROWSE_RESTART_HINT not in err:
            err = f"{err}\n{BROWSE_RESTART_HINT}"
        return ToolResult.err(err)

    out = stdout or "(no output)"
    if stderr:
        out = f"{out}\n--- stderr ---\n{stderr}"

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
        img = load_image_for_llm(shot_path) if shot_path else None
        # Persist path even when pixels fail to load (size/suffix) so
        # look_at_image(attestation_id) can still resolve the file.
        if shot_path:
            screenshot_abs = str(shot_path)

    core_interaction = head in ("js", "eval", "evaluate")
    attest_note = await issue_browse_e2e_attestation(
        agent_id=agent_id,
        workspace=workspace,
        argv=argv,
        stdout=out,
        task_id=params.task_id,
        core_interaction=core_interaction,
        screenshot_path=screenshot_abs,
    )

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
            shot_display = screenshot_abs.replace("\\", "/")
            out = (
                f"{out}{attest_note}\n"
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
                    from hiveweave.tools.bash import _is_same_workspace

                    _task = await TaskService().get_task(project_id, task_id)
                    if _task and TaskService._is_verify_task(_task):
                        _main_ws = await project_main_workspace(project_id)
                        if workspace and _main_ws and not _is_same_workspace(
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
