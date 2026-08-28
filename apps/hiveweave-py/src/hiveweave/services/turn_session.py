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
    "UNCOMMITTED_WORKTREE",
    # P0-2: CEO 项目级待办首次命中即硬拒（带明细），避免 soft-pass 后
    # CEO 仍收工导致项目卡死；backstop 同口径兜底。
    "CEO_PROJECT_PENDING",
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


# ── defer 连发断路器（P0-4, TEST_DSH_33）──────────────────
# 砚舟在 257 分钟内 defer 17 次，reason 几乎一字不差，平台无限静默接受。
# 同一 agent 连续用语义相同的 reason 关催办 = 停滞而非等待：达阈值后
# defer 不再静默接受（工具侧升级 + work_log 留痕），且 [TASK ADVANCE]
# 恢复提醒。streak 故意**不**随外部唤醒清除（跨唤醒复读正是本例形态），
# 只在 reason 变化或本轮真正推动了账本时清零。

DEFER_REASON_STREAK_LIMIT = 3
# 归一化后取前 N 字符做 key：容忍尾部细微改写，抓住"同一句话"复读。
DEFER_REASON_KEY_CHARS = 80

_defer_reason_streak: dict[str, tuple[str, int]] = {}


def normalize_defer_reason(reason: str) -> str:
    """Semantic key for a defer reason — whitespace/case-insensitive prefix."""
    compact = "".join(str(reason or "").split()).lower()
    return compact[:DEFER_REASON_KEY_CHARS]


def record_defer_reason(agent_id: str, reason: str) -> int:
    """Count consecutive defers carrying the same reason key. Returns the count."""
    key = normalize_defer_reason(reason)
    with _lock:
        prev_key, prev_n = _defer_reason_streak.get(agent_id, ("", 0))
        n = prev_n + 1 if key and key == prev_key else 1
        _defer_reason_streak[agent_id] = (key, n)
        return n


def defer_reason_streak(agent_id: str) -> int:
    with _lock:
        return _defer_reason_streak.get(agent_id, ("", 0))[1]


def defer_breaker_tripped(agent_id: str) -> bool:
    """True once the same reason has been deferred ``LIMIT`` times in a row."""
    return defer_reason_streak(agent_id) >= DEFER_REASON_STREAK_LIMIT


def clear_defer_reason_streak(agent_id: str) -> None:
    with _lock:
        _defer_reason_streak.pop(agent_id, None)
