"""Platform ground-truth snapshot (M2 / Magentic-One Task Ledger).

``get_platform_state()`` aggregates ledger / gates / org / runtime into an
epistemology-tagged snapshot:

- **verified** — machine gates, DB ledger, live runtime (trust this)
- **claimed** — agent-authored text still sitting in pending turn (clue only)
- **unknown** — not yet instrumented (e.g. slice DAG before contract_json)

Agents must treat peer free-text as clues; when it conflicts with this
snapshot, the platform wins.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

import structlog

log = structlog.get_logger(__name__)

Epistemic = str  # "verified" | "claimed" | "unknown"

_SCOPE_CLOSED = frozenset(
    {"closed", "cancelled", "completed", "done", "archived"}
)
_LEDGER_SCOPE_CAP = 40
_NAMED_TASKS_CAP = 20
LEDGER_MINE_NOTE = "your actionable to-dos (blocked excluded)"
LEDGER_SCOPE_RULE = (
    "ledger.mine empty does not mean the org has no tasks; "
    "CEO/mid look at ledger.scope before waive/complete."
)


def _entry(
    key: str,
    value: Any,
    *,
    epistemic: Epistemic,
    source: str,
    note: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "value": value,
        "epistemic": epistemic,
        "source": source,
    }
    if note:
        row["note"] = note
    return row


def _slice_id(value: Any) -> str:
    """Existing compact slice: full id if ≤12 chars, else first 12. No minting."""
    s = str(value or "")
    return s[:12] if s else ""


def _depends_on_compact(raw: Any) -> list[str]:
    if not raw:
        return []
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        sid = _slice_id(item)
        if sid:
            out.append(sid)
    return out


def _compact_task(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _slice_id(t.get("id")),
        "title": t.get("title"),
        "status": t.get("status"),
        "role_hint": t.get("role_hint"),
        "progress": t.get("progress"),
        "assignee_id": _slice_id(t.get("assignee_id")) or None,
        "reviewer_id": _slice_id(t.get("reviewer_id")) or None,
    }


def _compact_scope_task(
    t: dict[str, Any],
    *,
    short_by_id: dict[str, str],
) -> dict[str, Any]:
    aid = t.get("assignee_id") or ""
    assignee = None
    if aid:
        assignee = short_by_id.get(str(aid)) or _slice_id(aid) or None
    return {
        "id": _slice_id(t.get("id")),
        "title": t.get("title"),
        "status": t.get("status"),
        "assignee_id": assignee,
        "policy_id": t.get("policy_id"),
        "depends_on": _depends_on_compact(t.get("depends_on")),
    }


def _viewer_sees_project_scope(agent_row: dict[str, Any] | None) -> bool:
    """CEO-family viewers see every open task, including blocked."""
    if not agent_row:
        return False
    from hiveweave.services.policy import infer_role_family

    if infer_role_family(agent_row) == "ceo":
        return True
    if (agent_row.get("role") or "").strip().lower() == "ceo":
        return True
    return False


def _scope_sort_key(t: dict[str, Any]) -> tuple[int, int]:
    blocked_rank = 0 if (t.get("status") or "").lower() == "blocked" else 1
    ts = t.get("updated_at")
    if ts is None:
        ts = t.get("created_at") or 0
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        ts_i = 0
    return (blocked_rank, -ts_i)


def _short_id_map(agents: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in agents:
        aid = a.get("id")
        sid = a.get("short_id")
        if aid and sid:
            out[str(aid)] = str(sid)
    return out


async def build_platform_state(
    *,
    agent_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Build a platform state snapshot for ``agent_id`` in ``project_id``."""
    verified: list[dict[str, Any]] = []
    claimed: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    # ── Agent identity + live runtime (verified) ─────────
    agent_row: dict[str, Any] | None = None
    try:
        from hiveweave.services.org import OrgService

        agent_row = await OrgService().get_agent(agent_id)
    except Exception as e:
        log.warning("platform_state.agent_failed", error=str(e))
        unknown.append(
            _entry(
                "agent.record",
                None,
                epistemic="unknown",
                source="org",
                note=str(e),
            )
        )

    execution = "unknown"
    disposition = "unknown"
    no_progress = 0
    try:
        from hiveweave.agents.supervisor import agent_manager

        live = agent_manager.get_agent(agent_id)
        if live is None:
            execution = "offline"
            disposition = "runnable"
        else:
            st = getattr(getattr(live, "status", None), "value", None) or ""
            execution = "processing" if st == "processing" else "idle"
            disposition = getattr(live, "disposition", None) or "runnable"
            no_progress = int(getattr(live, "_no_progress_streak", 0) or 0)
        verified.append(
            _entry(
                "agent.execution",
                execution,
                epistemic="verified",
                source="agent_manager",
            )
        )
        verified.append(
            _entry(
                "agent.disposition",
                disposition,
                epistemic="verified",
                source="agent_manager",
            )
        )
        if no_progress:
            verified.append(
                _entry(
                    "agent.no_progress_streak",
                    no_progress,
                    epistemic="verified",
                    source="agent_manager",
                )
            )
    except Exception as e:
        unknown.append(
            _entry(
                "agent.runtime",
                None,
                epistemic="unknown",
                source="agent_manager",
                note=str(e),
            )
        )

    if agent_row:
        verified.append(
            _entry(
                "agent.identity",
                {
                    "id": agent_id[:12],
                    "short_id": agent_row.get("short_id"),
                    "name": agent_row.get("name"),
                    "role": agent_row.get("role"),
                    "status": agent_row.get("status"),
                    "permission_type": agent_row.get("permission_type"),
                },
                epistemic="verified",
                source="agents",
            )
        )

    # ── Turn gates (verified structure; summary text = claimed) ─
    gates: list[Any] = []
    pending_phase: str | None = None
    try:
        from hiveweave.services.turn_session import get_pending_turn_result

        pending = get_pending_turn_result(agent_id)
        if pending:
            pending_phase = pending.get("phase")
            gates = list(pending.get("gates") or [])
            verified.append(
                _entry(
                    "gates.pending_turn",
                    {
                        "phase": pending_phase,
                        "gates": gates,
                        "end_turn": bool(pending.get("end_turn")),
                    },
                    epistemic="verified",
                    source="turn_session",
                )
            )
            summary = pending.get("summary")
            if summary:
                claimed.append(
                    _entry(
                        "pending_turn.summary",
                        str(summary)[:400],
                        epistemic="claimed",
                        source="commit_turn",
                        note="Agent-authored; not platform-verified.",
                    )
                )
            waiting_on = pending.get("waiting_on")
            if waiting_on:
                claimed.append(
                    _entry(
                        "pending_turn.waiting_on",
                        waiting_on,
                        epistemic="claimed",
                        source="commit_turn",
                        note="Agent-declared wait; check waits contract below.",
                    )
                )
        else:
            verified.append(
                _entry(
                    "gates.pending_turn",
                    None,
                    epistemic="verified",
                    source="turn_session",
                    note="No pending commit_turn this wake.",
                )
            )
    except Exception as e:
        unknown.append(
            _entry(
                "gates",
                None,
                epistemic="unknown",
                source="turn_session",
                note=str(e),
            )
        )

    # ── Wait contracts (verified) ────────────────────────
    waits: list[dict[str, Any]] = []
    try:
        from hiveweave.services.wait_contract import wait_contract_service

        waits = await wait_contract_service.list_active(project_id, agent_id)
        verified.append(
            _entry(
                "waits.active",
                [
                    {
                        "ref": w.get("ref"),
                        "wake_on": w.get("wake_on"),
                        "expires_at": w.get("expires_at"),
                    }
                    for w in (waits or [])
                ],
                epistemic="verified",
                source="agent_waits",
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "waits",
                None,
                epistemic="unknown",
                source="agent_waits",
                note=str(e),
            )
        )

    # ── Task ledger: mine (actionable) vs scope (open, incl. blocked)
    obligations: list[dict[str, Any]] = []
    mine_compact: list[dict[str, Any]] = []
    scope_compact: list[dict[str, Any]] = []
    scope_truncated = False
    scope_status_counts: dict[str, int] = {}
    named_tasks: list[dict[str, Any]] = []
    try:
        from hiveweave.services.task import TaskService

        obligations = await TaskService().get_actionable_obligations(
            project_id, agent_id
        )
        mine_compact = [_compact_task(t) for t in obligations]
        verified.append(
            _entry(
                "ledger.mine",
                mine_compact,
                epistemic="verified",
                source="tasks",
                note=LEDGER_MINE_NOTE,
            )
        )
        # Alias: existing consumers (and identity copy) still read obligations.
        verified.append(
            _entry(
                "ledger.obligations",
                mine_compact,
                epistemic="verified",
                source="tasks",
                note=LEDGER_MINE_NOTE,
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "ledger.mine",
                None,
                epistemic="unknown",
                source="tasks",
                note=str(e),
            )
        )
        unknown.append(
            _entry(
                "ledger.obligations",
                None,
                epistemic="unknown",
                source="tasks",
                note=str(e),
            )
        )

    try:
        from hiveweave.db import project as project_db
        from hiveweave.services.org import OrgService
        from hiveweave.services.task import TaskService

        org_svc = OrgService()
        agents_for_ids: list[dict[str, Any]] = []
        try:
            agents_for_ids = await org_svc.list_agents(project_id)
        except Exception:
            agents_for_ids = []
        short_by_id = _short_id_map(agents_for_ids)

        all_open = await TaskService().list_tasks(project_id)
        project_wide = _viewer_sees_project_scope(agent_row)
        descendant_ids: set[str] = set()
        if not project_wide:
            try:
                descendants = await org_svc.get_all_descendants(agent_id)
                descendant_ids = {
                    str(d["id"]) for d in descendants if d.get("id")
                }
            except Exception as e:
                log.warning(
                    "platform_state.descendants_failed",
                    agent_id=agent_id[:12],
                    error=str(e),
                )
        assignee_ok = {agent_id} | descendant_ids

        scoped: list[dict[str, Any]] = []
        for t in all_open:
            status = (t.get("status") or "").lower()
            if status in _SCOPE_CLOSED:
                continue
            if project_wide:
                scoped.append(t)
                continue
            if t.get("creator_id") == agent_id:
                scoped.append(t)
                continue
            if t.get("assignee_id") in assignee_ok:
                scoped.append(t)
        scope_status_counts = dict(
            Counter((t.get("status") or "unknown") for t in scoped)
        )
        scoped.sort(key=_scope_sort_key)
        scope_truncated = len(scoped) > _LEDGER_SCOPE_CAP
        scope_compact = [
            _compact_scope_task(t, short_by_id=short_by_id)
            for t in scoped[:_LEDGER_SCOPE_CAP]
        ]
        verified.append(
            _entry(
                "ledger.scope",
                scope_compact,
                epistemic="verified",
                source="tasks",
                note=LEDGER_SCOPE_RULE,
            )
        )
        if scope_truncated:
            verified.append(
                _entry(
                    "ledger.scope_truncated",
                    True,
                    epistemic="verified",
                    source="tasks",
                )
            )
            verified.append(
                _entry(
                    "ledger.scope_status_counts",
                    scope_status_counts,
                    epistemic="verified",
                    source="tasks",
                    note="Counts for the full untruncated scope set.",
                )
            )

        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT from_agent_id, task_id, message_type FROM inbox "
                "WHERE to_agent_id = ? AND COALESCE(read, 0) = 0 "
                "AND task_id IS NOT NULL AND TRIM(task_id) != '' "
                "ORDER BY created_at DESC LIMIT ?",
                [agent_id, _NAMED_TASKS_CAP],
            )
            inbox_rows = await cur.fetchall()
            await cur.close()
            for r in inbox_rows:
                d = dict(r)
                fid = d.get("from_agent_id") or ""
                named_tasks.append(
                    {
                        "from": short_by_id.get(str(fid))
                        or (_slice_id(fid) or None),
                        "task_id": d.get("task_id"),
                        "message_type": d.get("message_type"),
                    }
                )
        except Exception as e:
            log.warning(
                "platform_state.named_tasks_failed",
                agent_id=agent_id[:12],
                error=str(e),
            )
        verified.append(
            _entry(
                "inbox.named_tasks",
                named_tasks,
                epistemic="verified",
                source="inbox",
                note="Unread rows with structured task_id; body text is ignored.",
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "ledger.scope",
                None,
                epistemic="unknown",
                source="tasks",
                note=str(e),
            )
        )

    # ── Verification cases (verified) ────────────────────
    try:
        from hiveweave.services.task import VerificationCaseService

        cases = await VerificationCaseService().list_cases_for_project(
            project_id, limit=20
        )
        verified.append(
            _entry(
                "ledger.verification_cases",
                [
                    {
                        "id": (c.get("id") or "")[:8],
                        "status": c.get("status"),
                        "original_task_id": (c.get("original_task_id") or "")[:8],
                        "verify_task_id": (c.get("verify_task_id") or "")[:8],
                        "merge_commit_hash": (
                            (c.get("merge_commit_hash") or "")[:12] or None
                        ),
                        "review_notes": (c.get("review_notes") or "")[:160],
                        "qa_agent_id": (
                            (c.get("qa_agent_id") or "")[:8] or None
                        ),
                    }
                    for c in cases
                ],
                epistemic="verified",
                source="verification_cases",
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "ledger.verification_cases",
                None,
                epistemic="unknown",
                source="verification_cases",
                note=str(e),
            )
        )

    # Stale worktree_error on self (verified)
    if agent_row and agent_row.get("worktree_error"):
        verified.append(
            _entry(
                "agent.worktree_error",
                str(agent_row.get("worktree_error"))[:200],
                epistemic="verified",
                source="agents",
                note="Clear via ensure worktree / restart heal if tree is healthy.",
            )
        )

    # ── Org snapshot + dismiss quota (verified) ──────────
    org_summary: dict[str, Any] = {}
    try:
        from hiveweave.services.org import OrgService
        from hiveweave.services.org_guardrails import (
            DISMISS_QUOTA_PER_GAME_DAY,
            current_game_day,
        )
        from hiveweave.db import project as project_db

        agents = await OrgService().list_agents(project_id)
        active = [
            a
            for a in agents
            if (a.get("status") or "active") == "active"
        ]
        org_summary = {
            "active_count": len(active),
            "archived_count": len(agents) - len(active),
            "active": [
                {
                    "short_id": a.get("short_id"),
                    "name": a.get("name"),
                    "role": a.get("role"),
                    "permission_type": a.get("permission_type"),
                }
                for a in active[:40]
            ],
        }
        day = await current_game_day(project_id)
        dismiss_n = 0
        try:
            conn = await project_db.get_project_db_by_project_id(project_id)
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM org_dismiss_log "
                "WHERE project_id = ? AND game_day = ?",
                [project_id, day],
            )
            row = await cur.fetchone()
            await cur.close()
            dismiss_n = int(row["n"] if row else 0)
        except Exception:
            dismiss_n = 0
        org_summary["dismiss_quota"] = {
            "game_day": day,
            "used": dismiss_n,
            "limit": DISMISS_QUOTA_PER_GAME_DAY,
            "remaining": max(0, DISMISS_QUOTA_PER_GAME_DAY - dismiss_n),
        }
        verified.append(
            _entry(
                "org.snapshot",
                org_summary,
                epistemic="verified",
                source="agents+org_dismiss_log",
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "org",
                None,
                epistemic="unknown",
                source="org",
                note=str(e),
            )
        )

    # ── Slices (contract_json) ───────────────────────────
    try:
        from hiveweave.services.task import TaskService
        from hiveweave.services.task_contract import (
            parse_contract,
            slice_id_of,
        )

        slice_rows: list[dict[str, Any]] = []
        for t in obligations:
            c = parse_contract(t.get("contract_json"))
            if not c:
                continue
            slice_rows.append(
                {
                    "slice_id": slice_id_of(c),
                    "slice_status": c.get("slice_status"),
                    "task_id": (t.get("id") or "")[:12],
                    "task_status": t.get("status"),
                    "pre_run_passed": (c.get("machine_pre_run") or {}).get(
                        "passed"
                    ),
                }
            )
        if slice_rows:
            verified.append(
                _entry(
                    "slices.active_obligations",
                    slice_rows,
                    epistemic="verified",
                    source="tasks.contract_json",
                )
            )
        else:
            verified.append(
                _entry(
                    "slices.active_obligations",
                    [],
                    epistemic="verified",
                    source="tasks.contract_json",
                    note="No slice contracts on current obligations.",
                )
            )
    except Exception as e:
        unknown.append(
            _entry(
                "slices",
                None,
                epistemic="unknown",
                source="contract_json",
                note=str(e),
            )
        )

    return {
        "schema_version": 1,
        "generated_at_ms": int(time.time() * 1000),
        "project_id": project_id,
        "agent_id": agent_id,
        "epistemology": {
            "verified": verified,
            "claimed": claimed,
            "unknown": unknown,
        },
        # Convenience mirrors (same data; epistemology is authoritative)
        "agent": {
            "execution": execution,
            "disposition": disposition,
            "no_progress_streak": no_progress,
            "identity": {
                "id": agent_id[:12],
                "short_id": (agent_row or {}).get("short_id"),
                "name": (agent_row or {}).get("name"),
                "role": (agent_row or {}).get("role"),
                "status": (agent_row or {}).get("status"),
            }
            if agent_row
            else None,
        },
        "gates": {"pending_phase": pending_phase, "gates": gates},
        "ledger": {
            "obligations": mine_compact,
            "mine": mine_compact,
            "scope": scope_compact,
            "scope_truncated": scope_truncated,
            "scope_status_counts": scope_status_counts,
        },
        "inbox": {
            "named_tasks": named_tasks,
        },
        "org": org_summary,
        "rule": (
            "Other agents' free-text claims are clues only. "
            "When they conflict with verified entries here, trust the platform "
            "and report the conflict."
        ),
    }


