"""Identity prompt: hire config keys + literal {kind:agent} in f-strings."""

from __future__ import annotations

from hiveweave.prompts.coordinator import build_coordinator_script
from hiveweave.prompts.executor import build_executor_script
from hiveweave.prompts.identity import build_identity_prompt, resolve_prompt_role_type

ARCHITECT = "Python互动学习产品架构师"


def test_resolve_prefers_permission_type_then_role_type():
    assert resolve_prompt_role_type("coordinator", "executor") == "coordinator"
    assert resolve_prompt_role_type("", "coordinator") == "coordinator"
    assert resolve_prompt_role_type(None, "executor") == "executor"
    assert resolve_prompt_role_type(None, None) == "executor"


def test_normalize_mirrors_permission_type_and_role_type():
    from hiveweave.agents.supervisor import normalize_agent_runtime_config

    hired = normalize_agent_runtime_config({"permission_type": "coordinator"})
    assert hired["permission_type"] == "coordinator"
    assert hired["role_type"] == "coordinator"

    restarted = normalize_agent_runtime_config({"role_type": "coordinator"})
    assert restarted["permission_type"] == "coordinator"
    assert restarted["role_type"] == "coordinator"

    conflict = normalize_agent_runtime_config(
        {"role_type": "executor", "permission_type": "coordinator"}
    )
    assert conflict["permission_type"] == "coordinator"
    assert conflict["role_type"] == "coordinator"


def test_generic_executor_script_keeps_literal_kind_braces():
    text = build_executor_script(ARCHITECT, "青梧")
    assert "{kind:agent" in text
    assert "{kind:task" in text
    assert "{{kind" not in text
    assert "You are an EXECUTOR" in text


def test_generic_coordinator_script_builds():
    text = build_coordinator_script(ARCHITECT, "青梧")
    assert "You are a COORDINATOR" in text
    assert ARCHITECT in text
    assert "one `ask_agent` to HR" in text
    assert "Do not send a work letter plus a separate" in text
    assert "ASSIGNEE_MUST_SUBMIT" in text
    assert "park=delegated" in text


def test_ceo_hire_flow_is_one_ask_not_two_letters():
    text = build_coordinator_script("ceo", "归零")
    assert "You are the CEO" in text
    assert "ask_agent` to HR" in text
    assert "Two inbox items" in text
    assert "请回报招聘结果" in text
    assert (
        "Use `send_message` with recipients=[\"HR的花名\"] to send the hiring request"
        not in text
    )
    assert "message HR with specific hiring requests" not in text


def test_identity_instruct_hr_uses_ask_agent():
    text = build_identity_prompt(
        role="ceo",
        role_type="coordinator",
        backstory="",
        name="归零",
        permission_type="coordinator",
    )
    assert "I will instruct HR" in text
    assert "call `ask_agent` to HR" in text
    assert "call `send_message` to HR in the same turn" not in text
    assert "One ask carries the work" in text
    assert "claimed ≠ idle" in text


def test_identity_hire_config_permission_type_only_uses_coordinator_script():
    """Live hire starts with permission_type, not the restart SQL role_type alias."""
    text = build_identity_prompt(
        role=ARCHITECT,
        role_type="",
        backstory="",
        name="青梧",
        permission_type="coordinator",
    )
    assert "You are a COORDINATOR" in text
    assert ARCHITECT in text
    assert "{kind:agent" in text
    assert "## Permission Level: coordinator" in text


def test_identity_executor_path_does_not_nameerror():
    text = build_identity_prompt(
        role="签到排行榜工程师",
        role_type="executor",
        backstory="",
        name="星野",
    )
    assert "You are an EXECUTOR" in text
    assert "{kind:agent" in text
    assert "{kind:task" in text
    assert "{{kind" not in text


def test_identity_permission_type_wins_over_stale_role_type():
    text = build_identity_prompt(
        role=ARCHITECT,
        role_type="executor",
        backstory="",
        name="青梧",
        permission_type="coordinator",
    )
    assert "You are a COORDINATOR" in text
    assert "You are an EXECUTOR" not in text
