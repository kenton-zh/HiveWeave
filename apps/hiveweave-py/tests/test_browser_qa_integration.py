"""Browser QA integration: browse tool + builtin skills + role routing."""

from __future__ import annotations

import asyncio

import hiveweave.tools.browse_tools  # noqa: F401 — register @tool

from hiveweave.config import resolve_browse_bin
from hiveweave.prompts.coordinator import build_coordinator_script
from hiveweave.prompts.executor import build_executor_script, _is_test_engineer_role
from hiveweave.services.permission import READONLY_TOOLS
from hiveweave.services.skill_registry import BUILTIN_SKILLS, SkillRegistryService
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS


def test_browse_and_qa_are_builtin_skills():
    slugs = {s["slug"] for s in BUILTIN_SKILLS}
    assert "browse" in slugs
    assert "qa" in slugs
    browse = next(s for s in BUILTIN_SKILLS if s["slug"] == "browse")
    assert "browse" in browse["instructions"].lower()
    assert browse["category"] == "tool"


def test_browse_tool_registered_and_permitted():
    assert "browse" in READONLY_TOOLS
    assert "browse" in TOOL_PARAM_SCHEMAS
    assert get_tool_def("browse") is not None


def test_resolve_browse_bin_finds_gstack_install():
    path = resolve_browse_bin()
    # Soft-skip if gstack not installed on this machine / CI
    if path is None:
        return
    assert path.is_file()
    assert path.name in {"browse", "browse.exe"}


def test_test_engineer_role_routing_chinese():
    assert _is_test_engineer_role("测试工程师")
    assert _is_test_engineer_role("前端测试工程师")
    assert _is_test_engineer_role("Test Engineer")
    assert _is_test_engineer_role("qa")
    script = build_executor_script("测试工程师", "鹿鸣")
    assert "browse" in script
    assert "真实浏览器" in script or "Chromium" in script


def test_inspector_not_confused_with_test_engineer():
    script = build_executor_script("审查员", "审慎")
    assert "browse(args" not in script
    assert "真实 Chromium" not in script


def test_ceo_and_hr_know_browser_qa():
    ceo = build_coordinator_script("ceo", "归零")
    assert "browse" in ceo
    assert "测试工程师" in ceo
    assert "IRON RULE" in ceo or "唯一标准验收" in ceo
    hr = build_coordinator_script("hr", "天线")
    assert "browse" in hr and "qa" in hr
    assert "测试工程师" in hr
    assert "从上到下匹配" in hr


def test_identity_requires_browse_for_ui_e2e():
    from hiveweave.prompts.identity import build_identity_prompt

    text = build_identity_prompt(
        role="前端工程师",
        role_type="executor",
        backstory="x",
        name="潮汐",
        goal="做游戏",
        model_id="gpt-4o",
    )
    assert "browse" in text
    assert "E2E" in text or "端到端" in text


def test_skill_registry_can_read_browse():
    async def _run():
        svc = SkillRegistryService()
        text = await svc.read_skill("browse")
        assert "Chromium" in text or "browse" in text.lower()
        detail = await svc.get_skill_detail("browse")
        assert "Built-in" in detail

    asyncio.run(_run())
