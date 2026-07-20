"""Project on/off-duty lifecycle — park inbox, stop agents cleanly, resume briefings.

Deactivate must not leave watchers polling or a wake stampede on next activate.
"""

from __future__ import annotations

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.services.inbox import InboxService

log = structlog.get_logger(__name__)

OFF_DUTY_CANCEL_REASON = "off_duty"
OFF_DUTY_STREAM_CONTENT = "[项目已下班，本轮进度已保存]"


async def _project_agent_ids(project_id: str) -> list[str]:
    """Union of router IDs + in-memory manager IDs + DB active agents."""
    ids: set[str] = set()
    try:
        from hiveweave.services.agent_router import agent_router

        ids.update(agent_router.get_project_agent_ids(project_id) or [])
    except Exception as e:
        log.warning("lifecycle_router_ids_failed", project_id=project_id, error=str(e))
    try:
        from hiveweave.agents.supervisor import agent_manager

        for aid, agent in list(agent_manager._agents.items()):
            if getattr(agent, "project_id", None) == project_id:
                ids.add(aid)
    except Exception as e:
        log.warning("lifecycle_manager_ids_failed", project_id=project_id, error=str(e))
    # Always union DB active agents — router/manager can be partial
    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(project_id)
        cursor = await conn.execute(
            "SELECT id FROM agents WHERE status = 'active'"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        ids.update(r[0] if not hasattr(r, "keys") else r["id"] for r in rows)
    except Exception as e:
        log.warning("lifecycle_db_ids_failed", project_id=project_id, error=str(e))
    return sorted(ids)


async def park_project_inbox(project_id: str, agent_ids: list[str] | None = None) -> int:
    """Park wake=1 unread inbox so deactivate does not leave a wake stampede.

    Returns number of messages parked.
    """
    ids = agent_ids if agent_ids is not None else await _project_agent_ids(project_id)
    inbox = InboxService()
    total = 0
    for aid in ids:
        try:
            total += await inbox.park_pending_wakes(aid)
        except Exception as e:
            log.warning("park_inbox_agent_failed", agent_id=aid, error=str(e))
    log.info("park_project_inbox_done", project_id=project_id, parked=total, agents=len(ids))
    return total


async def stop_project_cleanly(project_id: str) -> dict:
    """Stop every in-memory agent for the project (manager ∪ router), off-duty cancel."""
    from hiveweave.agents.supervisor import agent_manager

    ids = await _project_agent_ids(project_id)
    stopped = 0
    errors = 0
    for aid in ids:
        try:
            agent = agent_manager.get_agent(aid)
            if agent is not None:
                await agent.cancel(reason=OFF_DUTY_CANCEL_REASON)
                # Ensure removed from registry even if cancel left it
                agent_manager._agents.pop(aid, None)
                stopped += 1
            else:
                # Still try stop_agent for symmetry / logging
                await agent_manager.stop_agent(aid)
        except Exception as e:
            errors += 1
            log.warning(
                "stop_project_agent_failed",
                project_id=project_id,
                agent_id=aid,
                error=str(e),
            )
            agent_manager._agents.pop(aid, None)

    # Final sweep — anything still keyed to this project_id
    leftover = [
        aid
        for aid, ag in list(agent_manager._agents.items())
        if getattr(ag, "project_id", None) == project_id
    ]
    for aid in leftover:
        try:
            await agent_manager.stop_agent(aid)
            stopped += 1
        except Exception:
            agent_manager._agents.pop(aid, None)
            errors += 1

    result = {
        "stopped": stopped,
        "errors": errors,
        "agent_ids": ids,
        "leftover_cleared": len(leftover),
    }
    log.info("stop_project_cleanly_done", project_id=project_id, **result)
    return result


async def deliver_resume_briefings(project_id: str) -> dict:
    """On activate: park any leftover wake=1, then one coalesced briefing per agent.

    Parking here covers projects deactivated before the park feature existed
    (or deactivate that failed mid-way) so activate never stampede-wakes.
    """
    ids = await _project_agent_ids(project_id)
    # Safety net: coalesce leftover wake=1 unread even if deactivate skipped park
    pre_parked = await park_project_inbox(project_id, ids)
    inbox = InboxService()
    briefed = 0
    cleared = 0
    for aid in ids:
        try:
            n_cleared, sent = await inbox.deliver_parked_briefing(aid)
            cleared += n_cleared
            if sent:
                briefed += 1
        except Exception as e:
            log.warning(
                "resume_briefing_failed",
                project_id=project_id,
                agent_id=aid,
                error=str(e),
            )
    result = {
        "briefed": briefed,
        "parked_cleared": cleared,
        "pre_parked": pre_parked,
        "agents": len(ids),
    }
    log.info("deliver_resume_briefings_done", project_id=project_id, **result)
    return result


async def project_is_started(project_id: str | None) -> bool:
    if not project_id:
        return False
    row = await meta_db.query_one(
        "SELECT is_started FROM projects WHERE id = ?", [project_id]
    )
    if not row:
        return False
    return bool(dict(row).get("is_started"))


async def project_known_off_duty(project_id: str | None) -> bool:
    """True only when we positively know is_started=0.

    Missing project / DB errors → False (fail-open) so inbox watchers in
    tests or transient routing gaps keep polling instead of going silent.
    """
    if not project_id:
        return False
    try:
        row = await meta_db.query_one(
            "SELECT is_started FROM projects WHERE id = ?", [project_id]
        )
    except Exception:
        return False
    if not row:
        return False
    return not bool(dict(row).get("is_started"))
