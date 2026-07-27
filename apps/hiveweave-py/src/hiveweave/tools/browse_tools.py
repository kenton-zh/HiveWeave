"""browse — gstack Chromium CLI wrapper for agent UI/E2E testing."""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hiveweave.config import resolve_browse_bin, settings
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

# Minimum structured observation length for assert_visual (not free-text scrape —
# the field itself is the structured assertion evidence).
_VISUAL_OBSERVED_MIN = 40


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


async def _resolve_task_id(project_id: str, agent_id: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from hiveweave.services.task import TaskService

        tasks = await TaskService().list_tasks(project_id, assignee_id=agent_id)
        active = [t for t in tasks if t.get("status") in ("running", "claimed")]
        if active:
            return active[0].get("id")
    except Exception:
        pass
    return None


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
    """Extract output path from ``screenshot [path]`` argv."""
    if not argv:
        return None
    if (argv[0] or "").lower().replace("-", "_") != "screenshot":
        return None
    if len(argv) >= 2 and str(argv[1]).strip():
        return str(argv[1]).strip()
    return "screenshot.png"


@tool(
    "browse",
    "Drive a real Chromium browser via gstack browse (goto/click/fill/snapshot/"
    "screenshot/console/network/js/eval). Use js/eval for canvas MouseEvent "
    "injection when snapshot refs are insufficient. "
    "Prefer lookup_dev_server / start_dev_server for the app URL first. "
    "After screenshot the PNG pixels are injected into your next LLM turn — "
    "you MUST call assert_visual(observed, verdict) based on what you SEE "
    "(path-only evidence is rejected for UI submit). "
    "Example: browse(args=[\"goto\",\"http://127.0.0.1:3000\"]) then "
    "browse(args=[\"snapshot\",\"-i\"]). On success issues a browse_e2e attestation.",
    requires_workspace=True,
    security_level="shell",
)
async def browse_tool(
    params: BrowseParams, agent_id: str, workspace: str
) -> ToolResult:
    bin_path = resolve_browse_bin()
    if not bin_path:
        hint = (
            "gstack browse binary not found. Build it once:\n"
            "  cd %USERPROFILE%\\.claude\\skills\\gstack && bun install && bun run build\n"
            "Or set HIVEWEAVE_BROWSE_BIN to the browse.exe path."
        )
        if settings.browse_bin:
            hint = f"HIVEWEAVE_BROWSE_BIN={settings.browse_bin!r} is missing or not a file.\n" + hint
        return ToolResult.err(hint)

    argv = _parse_argv(params)
    if not argv:
        return ToolResult.err(
            'browse requires args or command. Example: '
            'args=["goto","http://127.0.0.1:3000"]'
        )

    # Treat evaluate as alias for js (TEST21 M14)
    if (argv[0] or "").lower() == "evaluate":
        argv = ["js", *argv[1:]]

    # Soft guard: discourage attaching to the operator's daily profile URLs
    # that look like credential harvesting — still allow localhost / file / http(s).
    joined = " ".join(argv).lower()
    if "cookie-import-browser" in joined and "--domain" not in joined:
        return ToolResult.err(
            "cookie-import-browser without --domain is blocked for agents. "
            "Use setup-browser-cookies skill manually, or pass an explicit --domain."
        )

    timeout = max(5, min(int(params.timeout_sec or 60), 300))
    # click/wait/js often need >10s for async UI; floor at 30s for those actions.
    head = (argv[0] or "").lower().replace("-", "_")
    if head in (
        "click", "wait", "wait_for", "waitfor", "fill", "press",
        "js", "eval", "evaluate",
    ):
        timeout = max(30, timeout)
    cmd = [str(bin_path), *argv]
    cwd = workspace if workspace and Path(workspace).is_dir() else None

    try:
        from hiveweave.util.win_subprocess import windows_no_window_kwargs

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, "GSTACK_HEADLESS": os.environ.get("GSTACK_HEADLESS", "1")},
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
            return ToolResult.err(
                f"browse timed out after {timeout}s: {' '.join(argv)}"
            )
    except FileNotFoundError:
        return ToolResult.err(f"browse binary not executable: {bin_path}")
    except OSError as e:
        return ToolResult.err(f"browse spawn failed: {e}")

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    code = proc.returncode if proc.returncode is not None else -1

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

    # Issue browse_e2e attestation on success
    attest_note = ""
    core_interaction = head in ("js", "eval", "evaluate")
    try:
        from hiveweave.services.attestation import (
            attestation_service,
            hash_stdout,
        )
        from hiveweave.tools.helpers import get_project_id

        project_id = await get_project_id(agent_id)
        if project_id:
            task_id = await _resolve_task_id(project_id, agent_id, params.task_id)
            commit = await _maybe_git_commit(workspace or "")
            cmd_url = " ".join(argv)[:500]
            if core_interaction:
                cmd_url = f"[core_interaction=1] {cmd_url}"
            att_id = await attestation_service.create(
                project_id,
                agent_id=agent_id,
                kind="browse_e2e",
                tool_call_id=str(uuid.uuid4()),
                task_id=task_id,
                command_or_url=cmd_url,
                exit_code=0,
                workspace=workspace or None,
                commit=commit,
                stdout_hash=hash_stdout(out),
                console_errors=0,
            )
            extra = " core_interaction=1" if core_interaction else ""
            attest_note = f"\n[attestation_id={att_id} kind=browse_e2e{extra}]"
    except Exception:
        pass

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
            task_id = await _resolve_task_id(project_id, agent_id, params.task_id)
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
