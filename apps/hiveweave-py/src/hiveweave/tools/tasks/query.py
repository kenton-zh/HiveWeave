"""get_tasks tool

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)

# ── get_tasks ───────────────────────────────────────────


class GetTasksParams(BaseModel):
    """Parameters for get_tasks tool."""
    model_config = ConfigDict(populate_by_name=True)

    status: str | None = Field(
        default=None,
        description="Filter by status (optional).",
        json_schema_extra={"aliases": ["status", "state"]},
    )
    assignee_id: str | None = Field(
        default=None,
        alias="assigneeId",
        description="Filter by assignee agent ID (optional).",
        json_schema_extra={"aliases": ["assigneeId", "assignee_id", "assignee"]},
    )


@tool(
    "get_tasks",
    "List tasks in the Task Ledger with optional filters (status, assignee).",
    requires_workspace=False,
    security_level="standard",
)
async def get_tasks_tool(
    params: GetTasksParams, agent_id: str, workspace: str
) -> ToolResult:
    """List tasks with optional filters."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = _task_svc.TaskService()
        tasks = await ts.list_tasks(
            project_id, status=params.status, assignee_id=params.assignee_id
        )
        if not tasks:
            body = "No tasks found matching the filters."
            try:
                from hiveweave.llm.streamer import _build_obligations_snapshot
                snap = await _build_obligations_snapshot(agent_id)
                if snap:
                    body += snap
            except Exception:
                pass
            return ToolResult.ok(body, tasks=[])
        lines = [
            "Tip: claim/submit/review/cancel accept full UUID or unique "
            "8-char prefix — prefer copying the full id= value."
        ]
        # Prefetch verification cases for VERIFY visibility (TEST12 dogfood)
        case_by_verify: dict[str, dict] = {}
        case_by_original: dict[str, dict] = {}
        try:
            from hiveweave.services.task import VerificationCaseService

            for case in await VerificationCaseService().list_cases_for_project(
                project_id, limit=50
            ):
                vid = case.get("verify_task_id")
                oid = case.get("original_task_id")
                if vid:
                    case_by_verify[str(vid)] = case
                if oid:
                    case_by_original[str(oid)] = case
        except Exception:
            pass
        for t in tasks:
            tid = str(t.get("id") or "")
            lines.append(
                f"- [{t.get('status', '?')}] {t.get('title', '?')} "
                f"(id={tid}, short={tid[:8]}, "
                f"progress={t.get('progress', 0)}%, "
                f"assignee={t.get('assignee_id') or 'unassigned'})"
            )
            case = case_by_verify.get(tid) or case_by_original.get(tid)
            if case:
                notes = (case.get("review_notes") or "").replace("\n", " ")[:120]
                lines.append(
                    f"    verification_case: status={case.get('status')}, "
                    f"merge={str(case.get('merge_commit_hash') or '')[:12] or '—'}, "
                    f"notes={notes or '—'}"
                )
            ev = t.get("evidence") or {}
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = {}
            if isinstance(ev, dict) and ev.get("verification_case") and not case:
                vc = ev["verification_case"]
                if isinstance(vc, dict):
                    lines.append(
                        f"    verification_case(evidence): "
                        f"status={vc.get('status')}, "
                        f"notes={str(vc.get('review_notes') or '')[:80]}"
                    )
        # Informational (TEST11 #7): creator tracking of running work —
        # not an obligation, just a Lead visibility partition.
        tracking = [
            t for t in tasks
            if t.get("creator_id") == agent_id
            and t.get("status") == "running"
            and t.get("assignee_id") != agent_id
        ]
        if tracking and not params.assignee_id:
            lines.append(
                "\n[tracking] Created by you, currently running "
                "(informational — not your review obligation):"
            )
            for t in tracking[:10]:
                tid = str(t.get("id") or "")
                lines.append(
                    f"  · {t.get('title', '?')[:40]} "
                    f"(id={tid}, "
                    f"assignee={str(t.get('assignee_id') or '?')[:12]}, "
                    f"progress={t.get('progress', 0)}%)"
                )
        body = f"Tasks ({len(tasks)}):\n" + "\n".join(lines)
        try:
            from hiveweave.llm.streamer import _build_obligations_snapshot
            snap = await _build_obligations_snapshot(agent_id)
            if snap:
                body += snap
        except Exception:
            pass
        return ToolResult.ok(body, tasks=tasks)
    except Exception as e:
        return ToolResult.err(f"Failed to list tasks: {e}")
