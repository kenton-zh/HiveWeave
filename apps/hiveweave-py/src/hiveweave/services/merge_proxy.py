"""MERGE PROXY — escalate overdue/unavailable merger to a parent with MERGE.

Platform never auto-runs git_worktree_merge; it wakes a coordinator ancestor.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def find_merge_capable_parent(
    project_id: str,
    agent_id: str,
    *,
    agents_by_id: dict[str, dict] | None = None,
) -> dict | None:
    """Walk parent_id chain; return first coordinator-family ancestor (has MERGE)."""
    from hiveweave.services.org import OrgService
    from hiveweave.services.policy import Capability, has_capability

    if agents_by_id is None:
        agents = await OrgService().list_agents(project_id)
        agents_by_id = {a.get("id"): a for a in agents if a.get("id")}

    cur = agents_by_id.get(agent_id) or {}
    parent_id = cur.get("parent_id")
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = agents_by_id.get(parent_id)
        if not parent:
            break
        if has_capability(parent, Capability.MERGE):
            return parent
        parent_id = parent.get("parent_id")
    return None


async def escalate_merge_proxy(
    project_id: str,
    task: dict,
    *,
    reason: str = "overdue",
    agents_by_id: dict[str, dict] | None = None,
    trigger: bool = True,
) -> str | None:
    """Send [MERGE PROXY] to merge-capable parent. Returns parent id or None."""
    from hiveweave.services.inbox import InboxService
    from hiveweave.services.org import OrgService

    creator_id = task.get("creator_id")
    if not creator_id:
        return None

    if agents_by_id is None:
        agents = await OrgService().list_agents(project_id)
        agents_by_id = {a.get("id"): a for a in agents if a.get("id")}

    parent = await find_merge_capable_parent(
        project_id, str(creator_id), agents_by_id=agents_by_id
    )
    if not parent or not parent.get("id"):
        log.info(
            "merge_proxy_no_parent",
            project_id=project_id,
            creator_id=creator_id,
            task_id=task.get("id"),
        )
        return None

    parent_id = str(parent["id"])
    creator = agents_by_id.get(str(creator_id)) or {}
    assignee_id = task.get("assignee_id")
    assignee = agents_by_id.get(str(assignee_id or "")) or {}
    short = (
        (assignee.get("short_id") or "")
        or (creator.get("short_id") or "")
        or "hw/<short_id>/..."
    )
    tid = str(task.get("id") or "")
    title = (task.get("title") or "(untitled)").split("\n")[0][:50]
    cname = creator.get("name") or str(creator_id)[:8]
    cshort = creator.get("short_id") or ""

    body = (
        f"[MERGE PROXY] Task '{title}' ({tid[:8]}) is approved; "
        f"original merger {cname}"
        + (f"/{cshort}" if cshort else "")
        + f" unavailable or overdue (reason={reason}). "
        f"Call git_worktree_merge(branchName='{short}') on their behalf. "
        f"Do not leave worktree-only."
    )
    try:
        await InboxService().send_message(
            from_agent_id="system",
            to_agent_id=parent_id,
            message=body,
            message_type="escalation",
            priority="urgent",
            task_id=tid or None,
            wake=True,
        )
    except Exception as e:
        log.warning(
            "merge_proxy_send_failed",
            parent_id=parent_id,
            task_id=tid,
            error=str(e),
        )
        return None

    if trigger:
        try:
            from hiveweave.agents.trigger import trigger_coordinator

            await trigger_coordinator(parent_id)
        except Exception as e:
            log.debug("merge_proxy_trigger_failed", parent_id=parent_id, error=str(e))

    log.info(
        "merge_proxy_escalated",
        project_id=project_id,
        task_id=tid,
        parent_id=parent_id,
        creator_id=creator_id,
        reason=reason,
    )
    return parent_id
