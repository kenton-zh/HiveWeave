"""Regression: every @tool must expose a real LLM schema (no empty additionalProperties).

CEO TEST2 failure mode: waive_attestation / cancel_task arrived with parameters: []
because TOOL_PARAM_SCHEMAS lacked entries and get_tool_schema_for_llm returned
{"type":"object","additionalProperties":true}.
"""

from __future__ import annotations

import hiveweave.tools  # noqa: F401 — populate registry
from hiveweave.tools.base import get_registry, get_tool_def
from hiveweave.tools.executor import (
    get_tool_description,
    get_tool_schema_for_llm,
    validate_tool_args,
)


# Previously missing from TOOL_PARAM_SCHEMAS (empty LLM schema hole)
_CRITICAL_HOLE_TOOLS = (
    "waive_attestation",
    "cancel_task",
    "unclaim_task",
    "git_worktree_create",
    "start_dev_server",
    "lookup_dev_server",
    "message_user",
    "check_agent_status",
)


def test_waive_attestation_schema_has_required_fields():
    schema = get_tool_schema_for_llm("waive_attestation")
    props = schema.get("properties") or {}
    assert "taskId" in props or "task_id" in props
    assert "reason" in props
    required = schema.get("required") or []
    assert "taskId" in required or "task_id" in required
    assert "reason" in required
    # Must NOT be the empty passthrough hole
    assert props, "waive_attestation must not have empty properties"
    assert schema.get("additionalProperties") is not True or props


def test_cancel_task_schema_has_required_fields():
    schema = get_tool_schema_for_llm("cancel_task")
    props = schema.get("properties") or {}
    assert "taskId" in props or "task_id" in props
    assert "reason" in props
    assert schema.get("required")


def test_critical_hole_tools_have_nonempty_schemas():
    for name in _CRITICAL_HOLE_TOOLS:
        schema = get_tool_schema_for_llm(name)
        assert schema.get("type") == "object"
        # Tools with required fields must advertise properties
        td = get_tool_def(name)
        assert td is not None, f"{name} not in @tool registry"
        llm = td.to_llm_schema()
        if llm.get("required"):
            props = schema.get("properties") or {}
            assert props, f"{name}: required fields but empty LLM properties: {schema}"
            for req in llm["required"]:
                # public name (camelCase) should appear in schema props
                assert req in props, f"{name}: missing required prop {req} in {props}"


def test_all_registered_tools_with_required_have_llm_props():
    """Harden: no registered tool with required params may show empty schema."""
    holes = []
    for name, td in get_registry().items():
        llm = td.to_llm_schema()
        if not llm.get("required"):
            continue
        schema = get_tool_schema_for_llm(name)
        props = schema.get("properties") or {}
        if not props:
            holes.append(name)
    assert holes == [], f"LLM schema empty for tools with required fields: {holes}"


def test_waive_attestation_description_from_registry():
    desc = get_tool_description("waive_attestation")
    assert "Waive" in desc or "waive" in desc.lower()
    assert "Execute the waive_attestation tool." != desc


def test_waive_validate_accepts_taskId_alias():
    td = get_tool_def("waive_attestation")
    assert td is not None
    params, err = td.validate(
        {
            "taskId": "37cc32a7-ec39-4ce5-a498-b24b4dca7afd",
            "reason": "docs_only coordinator task; code already on main",
        }
    )
    assert err is None, err
    assert params is not None
    assert params.task_id.startswith("37cc32a7")
    assert "docs_only" in params.reason


def test_validate_tool_args_fallback_for_waive():
    normalized, err = validate_tool_args(
        "waive_attestation",
        {"taskId": "abc", "reason": "x" * 8},
    )
    assert err is None, err
    # Canonical public names from registry schema
    assert "taskId" in normalized or "task_id" in normalized
    assert "reason" in normalized


def test_validate_tool_args_reports_missing_required():
    normalized, err = validate_tool_args("waive_attestation", {})
    assert err is not None
    assert "taskId" in err or "task_id" in err or "Missing" in err
