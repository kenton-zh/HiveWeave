"""browse — gstack Chromium CLI wrapper for agent UI/E2E testing."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hiveweave.config import resolve_browse_bin, settings
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult


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
        description="Max seconds to wait for the browse command (default 60).",
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


@tool(
    "browse",
    "Drive a real Chromium browser via gstack browse (goto/click/fill/snapshot/"
    "screenshot/console/network). Use for UI E2E and visual evidence. "
    "Prefer lookup_dev_server / start_dev_server for the app URL first. "
    "Example: browse(args=[\"goto\",\"http://127.0.0.1:3000\"]) then "
    "browse(args=[\"snapshot\",\"-i\"]).",
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

    # Soft guard: discourage attaching to the operator's daily profile URLs
    # that look like credential harvesting — still allow localhost / file / http(s).
    joined = " ".join(argv).lower()
    if "cookie-import-browser" in joined and "--domain" not in joined:
        return ToolResult.err(
            "cookie-import-browser without --domain is blocked for agents. "
            "Use setup-browser-cookies skill manually, or pass an explicit --domain."
        )

    timeout = max(5, min(int(params.timeout_sec or 60), 300))
    cmd = [str(bin_path), *argv]
    cwd = workspace if workspace and Path(workspace).is_dir() else None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, "GSTACK_HEADLESS": os.environ.get("GSTACK_HEADLESS", "1")},
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
    return ToolResult.ok(out)
