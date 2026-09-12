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
            "evidenceAttestationId": "att-evidence-1",
        }
    )
    assert err is None, err
    assert params is not None
    assert params.task_id.startswith("37cc32a7")
    assert "docs_only" in params.reason
    assert params.evidence_attestation_id == "att-evidence-1"


def test_validate_tool_args_fallback_for_waive():
    normalized, err = validate_tool_args(
        "waive_attestation",
        {
            "taskId": "abc",
            "reason": "x" * 8,
            "evidenceAttestationId": "att-1",
        },
    )
    assert err is None, err
    # Canonical public names from registry schema
    assert "taskId" in normalized or "task_id" in normalized
    assert "reason" in normalized
    assert (
        "evidenceAttestationId" in normalized
        or "evidence_attestation_id" in normalized
    )


def test_validate_tool_args_reports_missing_required():
    normalized, err = validate_tool_args("waive_attestation", {})
    assert err is not None
    assert "taskId" in err or "task_id" in err or "Missing" in err


def test_validate_tool_args_allows_omitting_evidence():
    """Schema: CEO may omit evidenceAttestationId. Coordinator still gated at runtime."""
    normalized, err = validate_tool_args(
        "waive_attestation",
        {"taskId": "abc", "reason": "x" * 20},
    )
    assert err is None
    assert normalized is not None


# ── 参数别名可达性（report TEST_DSH_54 #11 的**系统形态**）──────────────
#
# 别名有两张表：
#   · 手写的 `TOOL_PARAM_SCHEMAS`（模型可见面）
#   · pydantic 模型上的 `json_schema_extra={"aliases": [...]}`（代码强制面）
# 实测曾有 **73 处**别名只存在于后者 ⇒ 模型用它**天然会说的名字**
# （`dispatch_task` 的 `to`、`get_tasks` 的 `state`、`submit_task` 的
# `conclusion` / `blocking`、`list_files` 的 `dir_path`）会被
# `validate_tool_args` 判成 `Unknown parameters` 直接拒绝 —— 而代码本来是
# 接受的。这与 #11（`apply_patch.patches[].op`）和"三件套"纪律
# （`create_task.tags`）是同一类病：**模型可见的契约 ≠ 代码强制的契约**。
#
# 修法改机制（`_canonical_for_arg`：schema 精确 → 工具自身 pydantic 模型兜底
# → camel↔snake 归一），而不是逐条同步两张表 —— 手工同步已失败三次。
# 本条是**棘轮**：可达性只能变好不能变差。


_KNOWN_UNREACHABLE_ALIASES = frozenset({
    # read_memory 的 pydantic 字段叫 agent_id、schema 里却叫 moduleId ——
    # 两张表对同一形参用了**不同正名**，属真分歧（不是别名漏同步），
    # 需要人决定改哪一边，故进基线而不是在门禁里猜。
    "read_memory::agent_id::agent",
    "read_memory::agent_id::agentId",
    "read_memory::agent_id::agent_id",
})


def _unreachable_aliases() -> set[str]:
    """枚举「pydantic 声明了、但门禁判为 Unknown」的别名。"""
    from hiveweave.tools.base import _extract_aliases, get_registry
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    out: set[str] = set()
    for name, td in get_registry().items():
        schema = TOOL_PARAM_SCHEMAS.get(name)
        if not schema:
            continue
        props = schema.get("properties") or {}
        params = getattr(td, "params_model", None)
        if params is None:
            continue
        for field_name, field in params.model_fields.items():
            for alias in _extract_aliases(field):
                if alias in props:
                    continue
                if any(alias in (p.get("aliases") or []) for p in props.values()):
                    continue
                _norm, err = validate_tool_args(name, {alias: "probe"})
                if err and "Unknown parameters" in err:
                    out.add(f"{name}::{field_name}::{alias}")
    return out


def test_no_new_unreachable_aliases_ratchet():
    """棘轮：不可达别名集合必须 ⊆ 已知基线（修好可以，新增不行）。"""
    current = _unreachable_aliases()
    new = current - _KNOWN_UNREACHABLE_ALIASES
    assert not new, (
        "新出现「pydantic 声明了但门禁拒绝」的别名（模型会用却会被拒）：\n  "
        + "\n  ".join(sorted(new))
        + "\n修法二选一：把别名补进 TOOL_PARAM_SCHEMAS，或确认它在 pydantic 侧"
        "真的是想要的。**不要**只改一侧就提交。"
    )


def test_pydantic_declared_aliases_are_accepted():
    """代表性命中：模型自然会用、而 schema 未声明的别名必须可用。"""
    cases = [
        ("dispatch_task", {"to": "A009", "task": "x", "submitGate": "unit"}, None),
        ("get_tasks", {"state": "running"}, None),
        (
            "submit_task",
            {
                "taskId": "t", "summary": "s", "testsPassed": "x",
                "conclusion": "PASS",
            },
            None,
        ),
        # camel↔snake 归一：schema 只有 dirPath
        ("list_files", {"dir_path": "."}, None),
    ]
    for tool, args, _ in cases:
        _normalized, err = validate_tool_args(tool, args)
        assert err is None, f"{tool} 拒绝了 pydantic 已声明的别名：{err}"


def test_real_unknown_params_still_rejected():
    """反向对照：兜底不得变成"什么都收"——真未知参数仍旧 fail loud。

    ⚠ 审计 2026-09-12：本测试原先用 `dispatch_task`，而它有必填参数 ⇒
    返回的是「Missing required parameters」，`or "Missing required"` 一短路，
    断言就恒绿（把 unknown 分支整段删掉也不转红）。改用 **required 为空**的
    `get_tasks`，让 `Unknown parameters` 分支成为唯一可能的判据。
    """
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    schema = TOOL_PARAM_SCHEMAS["get_tasks"]
    assert not schema.get("required"), (
        "本反向对照依赖 get_tasks 无必填参数（否则又会被 Missing 短路）"
    )
    _n, err = validate_tool_args("get_tasks", {"zz_not_a_param": "x"})
    assert err is not None
    assert "Unknown parameters" in err, f"真未知参数必须被拒，实得：{err}"

    # 归一化不得放宽到"连 pydantic 都不认的拼写"（审计实测过 dirpath）
    _n2, err2 = validate_tool_args("list_files", {"dirpath": "."})
    assert err2 is not None and "Unknown parameters" in err2, (
        "归一化只应作用于 pydantic 认得的字段名；dirpath 连 pydantic 都不认"
    )
    # 而 CamelCase 变体（schema 正名）当然仍被接受
    _n3, err3 = validate_tool_args("list_files", {"dirPath": "."})
    assert err3 is None