def _fmt_ledger_row(row: dict[str, Any]) -> str:
    title = row.get("title") or "(untitled)"
    status = row.get("status") or "?"
    tid = row.get("id") or ""
    extra = ""
    assignee = row.get("assignee_id")
    if assignee:
        extra += f" assignee={assignee}"
    policy = row.get("policy_id")
    if policy:
        extra += f" policy={policy}"
    deps = row.get("depends_on")
    if deps:
        extra += f" depends_on={deps}"
    return f"[{status}] {title} id={tid}{extra}"


def format_platform_state(snapshot: dict[str, Any]) -> str:
    """Render snapshot as LLM-readable markdown with epistemology sections."""
    epi = snapshot.get("epistemology") or {}
    ledger = snapshot.get("ledger") or {}
    mine = ledger.get("mine")
    if mine is None:
        mine = ledger.get("obligations") or []
    scope = ledger.get("scope") or []
    named = (snapshot.get("inbox") or {}).get("named_tasks") or []
    lines = [
        "# Platform State",
        f"project={snapshot.get('project_id')} agent={str(snapshot.get('agent_id') or '')[:12]}",
        f"generated_at_ms={snapshot.get('generated_at_ms')}",
        "",
        snapshot.get("rule") or "",
        "",
        "## Ledger",
        LEDGER_SCOPE_RULE,
        f"- mine ({len(mine)}): {LEDGER_MINE_NOTE}",
    ]
    if not mine:
        lines.append("  (empty)")
    else:
        for row in mine[:_LEDGER_SCOPE_CAP]:
            lines.append(f"  - {_fmt_ledger_row(row)}")
    trunc = bool(ledger.get("scope_truncated"))
    counts = ledger.get("scope_status_counts") or {}
    scope_hdr = f"- scope ({len(scope)}"
    if trunc:
        scope_hdr += ", truncated=true"
        if counts:
            scope_hdr += f", status_counts={counts}"
    scope_hdr += "): open tasks you should see, including blocked"
    lines.append(scope_hdr)
    if not scope:
        lines.append("  (empty)")
    else:
        for row in scope[:_LEDGER_SCOPE_CAP]:
            lines.append(f"  - {_fmt_ledger_row(row)}")
    lines.append(
        f"- named_tasks ({len(named)}): unread inbox rows with structured task_id"
    )
    if not named:
        lines.append("  (none)")
    else:
        for row in named[:_NAMED_TASKS_CAP]:
            lines.append(
                f"  - from={row.get('from')} task_id={row.get('task_id')} "
                f"type={row.get('message_type')}"
            )
    lines.extend(
        [
            "",
            "## VERIFIED (trust these)",
        ]
    )
    for row in epi.get("verified") or []:
        lines.append(
            f"- `{row.get('key')}` ← {row.get('source')}: "
            f"{_fmt_value(row.get('value'))}"
        )
        if row.get("note"):
            lines.append(f"  note: {row['note']}")

    lines.append("")
    lines.append("## CLAIMED (agent-authored — not facts)")
    claimed = epi.get("claimed") or []
    if not claimed:
        lines.append("- (none)")
    else:
        for row in claimed:
            lines.append(
                f"- `{row.get('key')}` ← {row.get('source')}: "
                f"{_fmt_value(row.get('value'))}"
            )
            if row.get("note"):
                lines.append(f"  note: {row['note']}")

    lines.append("")
    lines.append("## UNKNOWN (do not invent)")
    for row in epi.get("unknown") or []:
        lines.append(
            f"- `{row.get('key')}` ← {row.get('source')}: "
            f"{row.get('note') or 'unknown'}"
        )

    return "\n".join(lines)


def _fmt_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (str, int, float, bool)):
        s = str(v)
        return s if len(s) <= 200 else s[:200] + "…"
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s if len(s) <= 500 else s[:500] + "…"
