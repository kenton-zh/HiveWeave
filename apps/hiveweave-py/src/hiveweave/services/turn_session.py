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
