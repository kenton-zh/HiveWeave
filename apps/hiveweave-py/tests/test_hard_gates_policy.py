"""P0 Hard Gates — Capability / PolicyService unit tests."""

from __future__ import annotations

import pytest

from hiveweave.services.org_invariants import validate_hire
from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import (
    Capability,
    capabilities_for,
    infer_role_family,
    policy_service,
    tool_hard_deny,
    write_path_allowed,
)


def _agent(**kwargs) -> dict:
    base = {
        "id": "a1",
        "name": "墨白",
        "role": "签到工程师",
        "permission_type": "executor",
        "permission_mode": "readwrite",
        "allowed_tools": "[]",
        "denied_tools": "[]",
        "ask_tools": "[]",
    }
    base.update(kwargs)
    return base


def test_infer_families():
    assert infer_role_family(_agent(role="hr", permission_type="coordinator")) == "hr"
    # role==ceo 优先于 permission_type=coordinator —— CEO 是独立行政 family
    assert infer_role_family(_agent(role="ceo", permission_type="coordinator")) == "ceo"
    assert infer_role_family(
        _agent(role="测试工程师", permission_type="executor")
    ) == "qa"
    assert infer_role_family(
        _agent(role="前端架构师", permission_type="coordinator")
    ) == "coordinator"
    assert infer_role_family(_agent(role="前端模块工程师")) == "executor"


def test_hr_caps_no_dispatch_or_bash():
    hr = _agent(role="hr", permission_type="coordinator")
    caps = capabilities_for(hr)
    assert Capability.STAFFING in caps
    assert Capability.DISPATCH not in caps
    assert Capability.BASH_SHELL not in caps
    assert tool_hard_deny(hr, "dispatch_task")
    assert tool_hard_deny(hr, "bash")
    assert tool_hard_deny(hr, "browse")
    assert tool_hard_deny(hr, "hire_agent") is None


def test_ceo_no_bash_browse_hire_or_source_write():
    """CEO 行政 family：派审合/org 放行，写码/bash/test/staffing 全拒。"""
    ceo = _agent(role="ceo", permission_type="coordinator", name="归零")
    assert infer_role_family(ceo) == "ceo"
    assert tool_hard_deny(ceo, "bash")
    assert tool_hard_deny(ceo, "browse")
    assert tool_hard_deny(ceo, "run_tests")
    assert tool_hard_deny(ceo, "hire_agent")
    assert tool_hard_deny(ceo, "edit_file")
    assert tool_hard_deny(ceo, "dispatch_task") is None
    assert tool_hard_deny(ceo, "review_task") is None
    assert tool_hard_deny(ceo, "git_worktree_merge") is None
    assert write_path_allowed(ceo, "src/app.ts")
    assert write_path_allowed(ceo, "docs/plan.md") is None


def test_builder_coordinator_has_code_caps():
    """中层 builder coordinator：协调权 + 写码权（SOURCE_WRITE/bash/test/browse）。"""
    mid = _agent(role="前端架构师", permission_type="coordinator", name="云岫")
    assert infer_role_family(mid) == "coordinator"
    assert tool_hard_deny(mid, "bash") is None
    assert tool_hard_deny(mid, "browse") is None
    assert tool_hard_deny(mid, "run_tests") is None
    assert tool_hard_deny(mid, "edit_file") is None
    assert tool_hard_deny(mid, "dispatch_task") is None
    assert tool_hard_deny(mid, "review_task") is None
    # staffing 仍是 HR 专属
    assert tool_hard_deny(mid, "hire_agent")
    # 源码写放开（不再限 docs 白名单）
    assert write_path_allowed(mid, "src/app.ts") is None
    assert write_path_allowed(mid, "docs/plan.md") is None


def test_executor_no_hire_or_dispatch():
    ex = _agent()
    assert tool_hard_deny(ex, "hire_agent")
    assert tool_hard_deny(ex, "dispatch_task")
    assert tool_hard_deny(ex, "bash") is None
    assert write_path_allowed(ex, "src/app.ts") is None


@pytest.mark.asyncio
async def test_allowed_tools_cannot_elevate_async(monkeypatch):
    svc = PermissionService()
    agent = _agent(
        role="ceo",
        permission_type="coordinator",
        allowed_tools='["bash", "edit_file"]',
    )

    async def fake_get(_aid):
        return agent

    monkeypatch.setattr(
        "hiveweave.services.permission.meta_db.get_agent_by_id", fake_get
    )
    assert await svc.evaluate("a1", "bash", {}) == "deny"
    assert await svc.evaluate("a1", "dispatch_task", {}) == "allow"


def test_policy_hard_check_write_scope():
    ceo = _agent(role="ceo", permission_type="coordinator")
    assert policy_service.hard_check(
        ceo, "write_file", {"filePath": "apps/web/src/App.tsx"}
    )
    assert (
        policy_service.hard_check(
            ceo, "write_file", {"filePath": "docs/adr/001.md"}
        )
        is None
    )


def test_hire_rejects_hr_as_parent():
    agents = [
        {
            "id": "ceo-1",
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "parent_id": None,
            "status": "active",
        },
        {
            "id": "hr-1",
            "name": "天线",
            "role": "hr",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "active",
        },
    ]
    err = validate_hire(
        agents=agents,
        name="青禾",
        role="签到模块工程师",
        permission_type="executor",
        parent_id="hr-1",
    )
    assert err is not None
    assert "HR cannot have subordinates" in err


def test_hire_rejects_name_equals_role():
    agents = [
        {
            "id": "ceo-1",
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "status": "active",
        },
        {
            "id": "arch-1",
            "name": "知远",
            "role": "architect",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "active",
        },
    ]
    err = validate_hire(
        agents=agents,
        name="前端工程师",
        role="前端工程师",
        permission_type="executor",
        parent_id="arch-1",
    )
    assert err is not None


def test_hire_rejects_duplicate_coordinator_under_parent():
    agents = [
        {
            "id": "ceo-1",
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "status": "active",
        },
        {
            "id": "arch-1",
            "name": "知远",
            "role": "frontend-architect",
            "permission_type": "coordinator",
            "parent_id": "ceo-1",
            "status": "active",
        },
    ]
    err = validate_hire(
        agents=agents,
        name="潮汐",
        role="frontend-architect",
        permission_type="coordinator",
        parent_id="ceo-1",
    )
    assert err is not None
    assert "already has coordinator" in err


def test_bootstrap_allows_reserved_ceo_hr():
    err = validate_hire(
        agents=[],
        name="归零",
        role="ceo",
        permission_type="coordinator",
        parent_id="",
        bootstrap=True,
    )
    assert err is None
