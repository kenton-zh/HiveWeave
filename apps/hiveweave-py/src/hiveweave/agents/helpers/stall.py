"""Cross-turn stall-break ledger and progress helpers.

Extracted from agent.py — behavior-preserving mechanical split (P1).
Mutable module state ``_stall_break_ledger`` lives here (not in constants).
"""

from __future__ import annotations

_stall_break_ledger: dict[str, list[int]] = {}  # agent_id → [timestamp_ms, …]


def _turn_has_substantial_progress(
    tool_calls: list | None,
    tasks_advanced: set | list | None = None,
) -> bool:
    """True when this turn produced mutating work (not pure readonly spin).

    TEST21 M6: "spin 过但最终有产出" must not count toward cross-turn park.
    """
    if tasks_advanced:
        return True
    if not tool_calls:
        return False
    try:
        from hiveweave.llm.streamer import DOOM_LOOP_READONLY_TOOLS
    except Exception:
        DOOM_LOOP_READONLY_TOOLS = frozenset()
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or tc.get("tool") or "").strip()
        if name and name not in DOOM_LOOP_READONLY_TOOLS:
            return True
    return False


async def _recent_successful_run_ms(
    agent_id: str, *, exclude_after_ms: int | None = None
) -> int | None:
    """Return ended_at (ms) of the latest completed agent_run, or None."""
    try:
        from hiveweave.db import project as project_db

        # project.query_one routes by agent_id → per-project DB
        row = await project_db.query_one(
            agent_id,
            "SELECT ended_at FROM agent_runs "
            "WHERE agent_id = ? AND status = 'completed' AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT 1",
            [agent_id],
        )
        if not row:
            return None
        ended = row["ended_at"]
        if ended is None:
            return None
        ended_i = int(ended)
        if exclude_after_ms is not None and ended_i >= exclude_after_ms:
            # Current turn just finishing — look one further back
            row2 = await project_db.query_one(
                agent_id,
                "SELECT ended_at FROM agent_runs "
                "WHERE agent_id = ? AND status = 'completed' AND ended_at IS NOT NULL "
                "AND ended_at < ? "
                "ORDER BY ended_at DESC LIMIT 1",
                [agent_id, exclude_after_ms],
            )
            if not row2:
                return None
            ended2 = row2["ended_at"]
            return int(ended2) if ended2 is not None else None
        return ended_i
    except Exception:
        return None
