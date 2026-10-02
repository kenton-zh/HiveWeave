"""批 6 一期 —— 工具契约单源化（报告缺口 19 / prompt-authoring skill §3 缺口 9）。

四个重灾工具（question / submit_task / dismiss_agent / bash 家族）的
``TOOL_PARAM_SCHEMAS`` 手写条目改为单源引用：

- description：工具模块级常量（``*_TOOL_DESCRIPTION``）是唯一 owner，
  ``@tool`` 注册处与 executor 手写条目都引用它；
- properties/required：由 pydantic 参数模型经 ``_schema_fields_from_pydantic``
  派生，手写层不再手写任何字段。

本文件钉住三件事：描述等于常量、字段与 pydantic 派生一致、bash 家族共享
同一 timeout 子字典且含 "(10 min)"。
"""

from hiveweave.tools.bash import (
    BASH_MAIN_TOOL_DESCRIPTION,
    BASH_TOOL_DESCRIPTION,
    PWSH_MAIN_TOOL_DESCRIPTION,
    PWSH_TOOL_DESCRIPTION,
    TOOL_DEFAULT_TIMEOUT_MS,
    BashParams,
)
from hiveweave.tools.executor import (
    TOOL_PARAM_SCHEMAS,
    _schema_fields_from_pydantic,
    get_tool_description,
    get_tool_schema_for_llm,
    validate_tool_args,
)
from hiveweave.tools.org_tools import (
    DISMISS_AGENT_TOOL_DESCRIPTION,
    DismissAgentParams,
)
from hiveweave.tools.question import QUESTION_TOOL_DESCRIPTION, QuestionParams
from hiveweave.tools.tasks.submit import (
    SUBMIT_TASK_TOOL_DESCRIPTION,
    SubmitTaskParams,
)


# ── 描述单源：get_tool_description / 手写条目 / @tool 注册 == 常量 ──


def test_question_description_single_source():
    assert get_tool_description("question") == QUESTION_TOOL_DESCRIPTION
    assert TOOL_PARAM_SCHEMAS["question"]["description"] == QUESTION_TOOL_DESCRIPTION
    from hiveweave.tools.base import get_tool_def

    assert get_tool_def("question").description == QUESTION_TOOL_DESCRIPTION
    # 新语义关键句在位（缺口 9：旧语义不得回流）
    assert "NEVER treat a failed question as" in QUESTION_TOOL_DESCRIPTION


def test_submit_task_description_single_source():
    assert get_tool_description("submit_task") == SUBMIT_TASK_TOOL_DESCRIPTION
    assert (
        TOOL_PARAM_SCHEMAS["submit_task"]["description"]
        == SUBMIT_TASK_TOOL_DESCRIPTION
    )
    from hiveweave.tools.base import get_tool_def

    assert get_tool_def("submit_task").description == SUBMIT_TASK_TOOL_DESCRIPTION
    # 批3（2026-09-29，有意变更）：审计门条款随 SUBMIT_AUDIT_GATE 灰度装配
    # —— off（默认，逐提交审计门已拆）无 CODE AUDIT GATE / waive /
    # attestation_impossible 条款；on 恢复全文。组装 owner =
    # build_submit_task_description（单源，B6）。
    from hiveweave.config import submit_audit_gate_enabled
    from hiveweave.tools.tasks.submit import build_submit_task_description

    assert SUBMIT_TASK_TOOL_DESCRIPTION == build_submit_task_description(
        audit_gate=submit_audit_gate_enabled()
    )
    if submit_audit_gate_enabled():
        assert "CODE AUDIT GATE" in SUBMIT_TASK_TOOL_DESCRIPTION
        assert "waive_attestation" in SUBMIT_TASK_TOOL_DESCRIPTION
        assert "attestation_impossible" in SUBMIT_TASK_TOOL_DESCRIPTION
    else:
        assert "CODE AUDIT GATE" not in SUBMIT_TASK_TOOL_DESCRIPTION
        assert "request_code_audit" not in SUBMIT_TASK_TOOL_DESCRIPTION


def test_dismiss_agent_description_single_source():
    assert get_tool_description("dismiss_agent") == DISMISS_AGENT_TOOL_DESCRIPTION
    assert (
        TOOL_PARAM_SCHEMAS["dismiss_agent"]["description"]
        == DISMISS_AGENT_TOOL_DESCRIPTION
    )
    from hiveweave.tools.base import get_tool_def

    assert get_tool_def("dismiss_agent").description == DISMISS_AGENT_TOOL_DESCRIPTION
    # 配额预告在位
    assert "dismiss quota" in DISMISS_AGENT_TOOL_DESCRIPTION
    assert "rehire cooldown" in DISMISS_AGENT_TOOL_DESCRIPTION


def test_bash_family_descriptions_single_source():
    cases = {
        "bash": BASH_TOOL_DESCRIPTION,
        "bash_main": BASH_MAIN_TOOL_DESCRIPTION,
        "pwsh": PWSH_TOOL_DESCRIPTION,
        "pwsh_main": PWSH_MAIN_TOOL_DESCRIPTION,
    }
    for name, const in cases.items():
        assert get_tool_description(name) == const, name
        assert TOOL_PARAM_SCHEMAS[name]["description"] == const, name
        from hiveweave.tools.base import get_tool_def

        assert get_tool_def(name).description == const, name
    # 方言与截断处方在位（pwsh 常量含 SANDBOX_TEMP_GUIDE，bash 不含）
    assert "auto-translation" in BASH_TOOL_DESCRIPTION
    assert "truncated to head+tail" in BASH_TOOL_DESCRIPTION
    assert "PowerShell" in PWSH_TOOL_DESCRIPTION


