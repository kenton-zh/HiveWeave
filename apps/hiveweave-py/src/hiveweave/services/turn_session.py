"""In-memory TurnResult buffer for the current agent turn.

``commit_turn`` writes here; exit gates read/clear. Also persisted to work_logs.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}


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
