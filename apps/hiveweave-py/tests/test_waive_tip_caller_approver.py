"""BUG-WAIVE-TIP 回归：幂等拒绝提示须识别「调用者本人即合法 approver」。

2026-08-05 DevBlog 死锁根因：waiver 已由 CEO 签发后，协调者（云岫）误判
「我也得签 waiver / 我得自己跑出 test_run」，重复调 waive_attestation 被
幂等拒绝。旧提示列出合法 approver 名单让他去 ask——而名单里的人就是他
自己 → 团队在三方互相等待中死锁。修复后：caller 在名单内时直接告知
「你就是合法第三方 approver，立即 review_task(approve)」。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hiveweave.tools.tasks.waive import _format_post_waive_approve_tip

CEO_ID = "ceo-归零"
COORD_ID = "coord-云岫"
ASSIGNEE_ID = "exec-星河"

_AGENTS = [
    {"id": CEO_ID, "name": "归零", "role": "ceo",
     "permission_type": "coordinator", "status": "active"},
    {"id": COORD_ID, "name": "云岫", "role": "前端架构师",
     "permission_type": "coordinator", "status": "active"},
    {"id": ASSIGNEE_ID, "name": "星河", "role": "后端架构师",
     "permission_type": "coordinator", "status": "active"},
]


def _mock_org():
    async def list_agents(self, project_id):
        return list(_AGENTS)

    async def get_agent(self, aid):
        for a in _AGENTS:
            if a["id"] == aid:
                return dict(a)
        return None

    return patch.multiple(
        "hiveweave.services.org.OrgService",
        list_agents=list_agents,
        get_agent=get_agent,
    )


@pytest.mark.asyncio
async def test_caller_in_holders_gets_direct_approve_tip():
    """caller（非 waived_by、非 assignee）在名单内 → 直接告知可 approve。"""
    with _mock_org():
        tip = await _format_post_waive_approve_tip(
            "p1",
            waived_by=CEO_ID,
            assignee_id=ASSIGNEE_ID,
            caller_id=COORD_ID,
        )
    assert "YOU are a lawful third-party approver" in tip
    assert 'review_task(decision="approve"' in tip
    # 不得再出现「去 ask 名单里的人」的列表式引导
    assert "Ask ONE of these REVIEW holders" not in tip


@pytest.mark.asyncio
async def test_caller_not_in_holders_gets_name_list():
    """caller 不在名单内（如 executor 误调）→ 保持原名单引导行为。"""
    with _mock_org():
        tip = await _format_post_waive_approve_tip(
            "p1",
            waived_by=CEO_ID,
            assignee_id=ASSIGNEE_ID,
            caller_id="some-random-executor",
        )
    assert "YOU are a lawful third-party approver" not in tip
    assert "Ask ONE of these REVIEW holders" in tip
    assert "云岫" in tip  # 名单里列出协调者（除 waived_by 与 assignee）


@pytest.mark.asyncio
async def test_no_caller_id_keeps_legacy_list_behavior():
    """不传 caller_id（waive 成功回执路径）→ 行为与修复前一致。"""
    with _mock_org():
        tip = await _format_post_waive_approve_tip(
            "p1",
            waived_by=CEO_ID,
            assignee_id=ASSIGNEE_ID,
        )
    assert "YOU are a lawful third-party approver" not in tip
    assert "Ask ONE of these REVIEW holders" in tip
