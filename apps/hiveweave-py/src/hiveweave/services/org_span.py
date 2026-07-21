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
    """Hard-block dispatch/create when assignee cannot write code.

    新契约（CEO 抽离 + 中层 builder）：assignee 须具备 SOURCE_WRITE
    （executor / qa / builder coordinator 均可）；family=ceo 一律拒绝
    （CEO 只做行政与里程碑验收，不承接改代码任务）。
    """
    from hiveweave.services.policy import (
        Capability,
        has_capability,
        infer_role_family,
    )

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
    name = agent.get("name") or to_agent_id[:8]
    family = infer_role_family(agent)
    if family == "ceo":
        return (
            f"拒绝派活：「{name}」是 CEO（行政/里程碑验收角色），不能承接改代码任务。"
            "请派给直属中层 coordinator 或 executor。"
        )
    if not has_capability(agent, Capability.SOURCE_WRITE):
        return (
            f"拒绝派活：「{name}」不具备源码写能力（role_family={family}），"
            "不能承接改代码任务。请改派有写码权的中层 coordinator / executor / QA。"
        )
    return None


async def validate_ceo_dispatch_target(
    from_agent_id: str, to_agent_id: str, org_service: Any = None
) -> str | None:
    """CEO 派工硬门：assignee 只能是直属中层 coordinator（family=coordinator）。

    修复「现只能派 executor」语义倒挂 —— CEO 不再日常直派叶子 executor/QA，
    也不向 HR 派开发任务；骨架/里程碑任务交给中层 builder 拆解。
    非 CEO 发起者不受此门约束（中层可自由派 executor/QA）。
    """
    from hiveweave.services.policy import infer_role_family

    if org_service is None:
        from hiveweave.services.org import OrgService

        org_service = OrgService()
    try:
        sender = await org_service.get_agent(from_agent_id)
        target = await org_service.get_agent(to_agent_id)
    except Exception as e:
        log.warning("ceo_dispatch_lookup_failed", error=str(e))
        return None
    if not sender or not target:
        return None
    if infer_role_family(sender) != "ceo":
        return None
    if from_agent_id == to_agent_id:
        return None
    if infer_role_family(target) != "coordinator":
        tname = target.get("name") or to_agent_id[:8]
        return (
            f"拒绝派活：CEO 只能把任务派给直属中层 coordinator（技术负责人/架构师等），"
            f"「{tname}」不是中层 coordinator。"
            "请把骨架/里程碑任务派给中层，由中层拆解后再派 executor/QA。"
        )
    return None
