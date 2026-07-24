"""In-memory TurnResult buffer for the current agent turn.

``commit_turn`` writes here; exit gates read/clear. Also persisted to work_logs.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}

# TEST11 evening P2-2: per-turn soft-warn ledger for commit_turn gate codes.
# First hit of a violation code → soft-pass (warn + allow); second → hard reject.
_soft_warn_counts: dict[str, dict[str, int]] = {}
_soft_passed: dict[str, set[str]] = {}


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
        _soft_passed.pop(agent_id, None)


def classify_commit_gate_soft_warn(
    agent_id: str, violations: list[str]
) -> tuple[list[str], list[str]]:
    """Split gate violations into (soft_pass, hard_reject) for this turn.

    First occurrence of each code soft-passes (count 0→1) and is recorded in
    ``_soft_passed`` so ``evaluate_turn_exit`` does not immediately re-block.
    Subsequent occurrences hard-reject.
    """
    soft: list[str] = []
    hard: list[str] = []
    with _lock:
        bag = _soft_warn_counts.setdefault(agent_id, {})
        passed = _soft_passed.setdefault(agent_id, set())
        for v in violations:
            code = str(v)
            n = bag.get(code, 0)
            if n == 0:
                bag[code] = 1
                passed.add(code)
                soft.append(code)
            else:
                bag[code] = n + 1
                hard.append(code)
    return soft, hard


def filter_soft_passed_violations(
    agent_id: str, violations: list[str]
) -> list[str]:
    """Drop violation codes that soft-passed earlier this turn."""
    with _lock:
        passed = _soft_passed.get(agent_id) or set()
        if not passed:
            return list(violations)
        return [v for v in violations if v not in passed]


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
