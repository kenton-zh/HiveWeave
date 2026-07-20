"""Org span hard gates — direct-report command chain.

Prevents CEO/coordinators from routinely dispatching or messaging across
levels (e.g. CEO → leaf executor under a Tech Lead). Escalation / system
watchdog paths do not go through these helpers.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def validate_dispatch_span(
    from_agent_id: str, to_agent_id: str, org_service: Any = None
) -> str | None:
    """Dispatch only to direct reports. Returns error text or None if OK."""
    return await _validate_span(
        from_agent_id,
        to_agent_id,
        org_service=org_service,
        allow_peer=False,
        allow_superior=False,
        action="派活",
    )


async def validate_message_span(
    from_agent_id: str, to_agent_id: str, org_service: Any = None
) -> str | None:
    """Messaging: direct reports, superior, or true peers (same parent)."""
    return await _validate_span(
        from_agent_id,
        to_agent_id,
        org_service=org_service,
        allow_peer=True,
        allow_superior=True,
        action="沟通",
    )


async def _validate_span(
    from_agent_id: str,
    to_agent_id: str,
    *,
    org_service: Any = None,
    allow_peer: bool,
    allow_superior: bool,
    action: str,
) -> str | None:
    if not from_agent_id or not to_agent_id:
        return f"拒绝{action}：缺少 from/to agent id"
    if from_agent_id == to_agent_id:
        return None

    if org_service is None:
        from hiveweave.services.org import OrgService

        org_service = OrgService()

    try:
        sender = await org_service.get_agent(from_agent_id)
        target = await org_service.get_agent(to_agent_id)
    except Exception as e:
        log.warning("org_span_lookup_failed", error=str(e))
        return None  # fail-open on infra errors — do not brick messaging

    if not target or target.get("status") == "archived":
        return f"拒绝{action}：目标不存在或已归档"

    # Direct report
    if target.get("parent_id") == from_agent_id:
        return None

    # Superior (1 hop)
    if allow_superior and sender and sender.get("parent_id") == to_agent_id:
        return None

    # Peer (same non-null parent)
    if (
        allow_peer
        and sender
        and sender.get("parent_id")
        and sender.get("parent_id") == target.get("parent_id")
    ):
        return None

    tname = target.get("name") or to_agent_id[:8]
    allowed = ["直属下属"]
    if allow_superior:
        allowed.append("上级")
    if allow_peer:
        allowed.append("同级")
    return (
        f"拒绝跨级{action}：只能联系{'/'.join(allowed)}。"
        f"「{tname}」不在你的指挥链直达范围内。"
        "跨级请让直属上级转发，或走升级/transfer，不要直接打穿组织。"
    )


async def validate_executor_assignee(
    to_agent_id: str, org_service: Any = None
) -> str | None:
    """Hard-block dispatch/create when assignee is a coordinator (no source write)."""
    if org_service is None:
        from hiveweave.services.org import OrgService

        org_service = OrgService()
    try:
        agent = await org_service.get_agent(to_agent_id)
    except Exception as e:
        log.warning("executor_assignee_lookup_failed", error=str(e))
        return None
    if not agent:
        return None
    perm = (agent.get("permission_type") or "").strip().lower()
    if perm == "coordinator":
        name = agent.get("name") or to_agent_id[:8]
        return (
            f"拒绝派活：「{name}」是 coordinator（只读协调角色），不能承接改代码任务。"
            "请改派 executor（工程师/QA 等可写角色），或让对方再 dispatch 给下属。"
        )
    return None
