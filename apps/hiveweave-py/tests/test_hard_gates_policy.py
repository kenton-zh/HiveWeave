"""P0 Hard Gates — Capability / PolicyService unit tests."""

from __future__ import annotations

import pytest

from hiveweave.services.org_invariants import validate_hire
from hiveweave.services.permission import PermissionService
from hiveweave.services.policy import (
    Capability,
    capabilities_for,
    classify_write_kind,
    has_visual_test_duty,
    infer_role_family,
    policy_service,
    tool_hard_deny,
    write_path_allowed,
)
from hiveweave.tools.org_tools import _hire_permission_mode


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


def test_infer_families_e2e_coordinator_not_qa():
    """role 名带 e2e 的协调员（permission_type=coordinator）→ coordinator，非 qa。

    E2E 实测误判：`is_test_engineer_role` 裸词 `e2e` 把协调员带偏成 qa，
    导致 CEO 无法派发给他们 —— 结构化角色 ID（permission_type=coordinator）
    须优先于 role 名扫描。
    """
    assert infer_role_family(
        _agent(role="S3 e2e 数据面负责人", permission_type="coordinator")
    ) == "coordinator"
    # 浏览器测试 / 测试工程师 语义明确，仍按 role 名兜底识别为 qa（Echo 事故）
    assert infer_role_family(
        _agent(role="浏览器测试工程师", permission_type="executor")
    ) == "qa"
    assert infer_role_family(
        _agent(role="前端测试工程师", permission_type="executor")
    ) == "qa"
    # permission_type 作为角色 ID 可直接表达 qa（无需 role 名）
    assert infer_role_family(
        _agent(role="验收专员", permission_type="qa")
    ) == "qa"


def test_hire_permission_mode_explicit_ceo_hr_readonly():
    """显式 permType=ceo/hr 与 coordinator+role 归族产出同一个 readonly（C1）。

    修复前：显式 permType 直接返回 readwrite，与"CEO 偏只读协调"意图相悖。
    """
    assert _hire_permission_mode("ceo", "CEO") == "readonly"
    assert _hire_permission_mode("hr", "HR经理") == "readonly"
    # builder coordinator 与叶子仍可写（SOURCE_WRITE 依赖 readwrite mode）
    assert _hire_permission_mode("coordinator", "架构师") == "readwrite"
    assert _hire_permission_mode("executor", "后端开发") == "readwrite"
    assert _hire_permission_mode("qa", "验收专员") == "readwrite"


def test_hr_caps_no_dispatch_or_bash():
    hr = _agent(role="hr", permission_type="coordinator")
    caps = capabilities_for(hr)
    assert Capability.STAFFING in caps
    assert Capability.DISPATCH not in caps
    assert Capability.BASH_SHELL not in caps
    assert tool_hard_deny(hr, "dispatch_task")
    assert tool_hard_deny(hr, "bash")
    assert tool_hard_deny(hr, "browse")
    assert tool_hard_deny(hr, "browse_main")
    assert tool_hard_deny(hr, "hire_agent") is None


def test_ceo_has_browse_not_test_duty():
    """CEO 可 browse 看产品；无 bash / TEST_RUN / 写码。自己的出证不算审批。"""
    ceo = _agent(role="ceo", permission_type="coordinator", name="归零")
    assert infer_role_family(ceo) == "ceo"
    caps = capabilities_for(ceo)
    assert Capability.DOC_WRITE in caps
    assert Capability.BROWSE in caps
    assert Capability.SOURCE_WRITE not in caps
    assert Capability.BASH_SHELL not in caps
    assert Capability.TEST_RUN not in caps
    assert Capability.BROWSER_ACCEPTANCE not in caps
    assert tool_hard_deny(ceo, "bash")
    assert tool_hard_deny(ceo, "browse") is None
    assert tool_hard_deny(ceo, "browse_main") is None
    assert tool_hard_deny(ceo, "assert_visual")
    assert tool_hard_deny(ceo, "game_run_case")
    assert tool_hard_deny(ceo, "game_run_case_main")
    assert tool_hard_deny(ceo, "run_tests")
    assert tool_hard_deny(ceo, "hire_agent")
    assert tool_hard_deny(ceo, "apply_patch")
    # edit_file 能力放行（DOC_WRITE），路径硬门另测
    assert tool_hard_deny(ceo, "edit_file") is None
    assert tool_hard_deny(ceo, "dispatch_task") is None
    assert tool_hard_deny(ceo, "review_task") is None
    assert tool_hard_deny(ceo, "git_worktree_merge") is None
    assert write_path_allowed(ceo, "src/app.ts")
    assert write_path_allowed(ceo, "docs/plan.md") is None
    # 任意文档路径（不限 docs/ 前缀）
    assert write_path_allowed(ceo, "CHANGELOG.md") is None
    assert write_path_allowed(ceo, "notes/ship-report.md") is None
    assert write_path_allowed(ceo, "src/components/README.md") is None
    # 运行时配置 / 无扩展非文档 → other → 拒
    assert write_path_allowed(ceo, "package.json")
    assert write_path_allowed(ceo, "docs/hack.py")


