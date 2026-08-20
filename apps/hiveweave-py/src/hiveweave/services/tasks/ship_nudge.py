"""Wake CEO to ``message_user`` when a milestone is ready to ship.

TEST_DSH_05: VERIFY closed, parent already closed, CEO already
``disposition=complete``. ``[TASK CLOSED]`` went to QA/mid-level only, so
the CEO never did 终验. Structured prefix (not NL) + inbox wake.
"""
from __future__ import annotations

import structlog

from .db import _query
from .verify import is_verify_title

log = structlog.get_logger(__name__)

SHIP_READY_PREFIX = "[SHIP READY]"


def ship_anchor_id(task: dict | None) -> str:
    """Idempotency key fragment: children share the parent so close is quiet."""
    if not task:
        return ""
    return str(task.get("parent_task_id") or task.get("id") or "")


def ship_ready_message(task: dict) -> str:
    tid = str(task.get("id") or "")
    title = str(task.get("title") or "").strip()[:80]
    return (
        f"{SHIP_READY_PREFIX} Milestone QA closed for task {tid}"
        f"{f' ({title})' if title else ''}. "
        "CEO next: review the evidence pack (browse_main to look is optional "
        "and is not a test duty), then message_user with the ship conclusion "
        "for the human. Do not disposition=complete until message_user succeeds."
    )


async def _active_ceo_id(project_id: str) -> str | None:
    rows = await _query(
        project_id,
        "SELECT id FROM agents WHERE lower(role) = 'ceo' "
        "AND status = 'active' LIMIT 1",
        [],
    )
    if not rows:
        return None
    return str(rows[0]["id"] or "") or None


async def _has_remaining_open_tasks(project_id: str) -> bool:
    rows = await _query(
        project_id,
        "SELECT id FROM tasks WHERE is_archived = 0 "
        "AND status NOT IN ('closed', 'cancelled') LIMIT 1",
        [],
    )
    return bool(rows)


async def maybe_nudge_ceo_ship_ready(project_id: str, task: dict | None) -> None:
    """Best-effort: VERIFY close, or last open task closed (waived QA)."""
    if not task or not project_id:
        return
    verify = is_verify_title(task.get("title"))
    if not verify:
        try:
            if await _has_remaining_open_tasks(project_id):
                return
        except Exception as e:
            log.warning("ship_nudge_open_scan_failed", error=str(e))
    await nudge_ceo_ship_ready(project_id, task)


async def nudge_ceo_ship_ready(project_id: str, task: dict) -> None:
    """Inbox + trigger CEO. Deduped per project+anchor so parent close is quiet."""
    ceo_id = await _active_ceo_id(project_id)
    if not ceo_id:
        log.info("ship_nudge_no_ceo", project_id=project_id[:8])
        return
    anchor = ship_anchor_id(task)
    if not anchor:
        return
    task_id = str(task.get("id") or "") or None
    from hiveweave.agents.trigger import trigger_coordinator
    from hiveweave.services.inbox import InboxService

    result = await InboxService().send_message(
        from_agent_id="system",
        to_agent_id=ceo_id,
        message=ship_ready_message(task),
        message_type="task",
        priority="urgent",
        task_id=task_id,
        wake=True,
        idempotency_key=f"ship-ready:{project_id}:{anchor}",
    )
    if result.get("deduped") or not result.get("should_wake", True):
        log.info(
            "ship_nudge_deduped",
            project_id=project_id[:8],
            ceo_id=ceo_id[:8],
            anchor=anchor[:8],
        )
        return
    await trigger_coordinator(ceo_id)
    log.info(
        "ship_nudge_sent",
        project_id=project_id[:8],
        ceo_id=ceo_id[:8],
        task_id=(task_id or "")[:8],
        verify=is_verify_title(task.get("title")),
    )
