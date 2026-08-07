"""Task ledger tools package — @tool registration via side-effect imports."""
from __future__ import annotations

# Import modules so @tool decorators run
from . import (  # noqa: F401
    admin,
    attestation_tools,
    create,
    dispatch,
    lifecycle,
    query,
    review,
    submit,
    verify,
    waive,
)

# Re-export helpers used outside this package
from .verify import (  # noqa: F401
    nudge_pending_verify_tasks,
    nudge_stale_verify_tasks,
    nudge_verify_tasks_after_merge,
    parse_short_id_from_branch,
    resolve_agent_id_by_short_id,
    retry_qa_blocked_verify_tasks,
    rework_tasks_after_merge_conflict,
    spawn_verify_for_approved_assignee,
)

__all__ = [
    "nudge_pending_verify_tasks",
    "nudge_stale_verify_tasks",
    "nudge_verify_tasks_after_merge",
    "parse_short_id_from_branch",
    "resolve_agent_id_by_short_id",
    "retry_qa_blocked_verify_tasks",
    "rework_tasks_after_merge_conflict",
    "spawn_verify_for_approved_assignee",
]
