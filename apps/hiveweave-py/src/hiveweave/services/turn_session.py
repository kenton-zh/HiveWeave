"""In-memory TurnResult buffer for the current agent turn.

``commit_turn`` writes here; exit gates read/clear. Also persisted to work_logs.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}

# Per-turn soft-warn ledger for *reminder-class* commit_turn gate codes.
# First hit → soft-pass (warn); second → hard reject at pre-check.
# Soft-pass NEVER suppresses the authoritative evaluate_turn_exit backstop
# (TEST14 BUG-1/P0c): reply contracts must remain enforceable.
_soft_warn_counts: dict[str, dict[str, int]] = {}

# Reply-contract / protocol codes — always hard-reject at pre-check.
# Soft-warn was designed to cut LLM round-trips; for UNREPLIED_ASKS that
# traded the whole org for one skipped send_message (TEST14 freeze).
HARD_COMMIT_GATE_CODES = frozenset({
    "UNREPLIED_ASKS",
})


def set_pending_turn_result(agent_id: str, payload: dict[str, Any]) -> None:
    with _lock:
        _pending[agent_id] = payload


def get_pending_turn_result(agent_id: str) -> dict[str, Any] | None:
    with _lock:
        raw = _pending.get(agent_id)
        return dict(raw) if raw else None


def pop_pending_turn_result(agent_id: str) -> dict[str, Any] | None:
    with _lock:
        raw = _pending.pop(agent_id, None)
        return dict(raw) if raw else None


def clear_pending_turn_result(agent_id: str) -> None:
    with _lock:
        _pending.pop(agent_id, None)
        _soft_warn_counts.pop(agent_id, None)


def classify_commit_gate_soft_warn(
    agent_id: str, violations: list[str]
) -> tuple[list[str], list[str]]:
    """Split gate violations into (soft_pass, hard_reject) for this turn.

    ``HARD_COMMIT_GATE_CODES`` (e.g. UNREPLIED_ASKS) always hard-reject.
    Other codes: first occurrence soft-passes (count 0→1); subsequent
    occurrences hard-reject. Soft-pass does **not** suppress
    ``evaluate_turn_exit`` — the backstop keeps repair/retrigger authority.
    """
    soft: list[str] = []
    hard: list[str] = []
    with _lock:
        bag = _soft_warn_counts.setdefault(agent_id, {})
        for v in violations:
            code = str(v)
            if code in HARD_COMMIT_GATE_CODES:
                bag[code] = bag.get(code, 0) + 1
                hard.append(code)
                continue
            n = bag.get(code, 0)
            if n == 0:
                bag[code] = 1
                soft.append(code)
            else:
                bag[code] = n + 1
                hard.append(code)
    return soft, hard


def filter_soft_passed_violations(
    agent_id: str, violations: list[str]
) -> list[str]:
    """No-op: soft-pass must not strip backstop violations (TEST14 P0c).

    Kept for import compatibility; always returns a copy of ``violations``.
    """
    return list(violations)


# ── task-advance defer (explicit "不推进") ─────────────────
# Set by defer_task_advance tool; cleared on next external wake.
# While set, agent.turn.after must not inject [TASK ADVANCE] nudges.

_defer_advance: dict[str, bool] = {}


def set_task_advance_deferred(agent_id: str, deferred: bool = True) -> None:
    with _lock:
        if deferred:
            _defer_advance[agent_id] = True
        else:
            _defer_advance.pop(agent_id, None)


def is_task_advance_deferred(agent_id: str) -> bool:
    with _lock:
        return bool(_defer_advance.get(agent_id))


def clear_task_advance_deferred(agent_id: str) -> None:
    with _lock:
        _defer_advance.pop(agent_id, None)
