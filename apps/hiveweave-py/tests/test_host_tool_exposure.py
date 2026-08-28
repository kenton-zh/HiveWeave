"""T3.2：Windows pwsh 宿主不暴露 bash/bash_main + pwsh_main 补第四格。

验收（计划 §6 检查点 4 前半 + pwsh_main 补格）：
- 工具矩阵 2×2 四格齐：bash/bash_main（Linux）/ pwsh/pwsh_main（Windows pwsh 宿主）
- 宿主过滤：get_tools_for_agent / custom allowed_tools 不含被隐藏工具
- 纵深防御：tool_hard_deny 对被隐藏工具给可操作错误（指向 pwsh_main）
- prompts 同步：build_identity_prompt / build_context_prompt 的工具名与
  实际暴露一致（[BASH DONE] 事件名不受影响）
- 回滚测试：摘掉过滤（模拟非 Windows）→ bash 系立刻恢复
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from hiveweave.services.host_env import tools_filter
from hiveweave.services.host_env.types import CapabilityLevel, ProbeResult


@pytest.fixture
def windows_pwsh_host():
    """模拟 Windows + pwsh 7.6 探测结果（full）。"""
    with patch.object(tools_filter.sys, "platform", "win32"):
        with patch(
            "hiveweave.services.host_env.runner.get_capability",
            return_value=ProbeResult(
                name="shell.pwsh", level=CapabilityLevel.FULL, detail="7.6.5"
            ),
        ):
            yield


@pytest.fixture
def linux_host():
    with patch.object(tools_filter.sys, "platform", "linux"):
        yield


def test_hidden_tools_on_windows_pwsh_host(windows_pwsh_host):
    hidden = tools_filter.host_hidden_tools()
    assert hidden == frozenset({"bash", "bash_main"})
    assert tools_filter.filter_tools_for_host(
        ["bash", "bash_main", "pwsh", "pwsh_main", "read_file"]
    ) == ["pwsh", "pwsh_main", "read_file"]


def test_bash_visible_on_linux(linux_host):
    assert tools_filter.host_hidden_tools() == frozenset()
    assert tools_filter.filter_tools_for_host(
        ["bash", "bash_main", "pwsh"]
    ) == ["bash", "bash_main", "pwsh"]


def test_pwsh_unprobed_keeps_bash():
    """探测未跑（测试 / 直调环境）→ bash 保持暴露（保守回退，零行为漂移）；
    生产 lifespan 必跑启动探测，不受此回退影响。"""
    assert tools_filter.host_hidden_tools() == frozenset()


async def test_pwsh_main_registered_and_schema_present():
    from hiveweave.tools.base import get_tool_def
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    pwsh_main = get_tool_def("pwsh_main")
    assert pwsh_main is not None
    # 与 pwsh 同参数面（test_run 凭证签发链不受影响）
    assert TOOL_PARAM_SCHEMAS["pwsh_main"]["properties"].keys() == (
        TOOL_PARAM_SCHEMAS["pwsh"]["properties"].keys()
    )
    assert "PROJECT ROOT" in TOOL_PARAM_SCHEMAS["pwsh_main"]["description"]
    # pwsh 描述不再指向 bash_main（T3.2 三处同步）
    from hiveweave.tools.bash import PWSH_TOOL_DESCRIPTION

    assert "pwsh_main" in PWSH_TOOL_DESCRIPTION
    assert "bash_main" not in PWSH_TOOL_DESCRIPTION


async def test_hard_deny_points_to_pwsh_on_windows_host(
    windows_pwsh_host,
):
    from hiveweave.services.policy import tool_hard_deny

    executor = {"role": "developer", "permission_type": "executor",
                "permission_mode": "readwrite"}
    deny = tool_hard_deny(executor, "bash")
    assert deny is not None and "pwsh_main" in deny
    # pwsh 系不受影响
    assert tool_hard_deny(executor, "pwsh") is None
    assert tool_hard_deny(executor, "pwsh_main") is None


async def test_get_tools_for_agent_filtered_on_windows_host(
    windows_pwsh_host,
):
    from hiveweave.services.permission import permission_service

    executor = {"role": "developer", "permission_type": "executor",
                "permission_mode": "readwrite"}
    names = permission_service.get_tools_for_agent(executor)
    assert "bash" not in names
    assert "bash_main" not in names
    assert "pwsh" in names and "pwsh_main" in names


def test_identity_prompt_names_match_host(windows_pwsh_host):
    from hiveweave.prompts.identity import build_identity_prompt

    prompt = build_identity_prompt(
        role="test_engineer", role_type="executor", backstory="", name="QA"
    )
    assert "`bash`" not in prompt
    assert "`bash_main`" not in prompt
    assert "`pwsh_main`" in prompt
    # 平台固定事件名不受工具名替换影响
    assert "[BASH DONE]" in prompt


def test_identity_prompt_keeps_bash_on_linux(linux_host):
    from hiveweave.prompts.identity import build_identity_prompt

    prompt = build_identity_prompt(
        role="test_engineer", role_type="executor", backstory="", name="QA"
    )
    assert "`bash_main`" in prompt
    assert "`pwsh_main`" not in prompt


def test_context_prompt_names_match_host(windows_pwsh_host):
    from hiveweave.prompts.context import build_context_prompt

    ctx = build_context_prompt(
        "agent-x", memories=None, handoffs=None,
        involvement_level="standard", role="test_engineer",
    )
    # QA 角色的 VERIFY 行随宿主切换
    assert "bash_main" not in (ctx or "")
