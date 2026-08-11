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

        # code_audit 视角：每任务透出其 assignee 最近一次审计结论（只读）。
        # 与 waiver 预取同模式 —— 一次 IN 查询取最新行，按 agent 分组，无 N+1。
        latest_audit_by_agent: dict[str, str] = {}
        try:
            from hiveweave.services.attestation import (
                _conn as _attestation_conn_audit,
                attestation_service,
                parse_audit_verdict,
            )
            from hiveweave.services.code_audit import CODE_AUDIT_KIND

            await attestation_service.ensure_schema(project_id)
            agent_ids = sorted(
                {
                    str(t.get("assignee_id") or "")
                    for t in tasks
                    if t.get("assignee_id")
                }
            )
            if agent_ids:
                _placeholders = ",".join(["?"] * len(agent_ids))
                _now_ms = int(time.time() * 1000)
                conn = await _attestation_conn_audit(project_id)
                cur = await conn.execute(
                    "SELECT agent_id, command_or_url, exit_code "
                    "FROM tool_attestations "
                    "WHERE project_id = ? AND kind = ? "
                    f"AND agent_id IN ({_placeholders}) "
                    "AND created_at >= ? "
                    "ORDER BY created_at DESC",
                    [project_id, CODE_AUDIT_KIND, *agent_ids, _now_ms - 3600_000],
                )
                rows = await cur.fetchall()
                await cur.close()
                seen: set[str] = set()
                for r in rows:
                    aid = str(r["agent_id"]) if "agent_id" in r.keys() else ""
                    if not aid or aid in seen:
                        continue
                    seen.add(aid)
                    verdict = parse_audit_verdict(dict(r))
                    if verdict:
                        latest_audit_by_agent[aid] = verdict
        except Exception as e:
            log.debug("get_tasks_latest_audit_prefetch_failed", error=str(e))

        # 撞门前透出 VERIFY 串行锁 + attestation 基线状态（claim/review
        # 前置）。slack-clone_01 成本审计根因二：claim 54% 撞门 —— get_tasks
        # 不透锁状态，QA 反复 claim 被挡；review 23% —— attestation 基线过期
        # 只在 review_task 才暴露。此处每行只读透出，_in_flight_verify_task /
        # check_verify_baseline 原样复用（不重写逻辑）。
        _f2_ts = _task_svc.TaskService()
        _f2_holder = None
        try:
            from hiveweave.tools.tasks.verify_spawn import _in_flight_verify_task

            _f2_holder = await _in_flight_verify_task(project_id)
        except Exception:
            _f2_holder = None
        _f2_queued = sorted(
            (
                t
                for t in tasks
                if _f2_ts._is_verify_task(t)
                and t.get("status") == "created"
                and t.get("assignee_id")
            ),
            key=lambda t: ((t.get("created_at") or 0), (t.get("id") or "")),
        )
        _f2_queue_pos = {
            str(t.get("id") or ""): i for i, t in enumerate(_f2_queued)
        }
        for t in tasks:
            tk = str(t.get("id") or "")
            t["verify_in_flight"] = False
            t["verify_in_flight_id"] = None
            t["verify_queue_position"] = None
            t["attestation_baseline_ok"] = None
            if _f2_ts._is_verify_task(t):
                # claim 门按 except_id 自豁免 —— 持有者自己不被挡，其余
                # VERIFY 被当前持有者挡（与 claim.py 同一判定）。
                blocker = None
                if _f2_holder is not None:
                    if str(_f2_holder.get("id")) != tk:
                        blocker = _f2_holder
                    else:
                        try:
                            from hiveweave.tools.tasks.verify_spawn import (
                                _in_flight_verify_task as _ift,
                            )

                            blocker = await _ift(project_id, except_id=tk)
                        except Exception:
                            blocker = None
                t["verify_in_flight"] = blocker is not None
                if blocker:
                    t["verify_in_flight_id"] = str(blocker.get("id") or "")
                t["verify_queue_position"] = _f2_queue_pos.get(tk)
            if t.get("status") == "reviewing":
                try:
                    from hiveweave.services.attestation import check_verify_baseline

                    _baseline_err = await check_verify_baseline(
                        project_id, t, max_behind=5
                    )
                    t["attestation_baseline_ok"] = _baseline_err is None
                except Exception:
                    t["attestation_baseline_ok"] = None
            t["latest_audit_verdict"] = latest_audit_by_agent.get(
                str(t.get("assignee_id") or "")
            )
        if _f2_holder is not None:
            _h = _f2_holder
            lines.append(
                f"verify_serial_lock: held by {str(_h.get('id') or '')[:8]} "
                f"({_h.get('status')}, "
                f"assignee={str(_h.get('assignee_id') or '?')[:12]}) — created "
                f"VERIFY 的 claim 会被挡，直到它收口"
            )
        for t in tasks:
            tk = str(t.get("id") or "")
            lines.append(
                f"- [{t.get('status', '?')}] {t.get('title', '?')} "
                f"(id={tk}, short={tk[:8]}, "
                f"progress={t.get('progress', 0)}%, "
                f"assignee={t.get('assignee_id') or 'unassigned'})"
            )
            if t.get("verify_in_flight"):
                _blk = str(t.get("verify_in_flight_id") or "")[:8]
                lines.append(
                    f"    verify_lock: blocked by in-flight VERIFY "
                    f"({_blk}) — claim waits until MAIN frees"
                )
            _qp = t.get("verify_queue_position")
            if _qp is not None:
                lines.append(
                    "    verify_queue_position: "
                    + (
                        "0 — next in line"
                        if _qp == 0
                        else f"{_qp} ({_qp} VERIFY ahead)"
                    )
                )
            if t.get("attestation_baseline_ok") is True:
                lines.append("    attestation_baseline: ok")
            elif t.get("attestation_baseline_ok") is False:
                lines.append(
                    "    attestation_baseline: STALE — 需在 MAIN 当前 tip "
                    "重跑测试后再 approve"
                )
            _av = t.get("latest_audit_verdict")
            if _av:
                lines.append(f"    latest_audit_verdict: {_av}")
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
            case = case_by_verify.get(tk) or case_by_original.get(tk)  # type: ignore[assignment]
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
