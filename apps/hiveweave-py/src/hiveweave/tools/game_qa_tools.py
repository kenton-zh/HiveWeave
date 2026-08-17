"""game_run_case — H5/canvas harness runner (scripts drive, vision judges)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hiveweave.config import resolve_browse_bin
from hiveweave.tools.base import tool
from hiveweave.tools.browse_tools import (
    _force_main_ui_workspace,
    browse_exec,
    browse_missing_bin_hint,
    issue_browse_e2e_attestation,
)
from hiveweave.tools.result import ToolResult

_PROBE_JS = (
    "(() => {"
    " const hw = window.__HW_TEST__;"
    " return JSON.stringify({"
    "  hw: !!(hw && hw.ready),"
    "  version: hw && hw.version || null,"
    "  cases: hw && typeof hw.list === 'function' ? hw.list() : [],"
    "  render_game_to_text: typeof window.render_game_to_text,"
    "  advanceTime: typeof window.advanceTime"
    " });"
    "})()"
)

_LIST_JS = (
    "(() => {"
    " if (!window.__HW_TEST__ || typeof window.__HW_TEST__.list !== 'function')"
    "  return JSON.stringify({error:'no __HW_TEST__.list'});"
    " return JSON.stringify({cases: window.__HW_TEST__.list()});"
    "})()"
)


def _run_case_js(case_id: str) -> str:
    # Escape for embedding in a JS string literal.
    safe = (
        case_id.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    return (
        "(async () => {"
        " if (!window.__HW_TEST__ || typeof window.__HW_TEST__.run !== 'function')"
        "  return JSON.stringify({error:'no __HW_TEST__.run'});"
        f" const r = await window.__HW_TEST__.run('{safe}');"
        " return JSON.stringify(r);"
        "})()"
    )


def _parse_json_blob(text: str) -> dict[str, Any] | None:
    """Extract first JSON object from browse js stdout (may have wrappers)."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Strip common UNTRUSTED markers / quotes
    for marker in (
        "--- BEGIN UNTRUSTED EXTERNAL CONTENT ---",
        "--- END UNTRUSTED EXTERNAL CONTENT ---",
    ):
        raw = raw.replace(marker, "")
    raw = raw.strip().strip("`").strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            raw = json.loads(raw)
            if isinstance(raw, str):
                raw = raw.strip()
        except json.JSONDecodeError:
            pass
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def _js(
    workspace: str, expr: str, timeout_sec: int, agent_id: str | None = None
) -> tuple[int, str, str]:
    return await browse_exec(
        ["js", expr], workspace, timeout_sec=timeout_sec, agent_id=agent_id
    )


class GameRunCaseParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: Literal["probe", "list", "run"] = Field(
        ...,
        description=(
            "probe: detect __HW_TEST__ / render_game_to_text / advanceTime. "
            "list: list case ids. "
            "run: execute one case, screenshot canvas, return codePass + visionCriteria."
        ),
    )
    case_id: str | None = Field(
        default=None,
        alias="caseId",
        description="Required when action=run. Case id from list().",
        json_schema_extra={"aliases": ["caseId", "case_id", "id"]},
    )
    screenshot_path: str | None = Field(
        default=None,
        alias="screenshotPath",
        description=(
            "Where to save canvas screenshot after run "
            "(default evidence/hw-game-<caseId>.png)."
        ),
        json_schema_extra={"aliases": ["screenshotPath", "screenshot_path"]},
    )
    screenshot_selector: str = Field(
        default="canvas",
        alias="screenshotSelector",
        description="CSS selector for screenshot (default canvas).",
        json_schema_extra={"aliases": ["screenshotSelector", "selector"]},
    )
    timeout_sec: int = Field(
        default=90,
        alias="timeoutSec",
        description="Per browse step timeout seconds (default 90).",
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="Optional task id for browse_e2e attestation.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


@tool(
    "game_run_case",
    "Run an H5/canvas game test harness case (docs/spec/h5-game-test-harness.md). "
    "Requires prior browse(goto) to the game URL (?hw_test=1). "
    "Actions: probe | list | run(caseId). "
    "run() drives inputs via window.__HW_TEST__, returns codePass + visionCriteria, "
    "screenshots canvas, injects pixels — then you MUST assert_visual. "
    "No harness → observe-only; do not claim gameplay pass. "
    "Never attempt realtime AI play of action games.",
    requires_workspace=True,
    security_level="shell",
)
async def game_run_case_tool(
    params: GameRunCaseParams, agent_id: str, workspace: str
) -> ToolResult:
    if not resolve_browse_bin():
        return ToolResult.err(browse_missing_bin_hint())

    action = (params.action or "").strip().lower()
    timeout = max(30, min(int(params.timeout_sec or 90), 300))

    exec_ws, force_note = await _force_main_ui_workspace(
        agent_id, workspace, params.task_id
    )
    if not exec_ws and force_note:
        return ToolResult.err(force_note.strip())
    workspace = exec_ws or workspace

    try:
        if action == "probe":
            return await _action_probe(agent_id, workspace, timeout, params.task_id)
        if action == "list":
            return await _action_list(agent_id, workspace, timeout, params.task_id)
        if action == "run":
            case_id = (params.case_id or "").strip()
            if not case_id:
                return ToolResult.err(
                    "game_run_case action=run requires caseId "
                    "(from action=list)."
                )
            return await _action_run(
                agent_id,
                workspace,
                timeout,
                case_id,
                params.screenshot_path,
                params.screenshot_selector or "canvas",
                params.task_id,
            )
        return ToolResult.err(
            f"Unknown action={action!r}. Use probe | list | run."
        )
    except FileNotFoundError:
        return ToolResult.err(browse_missing_bin_hint())
    except OSError as e:
        return ToolResult.err(f"game_run_case browse spawn failed: {e}")


async def _action_probe(
    agent_id: str, workspace: str, timeout: int, task_id: str | None
) -> ToolResult:
    code, stdout, stderr = await _js(workspace, _PROBE_JS, timeout, agent_id)
    if code != 0:
        return ToolResult.err(
            f"probe failed exit={code}\n{stdout[-2000:]}\n{stderr[-1000:]}"
        )
    data = _parse_json_blob(stdout) or {}
    attest = await issue_browse_e2e_attestation(
        agent_id=agent_id,
        workspace=workspace,
        argv=["js", "probe __HW_TEST__"],
        stdout=stdout,
        task_id=task_id,
        core_interaction=True,
    )
    hw = bool(data.get("hw"))
    has_rgt = data.get("render_game_to_text") == "function"
    has_at = data.get("advanceTime") == "function"
    if hw:
        tier = "instrumented"
        next_hint = (
            "Harness ready. Next: game_run_case(action=\"list\") then "
            "game_run_case(action=\"run\", caseId=...)."
        )
    elif has_rgt and has_at:
        tier = "scripted"
        next_hint = (
            "Compat hooks only (no __HW_TEST__). Drive via browse js "
            "advanceTime + action bursts, then screenshot + assert_visual. "
            "Prefer adding window.__HW_TEST__."
        )
    else:
        tier = "observe-only"
        next_hint = (
            "No game harness. Only boot/console/static screenshot allowed — "
            "do NOT claim gameplay pass. Ask Executor to implement "
            "docs/spec/h5-game-test-harness.md."
        )
    out = (
        f"tier={tier}\n"
        f"probe={json.dumps(data, ensure_ascii=False)}\n"
        f"{next_hint}"
        f"{attest}"
    )
    return ToolResult.ok(
        out,
        tier=tier,
        harness=data,
        cases=data.get("cases") or [],
    )


async def _action_list(
    agent_id: str, workspace: str, timeout: int, task_id: str | None
) -> ToolResult:
    code, stdout, stderr = await _js(workspace, _LIST_JS, timeout, agent_id)
    if code != 0:
        return ToolResult.err(
            f"list failed exit={code}\n{stdout[-2000:]}\n{stderr[-1000:]}"
        )
    data = _parse_json_blob(stdout) or {}
    if data.get("error"):
        return ToolResult.err(
            f"{data['error']}. Run probe first; game must expose "
            "window.__HW_TEST__.list()."
        )
    cases = data.get("cases") or []
    attest = await issue_browse_e2e_attestation(
        agent_id=agent_id,
        workspace=workspace,
        argv=["js", "__HW_TEST__.list()"],
        stdout=stdout,
        task_id=task_id,
        core_interaction=True,
    )
    out = (
        f"cases={json.dumps(cases, ensure_ascii=False)}\n"
        "Next: game_run_case(action=\"run\", caseId=\"<id>\")"
        f"{attest}"
    )
    return ToolResult.ok(out, cases=cases)


async def _action_run(
    agent_id: str,
    workspace: str,
    timeout: int,
    case_id: str,
    screenshot_path: str | None,
    selector: str,
    task_id: str | None,
) -> ToolResult:
    code, stdout, stderr = await _js(
        workspace, _run_case_js(case_id), timeout, agent_id
    )
    if code != 0:
        return ToolResult.err(
            f"run({case_id!r}) js failed exit={code}\n"
            f"{stdout[-2000:]}\n{stderr[-1000:]}"
        )
    result = _parse_json_blob(stdout)
    if not result:
        return ToolResult.err(
            f"run({case_id!r}) did not return JSON HwCaseResult.\n"
            f"stdout={stdout[:1500]}"
        )
    if result.get("error"):
        return ToolResult.err(str(result["error"]))

    code_pass = bool(result.get("codePass"))
    code_errors = result.get("codeErrors") or []
    vision_criteria = (result.get("visionCriteria") or "").strip()
    hint_sel = (result.get("screenshotHint") or selector or "canvas").strip()
    shot_rel = (screenshot_path or "").strip() or (
        f"evidence/hw-game-{_safe_filename(case_id)}.png"
    )

    # Ensure evidence dir exists under workspace
    shot_abs_parent = Path(workspace) / Path(shot_rel).parent
    try:
        shot_abs_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    scode, sout, serr = await browse_exec(
        ["screenshot", "--selector", hint_sel, shot_rel],
        workspace,
        timeout_sec=timeout,
        agent_id=agent_id,
    )
    shot_ok = scode == 0
    shot_note = sout if shot_ok else f"screenshot failed exit={scode}: {serr or sout}"

    attest = await issue_browse_e2e_attestation(
        agent_id=agent_id,
        workspace=workspace,
        argv=["game_run_case", "run", case_id],
        stdout=json.dumps(result, ensure_ascii=False),
        task_id=task_id,
        core_interaction=True,
    )

    extra: dict[str, Any] = {
        "case_id": case_id,
        "code_pass": code_pass,
        "code_errors": code_errors,
        "vision_criteria": vision_criteria,
        "screenshot_path": shot_rel,
        "harness_result": result,
        "gate": "pending_vision" if code_pass else "code_fail",
    }

    from hiveweave.services.vision import load_image_for_llm, resolve_screenshot_path

    img = None
    if shot_ok:
        resolved = resolve_screenshot_path(workspace, shot_rel)
        img = load_image_for_llm(resolved) if resolved else None
        if img:
            extra["images"] = [img]
            extra["screenshot_path"] = img.get("path") or str(resolved)

    if not code_pass:
        out = (
            f"CASE FAIL (code gate): id={case_id}\n"
            f"codePass=false errors={json.dumps(code_errors, ensure_ascii=False)}\n"
            f"result={json.dumps(result, ensure_ascii=False)}\n"
            f"screenshot={shot_rel} ok={shot_ok}\n"
            f"{shot_note}\n"
            "Do NOT assert_visual pass. Dual gate failed on code. "
            "Fix game/case or report fail."
            f"{attest}"
        )
        # Still attach image for diagnosis
        return ToolResult.ok(out, **extra)

    # codePass — vision still required
    criteria_line = vision_criteria or (
        f"Case {case_id} visual acceptance (describe what you see)."
    )
    out = (
        f"CASE codePass=true: id={case_id}\n"
        f"simulatedMs={result.get('simulatedMs')}\n"
        f"visionCriteria={criteria_line}\n"
        f"screenshot={shot_rel} ok={shot_ok}\n"
        f"{shot_note}\n"
        "[VISION] Pixels attached when screenshot ok. Inspect the image, then:\n"
        f"assert_visual(screenshotPath={shot_rel!r}, "
        f"observed=\"…what you SEE…\", "
        f"criteria={criteria_line!r}, "
        "verdict=\"pass\"|\"fail\")\n"
        "Submit only if codePass AND visual verdict=pass."
        f"{attest}"
    )
    if not shot_ok or not img:
        out += (
            "\n[WARN] Screenshot/vision inject failed — re-run browse screenshot "
            "then assert_visual before submit."
        )
    return ToolResult.ok(out, **extra)


def _safe_filename(case_id: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", case_id).strip("._")
    return (s or "case")[:64]
