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
from typing import Any

import structlog

log = structlog.get_logger(__name__)

Epistemic = str  # "verified" | "claimed" | "unknown"


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


def _compact_task(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": (t.get("id") or "")[:12],
        "title": t.get("title"),
        "status": t.get("status"),
        "role_hint": t.get("role_hint"),
        "progress": t.get("progress"),
        "assignee_id": (t.get("assignee_id") or "")[:12] or None,
        "reviewer_id": (t.get("reviewer_id") or "")[:12] or None,
    }


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

    # ── Task ledger obligations (verified) ───────────────
    obligations: list[dict[str, Any]] = []
    try:
        from hiveweave.services.task import TaskService

        obligations = await TaskService().get_actionable_obligations(
            project_id, agent_id
        )
        verified.append(
            _entry(
                "ledger.obligations",
                [_compact_task(t) for t in obligations],
                epistemic="verified",
                source="tasks",
            )
        )
    except Exception as e:
        unknown.append(
            _entry(
                "ledger.obligations",
                None,
                epistemic="unknown",
                source="tasks",
                note=str(e),
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

    # ── Slices (not landed yet → unknown) ────────────────
    unknown.append(
        _entry(
            "slices.chain",
            None,
            epistemic="unknown",
            source="contract_json",
            note=(
                "Slice DAG / contract_json not deployed yet. "
                "Do not invent slice status from peer chat."
            ),
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
            "obligations": [_compact_task(t) for t in obligations],
        },
        "org": org_summary,
        "rule": (
            "Other agents' free-text claims are clues only. "
            "When they conflict with verified entries here, trust the platform "
            "and report the conflict."
        ),
    }


def format_platform_state(snapshot: dict[str, Any]) -> str:
    """Render snapshot as LLM-readable markdown with epistemology sections."""
    epi = snapshot.get("epistemology") or {}
    lines = [
        "# Platform State",
        f"project={snapshot.get('project_id')} agent={str(snapshot.get('agent_id') or '')[:12]}",
        f"generated_at_ms={snapshot.get('generated_at_ms')}",
        "",
        snapshot.get("rule") or "",
        "",
        "## VERIFIED (trust these)",
    ]
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