# ── 字段单源：properties/required 与 pydantic 派生一致 ──────────────


def test_properties_derive_from_pydantic():
    cases = {
        "bash": BashParams,
        "question": QuestionParams,
        "dismiss_agent": DismissAgentParams,
        "submit_task": SubmitTaskParams,
    }
    for name, model in cases.items():
        derived = _schema_fields_from_pydantic(model)
        assert TOOL_PARAM_SCHEMAS[name]["properties"] == derived["properties"], name
        assert TOOL_PARAM_SCHEMAS[name]["required"] == derived["required"], name


def test_required_match_pydantic():
    # pydantic 是 required 的唯一 owner：submit 的 testsPassed/taskId 不再由
    # 参数层硬拦（真实门禁回执接管）—— 有意切换 owner 的行为面，钉住。
    # P0-4：顶层 summary 亦改为可选（缺失时从 deliveryContract.summary
    # 回填，两者皆缺才由门禁拒）⇒ submit 无参数层 required（空 required
    # 时 to_llm_schema 直接省略该键）。
    assert get_tool_schema_for_llm("bash")["required"] == ["command"]
    assert get_tool_schema_for_llm("question")["required"] == ["question"]
    assert get_tool_schema_for_llm("dismiss_agent")["required"] == ["agentId"]
    assert get_tool_schema_for_llm("submit_task").get("required", []) == []


def test_property_types_survive_optional_unwrap():
    """PEP-604 Optional 字段的类型不得错落 string（helper 以 raw schema 权威）。"""
    submit_props = get_tool_schema_for_llm("submit_task")["properties"]
    assert submit_props["filesChanged"]["type"] == "array"
    assert submit_props["filesChanged"]["items"] == {"type": "string"}
    assert submit_props["testsPassed"]["type"] == "boolean"
    assert submit_props["deliveryContract"]["type"] == "object"
    assert submit_props["summary"]["type"] == "string"


# ── bash 家族：timeout 子字典共享 + "(10 min)" 文案 ─────────────────


def test_bash_family_timeout_copy_and_sharing():
    timeout = TOOL_PARAM_SCHEMAS["bash"]["properties"]["timeout"]
    for name in ("bash_main", "pwsh", "pwsh_main"):
        # 既有性质：四个条目共享同一 timeout 子字典对象（一处编辑四处生效）
        assert TOOL_PARAM_SCHEMAS[name]["properties"]["timeout"] is timeout, name
        assert "(10 min)" in timeout["description"], name
    assert (
        f"Default: {TOOL_DEFAULT_TIMEOUT_MS['bash']} ms (10 min)"
        in timeout["description"]
    )
    assert "Ignored when background=true" in timeout["description"]


def test_pwsh_command_property_is_dialect_contract():
    from hiveweave.tools.bash import PWSH_COMMAND_DESCRIPTION

    cmd = TOOL_PARAM_SCHEMAS["pwsh"]["properties"]["command"]
    assert cmd["description"] == PWSH_COMMAND_DESCRIPTION
    assert TOOL_PARAM_SCHEMAS["bash"]["properties"]["command"] is not cmd
    # pwsh 只换 command 的描述，timeout/background 等仍与 bash 共享对象
    assert (
        TOOL_PARAM_SCHEMAS["pwsh"]["properties"]["timeout"]
        is TOOL_PARAM_SCHEMAS["bash"]["properties"]["timeout"]
    )


# ── 别名可达性：手写层历史别名不因切换 owner 丢失（回填进 pydantic）──


def test_legacy_aliases_still_reachable():
    cases = [
        ("bash", {"cmd": "echo hi"}, None),
        ("question", {"message": "hi?"}, None),
        ("dismiss_agent", {"target": "A009"}, None),
        # submit 无参数层 required（P0-4 后 summary 亦可选）——历史别名
        # 可达的判据是「不报 Unknown parameters」，缺 summary 属正常 Missing。
        ("submit_task", {"summary": "s", "commitSha": "abc"}, None),
        ("submit_task", {"summary": "s", "description": "did things"}, None),
        ("submit_task", {"summary": "s", "commitHash": "def"}, None),
    ]
    for tool_name, args, _ in cases:
        _normalized, err = validate_tool_args(tool_name, args)
        assert err is None, f"{tool_name} 丢了历史别名 {args}: {err}"
        if err is None:
            assert "Unknown parameters" not in (err or ""), (tool_name, err)


def test_submit_commitHash_not_misrouted_to_commit():
    normalized, _err = validate_tool_args(
        "submit_task", {"summary": "s", "commitHash": "deadbeef"}
    )
    assert normalized is not None
    assert "commitHash" in normalized, normalized
    assert "commit" not in normalized or normalized.get("commit") is None


def test_llm_facing_schema_strips_aliases():
    schema = get_tool_schema_for_llm("question")
    assert all("aliases" not in p for p in schema["properties"].values())
    bash_schema = get_tool_schema_for_llm("bash")
    assert bash_schema["properties"]["timeout"].get("maximum") == 600000
