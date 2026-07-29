"""Soft unblock: non-specific reminders + hard forbids (no next-actor routing).

Platform may state *facts* and *refuse* unlawful shortcuts. It must NOT
emit structured next_action / candidate lists / auto-dispatch.
"""

from __future__ import annotations

from typing import Any

# Non-specific reminder — AI chooses the lawful advance path.
REVIEW_PATH_BLOCKED_REMINDER = (
    "审批路径受阻且证据仍在；勿以 cancel 清场；请自行选择合法推进方式。"
)

# Statuses where work is waiting on review — cancel is the wrong escape hatch
# when machine evidence (waiver or attestation) already exists.
_REVIEW_PIPE_STATUSES = frozenset({"submitted", "reviewing"})


async def review_deadlock_blocks_cancel(
    project_id: str,
    task: dict[str, Any],
) -> str | None:
    """Return forbid reason if cancel would only clear a review deadlock.

    Machine-checkable (no NL intent scan):
    - task is in submitted/reviewing (review pipe)
    - AND a valid waiver exists OR evidence carries attestation_ids / tests_passed
    """
    status = (task.get("status") or "").strip()
    if status not in _REVIEW_PIPE_STATUSES:
        return None

    tid = task.get("id")
    if not tid:
        return None

    from hiveweave.services.attestation import get_valid_waiver

    try:
        if await get_valid_waiver(project_id, str(tid)):
            return (
                f"cancel_task refused for task {str(tid)[:8]}: "
                f"review pipe ({status}) with an active waiver. "
                f"{REVIEW_PATH_BLOCKED_REMINDER}"
            )
    except Exception:
        pass

    evidence = task.get("evidence") or {}
    if isinstance(evidence, str):
        import json

        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = {}
    if not isinstance(evidence, dict):
        return None

    aids = evidence.get("attestation_ids") or []
    has_aids = isinstance(aids, list) and any(str(x).strip() for x in aids)
    tests_ok = evidence.get("tests_passed") is True
    if has_aids or tests_ok:
        return (
            f"cancel_task refused for task {str(tid)[:8]}: "
            f"review pipe ({status}) still has execution evidence. "
            f"{REVIEW_PATH_BLOCKED_REMINDER}"
        )
    return None


def soft_reminder_after_self_review_deny(*, has_waiver: bool) -> str:
    """Append soft reminder when self-review is denied (esp. after waiver)."""
    if not has_waiver:
        return ""
    return f"\n{REVIEW_PATH_BLOCKED_REMINDER}"
