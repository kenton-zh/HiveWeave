"""Org-level blast-radius guards (DESIGN-3).

YLGY lesson: hallucination alone did not destroy the org — hallucination ×
unbounded dismiss/hire did. These gates are small, deterministic controllers:

1. **Dismiss quota** — at most ``DISMISS_QUOTA_PER_GAME_DAY`` dismissals per
   project per game day (1 game day = 1 real hour).
2. **Same-role rehire cooldown** — hiring a role that was dismissed within
   ``SAME_ROLE_REHIRE_COOLDOWN_GAME_DAYS`` is hard-rejected; prefer
   ``transfer_agent`` / ``bind_skill``.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog

from hiveweave.db import project as project_db
from hiveweave.services.game_time import (
    GAME_SECONDS_PER_DAY,
    REAL_SECONDS_PER_GAME_DAY,
)

log = structlog.get_logger(__name__)

# Magentic-One-sized levers: small N, big radius control.
DISMISS_QUOTA_PER_GAME_DAY = 3
SAME_ROLE_REHIRE_COOLDOWN_GAME_DAYS = 1


def _normalize_role_key(role: str) -> str:
    return re.sub(r"\s+", "", (role or "").strip().lower())


async def current_game_day(project_id: str) -> int:
    """Return the project's current game-day index (0-based).

    Falls back to wall-clock buckets of ``REAL_SECONDS_PER_GAME_DAY`` when
    game time is unavailable (tests / pre-activate).
    """
    try:
        from hiveweave.services.game_time import GameTimeService

        gt = await GameTimeService().get_current_time(project_id)
        gs = int(gt.get("game_seconds") or 0)
        return gs // GAME_SECONDS_PER_DAY
    except Exception as e:
        log.debug(
            "org_guardrails.game_day_fallback",
            project_id=project_id,
            error=str(e),
        )
        return int(time.time()) // REAL_SECONDS_PER_GAME_DAY


async def check_dismiss_quota(project_id: str) -> str | None:
    """Return an error message if today's dismiss quota is exhausted."""
    day = await current_game_day(project_id)
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM org_dismiss_log "
            "WHERE project_id = ? AND game_day = ?",
            [project_id, day],
        )
        row = await cur.fetchone()
        await cur.close()
        n = int(row["n"] if row else 0)
    except Exception as e:
        # Fail-open on schema/read errors so dismiss still works if migration
        # has not run yet; log loudly.
        log.warning(
            "org_guardrails.dismiss_quota_check_failed",
            project_id=project_id,
            error=str(e),
        )
        return None

    if n >= DISMISS_QUOTA_PER_GAME_DAY:
        return (
            f"Dismiss quota exhausted for this project on game day {day} "
            f"({n}/{DISMISS_QUOTA_PER_GAME_DAY}). "
            "Prefer transfer_agent / bind_skill over dismiss+rehire. "
            "Wait for the next game day, or ask the user via the question "
            "tool before further dismissals."
        )
    return None


async def record_dismiss(
    project_id: str,
    *,
    agent_id: str,
    role: str,
    dismissed_by: str | None = None,
    short_id: str | None = None,
    name: str | None = None,
) -> None:
    """Persist a dismiss event for quota + same-role cooldown."""
    day = await current_game_day(project_id)
    now_ms = int(time.time() * 1000)
    role_key = _normalize_role_key(role)
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        await conn.execute(
            "INSERT INTO org_dismiss_log "
            "(id, project_id, agent_id, role, role_key, short_id, name, "
            " game_day, dismissed_by, dismissed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(uuid.uuid4()),
                project_id,
                agent_id,
                role or "",
                role_key,
                short_id or "",
                name or "",
                day,
                dismissed_by or "",
                now_ms,
            ],
        )
        await conn.commit()
        log.info(
            "org_guardrails.dismiss_recorded",
            project_id=project_id,
            agent_id=agent_id,
            role=role,
            game_day=day,
        )
    except Exception as e:
        log.warning(
            "org_guardrails.dismiss_record_failed",
            project_id=project_id,
            agent_id=agent_id,
            error=str(e),
        )


async def check_same_role_rehire(
    project_id: str, role: str
) -> str | None:
    """Hard-reject hiring a role dismissed within the cooldown window."""
    role_key = _normalize_role_key(role)
    if not role_key:
        return None
    day = await current_game_day(project_id)
    since_day = day - (SAME_ROLE_REHIRE_COOLDOWN_GAME_DAYS - 1)
    try:
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT agent_id, role, name, short_id, game_day, dismissed_at "
            "FROM org_dismiss_log "
            "WHERE project_id = ? AND role_key = ? AND game_day >= ? "
            "ORDER BY dismissed_at DESC LIMIT 1",
            [project_id, role_key, since_day],
        )
        row = await cur.fetchone()
        await cur.close()
    except Exception as e:
        log.warning(
            "org_guardrails.rehire_check_failed",
            project_id=project_id,
            role=role,
            error=str(e),
        )
        return None

    if not row:
        return None

    d = dict(row)
    return (
        f"Same-role rehire blocked: role '{d.get('role') or role}' was "
        f"dismissed on game day {d.get('game_day')} "
        f"({d.get('name') or '?'}/{d.get('short_id') or d.get('agent_id', '')[:8]}). "
        f"Cooldownoldown is {SAME_ROLE_REHIRE_COOLDOWN_GAME_DAYS} game day(s). "
        "Do NOT dismiss+rehire to 'fix' a person — use transfer_agent or "
        "bind_skill instead."
    )


def dismiss_quota_snapshot(rows: list[dict[str, Any]], game_day: int) -> dict:
    """Test helper: summarise dismiss rows for a game day."""
    today = [r for r in rows if int(r.get("game_day") or -1) == game_day]
    return {
        "game_day": game_day,
        "count": len(today),
        "quota": DISMISS_QUOTA_PER_GAME_DAY,
        "remaining": max(0, DISMISS_QUOTA_PER_GAME_DAY - len(today)),
    }
