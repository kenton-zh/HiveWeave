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
        # Fail-closed with reason so the model knows WHY (not silent allow).
        return (
            f"拒绝{action}：组织查询失败，无法校验指挥链（{e}）。"
            "请稍后重试；不要假设跨级已放行。"
        )

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
    # B5（2026-09-28）：失败回执 = 事实 + 下一步处方。跨级派活最常见的断点
    # 是目标下属的直属中层尚未接活/开工；沟通路径无派活语义，不带此句。
    dispatch_hint = (
        "若目标下属的直属中层尚未接活/开工，先向你的直属下属"
        "（该叶子的直属中层）本人派活，再由其转派。"
        if action == "派活"
        else ""
    )
    # 批25 修复2c：跨级沟通拒补「正确通道一句话」—— 改道路径已有
    #（上级转发/升级/transfer），再点名可直接使用的通道（直属下属/同级/
    # 直属上级可直接消息/ask，范围外对象经直属上级转达），模型拿到拒收
    # 后有确定下一步。派活向已有 B5 处方，不重复加。
    channel_hint = (
        "正确通道：直属下属、同级、直属上级可直接消息/ask；"
        "范围外对象把要说的话发给你直属上级，请其转达或协调。"
        if action != "派活"
        else ""
    )
    return (
        f"拒绝跨级{action}：只能联系{'/'.join(allowed)}。"
        f"「{tname}」不在你的指挥链直达范围内。"
        "跨级请让直属上级转发，或走升级/transfer，不要直接打穿组织。"
        f"{dispatch_hint}{channel_hint}"
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
        return (
            f"拒绝派活：组织查询失败，无法校验 assignee 写码能力（{e}）。"
            "请稍后重试。"
        )
    if not agent:
        return (
            f"拒绝派活：assignee `{to_agent_id[:12]}` 不存在或无法加载，"
            "请用 list_subordinates / view_org_chart 确认目标后再派。"
        )
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
        return (
            f"拒绝派活：组织查询失败，无法校验 CEO 派工目标（{e}）。"
            "请稍后重试。"
        )
    if not sender:
        return None  # cannot classify sender → skip CEO-only gate
    if infer_role_family(sender) != "ceo":
        return None
    if from_agent_id == to_agent_id:
        return None
    if not target:
        return (
            f"拒绝派活：目标 `{to_agent_id[:12]}` 不存在或无法加载，"
            "请用 view_org_chart 确认直属中层后再派。"
        )
    if infer_role_family(target) != "coordinator":
        tname = target.get("name") or to_agent_id[:8]
        return (
            f"拒绝派活：CEO 只能把任务派给直属中层 coordinator（技术负责人/架构师等），"
            f"「{tname}」不是中层 coordinator。"
            "请把骨架/里程碑任务派给中层，由中层拆解后再派 executor/QA。"
        )
    return None
