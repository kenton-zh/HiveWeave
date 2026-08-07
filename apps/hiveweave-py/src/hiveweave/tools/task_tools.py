"""Compatibility shim for task_tools — implementation lives in tools.tasks.

Importing this module still registers all task @tools (via tasks package).
External callers may keep ``from hiveweave.tools.task_tools import …``.
"""
from __future__ import annotations

# Side-effect: register @tool handlers
import hiveweave.tools.tasks  # noqa: F401

from hiveweave.tools.tasks.verify import (  # noqa: F401
    nudge_pending_verify_tasks,
    nudge_stale_verify_tasks,
    nudge_verify_tasks_after_merge,
    parse_short_id_from_branch,
    resolve_agent_id_by_short_id,
    retry_qa_blocked_verify_tasks,
    rework_tasks_after_merge_conflict,
    spawn_verify_for_approved_assignee,
)
from hiveweave.tools.tasks.verify_spawn import (  # noqa: F401
    VERIFY_STALE_MS,
    _find_independent_qa,
    _nudge_one_verify_task,
    _spawn_post_approve_verify_task,
    _stale_verify_cooldowns,
    _verify_required_capabilities,
)

# Param models / tool callables re-exported for tests that import them
from hiveweave.tools.tasks.admin import (  # noqa: F401
    CancelTaskParams,
    ReassignTaskParams,
    UnclaimTaskParams,
    cancel_task_tool,
    reassign_task_tool,
    unclaim_task_tool,
)
from hiveweave.tools.tasks.attestation_tools import (  # noqa: F401
    AttestDocReviewParams,
    attest_doc_review_tool,
)
from hiveweave.tools.tasks.create import CreateTaskParams, create_task_tool  # noqa: F401
from hiveweave.tools.tasks.dispatch import (  # noqa: F401
    DispatchTaskParams,
    dispatch_task_tool,
)
from hiveweave.tools.tasks.lifecycle import (  # noqa: F401
    ClaimTaskParams,
    UpdateProgressParams,
    UpdateTaskStatusParams,
    claim_task_tool,
    update_progress_tool,
    update_task_status_tool,
)
from hiveweave.tools.tasks.query import GetTasksParams, get_tasks_tool  # noqa: F401
from hiveweave.tools.tasks.review import ReviewTaskParams, review_task_tool  # noqa: F401
from hiveweave.tools.tasks.submit import SubmitTaskParams, submit_task_tool  # noqa: F401
from hiveweave.tools.tasks.waive import (  # noqa: F401
    WaiveAttestationParams,
    WaiveMergeParams,
    waive_attestation_tool,
    waive_merge_tool,
)

__all__ = [
    "CancelTaskParams",
    "cancel_task_tool",
    "WaiveAttestationParams",
    "waive_attestation_tool",
    "ReviewTaskParams",
    "review_task_tool",
    "retry_qa_blocked_verify_tasks",
    "nudge_pending_verify_tasks",
    "nudge_stale_verify_tasks",
    "nudge_verify_tasks_after_merge",
    "parse_short_id_from_branch",
    "resolve_agent_id_by_short_id",
    "rework_tasks_after_merge_conflict",
    "spawn_verify_for_approved_assignee",
    "VERIFY_STALE_MS",
    "_find_independent_qa",
    "_nudge_one_verify_task",
    "_spawn_post_approve_verify_task",
    "_verify_required_capabilities",
]