def test_classify_write_kind():
    assert classify_write_kind("README.md") == "document"
    assert classify_write_kind("docs/a.rst") == "document"
    assert classify_write_kind("LICENSE") == "document"
    assert classify_write_kind("src/App.tsx") == "source"
    assert classify_write_kind("styles.css") == "source"
    assert classify_write_kind("package.json") == "other"
    assert classify_write_kind("Makefile") == "other"


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
        allowed_tools='["bash", "edit_file", "assert_visual", "game_run_case"]',
    )

    async def fake_get(_aid):
        return agent

    monkeypatch.setattr(
        "hiveweave.services.permission.meta_db.get_agent_by_id", fake_get
    )
    assert await svc.evaluate("a1", "bash", {}) == "deny"
    assert await svc.evaluate("a1", "assert_visual", {}) == "deny"
    assert await svc.evaluate("a1", "game_run_case", {}) == "deny"
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
    assert (
        policy_service.hard_check(
            ceo, "edit_file", {"filePath": "RELEASE_NOTES.md"}
        )
        is None
    )
    assert policy_service.hard_check(
        ceo, "edit_file", {"filePath": "src/main.py"}
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


def test_ceo_has_browse_without_visual_test_duty():
    ceo = _agent(role="ceo", permission_type="coordinator", name="归零")
    qa = _agent(role="测试工程师", permission_type="executor")
    assert has_visual_test_duty(ceo) is False
    assert has_visual_test_duty(qa) is True


def test_look_only_screenshot_followup_does_not_nudge_assert():
    from hiveweave.tools.browse_tools import screenshot_followup_text

    look = screenshot_followup_text("/tmp/x.png", look_only=True)
    assert "Then call" not in look
    assert "assert_visual(" not in look
    assert "look_at_image" not in look
    stamp = screenshot_followup_text("/tmp/x.png", look_only=False)
    assert "assert_visual(" not in stamp
    assert "pixels attached" in stamp.lower() or "像素" in stamp


@pytest.mark.asyncio
async def test_ceo_own_browse_e2e_does_not_unlock_approve(tmp_path):
    """reviewer_must_hold=False skips CEO's own browse_e2e; consume QA works."""
    from unittest.mock import AsyncMock, patch

    from hiveweave.services.attestation import (
        BROWSE_E2E_KIND,
        attestation_service,
        find_reviewer_attestation,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    project_id = "p-ceo-browse"
    kinds = frozenset({BROWSE_E2E_KIND, "visual_check", "test_run"})
    with patch(
        "hiveweave.db.meta.get_project_workspace",
        AsyncMock(return_value=str(ws)),
    ):
        await attestation_service.ensure_schema(project_id)
        own_id = await attestation_service.create(
            project_id,
            agent_id="ceo1",
            kind=BROWSE_E2E_KIND,
            command_or_url="browse goto",
            exit_code=0,
            workspace=str(ws),
            stdout="ok",
            task_id="task-ui",
        )
        assert own_id
        own = await find_reviewer_attestation(
            project_id,
            "task-ui",
            "ceo1",
            kinds,
            consume_agent_ids=None,
            reviewer_must_hold=False,
        )
        assert own is False
        qa_id = await attestation_service.create(
            project_id,
            agent_id="qa1",
            kind=BROWSE_E2E_KIND,
            command_or_url="browse_main goto",
            exit_code=0,
            workspace=str(ws),
            stdout="ok",
            task_id="task-ui",
        )
        assert qa_id
        consumed = await find_reviewer_attestation(
            project_id,
            "task-ui",
            "ceo1",
            kinds,
            consume_agent_ids=["qa1"],
            reviewer_must_hold=False,
        )
        assert consumed is True
