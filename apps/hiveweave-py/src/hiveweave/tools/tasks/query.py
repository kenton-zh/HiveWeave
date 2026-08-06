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
        # P0-1: prefetch active waivers so agents can SEE who waived a task
        # (waived_by third-party isolation is otherwise invisible — agents
        # guessed wrong in TEST18 and deadlocked approve for hours).
        waiver_by_task: dict[str, dict] = {}
        try:
            from hiveweave.services.attestation import (
                WAIVER_KIND,
                _conn as _attestation_conn,
                attestation_service,
                canonical_task_id,
            )
            import time as _time

            await attestation_service.ensure_schema(project_id)
            # Normalize task ids to the same canonical form `create()` stored
            # (full UUID, dash-stripped) so the IN-match never misses on the
            # dashed form agents/tasks table carry.
            tids: list[str] = []
            for t in tasks:
                tid = str(t.get("id") or "")
                if not tid:
                    continue
                ctid = await canonical_task_id(project_id, tid) or tid
                tids.append(ctid)
            if tids:
                placeholders = ",".join(["?"] * len(tids))
                now_ms = int(_time.time() * 1000)
                # _conn returns a cached LRU connection — do NOT close it
                conn = await _attestation_conn(project_id)
                cur = await conn.execute(
                    "SELECT task_id, agent_id, expires_at "
                    "FROM tool_attestations "
                    "WHERE project_id = ? AND kind = ? "
                    f"AND task_id IN ({placeholders}) "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY created_at DESC",
                    [project_id, WAIVER_KIND, *tids, now_ms],
                )
                rows = await cur.fetchall()
                await cur.close()
                for r in rows:
                    stored_tid = str(r["task_id"]) if "task_id" in r.keys() else None
                    if stored_tid and stored_tid not in waiver_by_task:
                        waiver_by_task[stored_tid] = {
                            "waived_by": str(r["agent_id"]) if "agent_id" in r.keys() else None,
                            "waiver_expires_at": r["expires_at"] if "expires_at" in r.keys() else None,
                        }
        except Exception as e:
            log.debug("get_tasks_waiver_prefetch_failed", error=str(e))
        # Map waived_by id -> short_id/name for display
        waver_names: dict[str, str] = {}
        try:
            from hiveweave.services.org import OrgService

            org = OrgService()
            for w in waiver_by_task.values():
                wid = w.get("waived_by")
                if wid and wid not in waver_names:
                    a = await org.get_agent(wid)
                    if a:
                        waver_names[wid] = f"{a.get('name','?')} ({a.get('short_id','?')})"
        except Exception:
            pass
        for t in tasks:
            tk = str(t.get("id") or "")
            lines.append(
                f"- [{t.get('status', '?')}] {t.get('title', '?')} "
                f"(id={tk}, short={tk[:8]}, "
                f"progress={t.get('progress', 0)}%, "
                f"assignee={t.get('assignee_id') or 'unassigned'})"
            )
            # waiver_by_task is keyed by the canonical (dash-stripped) form that
            # `create_waiver` stored — look up with the same canonical key so the
            # dotted task id never misses (P0-1 waiver visibility regression).
            wv = waiver_by_task.get(
                (await canonical_task_id(project_id, tk)) or tk
            )
            if wv:
                wname = waver_names.get(
                    str(wv.get("waived_by")), str(wv.get("waived_by") or "?")[:8]
                )
                lines.append(
                    f"    waiver: waived_by={wname} "
                    f"expires_at={wv.get('waiver_expires_at')} "
                    f"(waived_by CANNOT approve; rework clears waiver)"
                )
            case = case_by_verify.get(tk) or case_by_original.get(tk)
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
