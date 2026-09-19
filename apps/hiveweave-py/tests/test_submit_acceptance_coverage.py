"""TEST_DSH_63 接线修复回归：acceptance_coverage 从 submit_task 到覆盖门的三处接线。

修复前（P0 结构断线，门自出生起经 submit_task 不可满足）：
  · SubmitTaskParams 无该字段（pydantic extra=ignore ⇒ 模型硬传也被静默吞）
  · evidence 组装区不喂 COVERAGE_EVIDENCE_KEY ⇒ 覆盖门永远判全缺
  · executor 的 TOOL_PARAM_SCHEMAS["submit_task"] 零暴露 ⇒ 传参被判 Unknown
本文件锁死：参数入口 → evidence 透传 → LLM schema 暴露 → 判据解析往返。
"""

from __future__ import annotations

import hiveweave.tools  # noqa: F401 — populate @tool registry

from hiveweave.services.tasks.acceptance import (
    COVERAGE_EVIDENCE_KEY,
    parse_acceptance_coverage,
)
from hiveweave.tools.executor import (
    TOOL_PARAM_SCHEMAS,
    get_tool_schema_for_llm,
    validate_tool_args,
)
from hiveweave.tools.tasks.submit import SubmitTaskParams, _build_evidence

_SAMPLE = {
    "1": {"attestation_ids": ["att-1"]},
    "2": {"not_applicable_reason": "本环境无边界数据集"},
}


def _params(**kw: object) -> SubmitTaskParams:
    base: dict = {"task_id": "t-1", "summary": "s", "tests_passed": True}
    base.update(kw)
    return SubmitTaskParams(**base)


def test_params_accept_both_spellings():
    """alias（acceptanceCoverage）与字段名（populate_by_name）都必须进模型。"""
    by_alias = SubmitTaskParams.model_validate(
        {"taskId": "t", "summary": "s", "acceptanceCoverage": _SAMPLE}
    )
    by_name = SubmitTaskParams.model_validate(
        {"taskId": "t", "summary": "s", "acceptance_coverage": _SAMPLE}
    )
    assert by_alias.acceptance_coverage == _SAMPLE
    assert by_name.acceptance_coverage == _SAMPLE


def test_build_evidence_includes_acceptance_coverage():
    ev = _build_evidence(_params(acceptance_coverage=_SAMPLE), "generic_tests", [])
    assert ev[COVERAGE_EVIDENCE_KEY] == _SAMPLE


def test_build_evidence_omits_key_when_unset():
    ev = _build_evidence(_params(), "generic_tests", [])
    assert COVERAGE_EVIDENCE_KEY not in ev
    # 基线骨架不回归（抽函数前的既有键仍在）
    assert ev["summary"] == "s"
    assert ev["tests_passed"] is True
    assert ev["attestation_ids"] == []


def test_schema_exposes_acceptanceCoverage():
    """LLM 可见面（TOOL_PARAM_SCHEMAS）必须暴露 acceptanceCoverage。"""
    props = TOOL_PARAM_SCHEMAS["submit_task"]["properties"]
    assert "acceptanceCoverage" in props
    assert "acceptance_coverage" in props["acceptanceCoverage"]["aliases"]
    # get_tool_schema_for_llm 走同一张表（strip aliases 后属性名仍在）
    llm_props = get_tool_schema_for_llm("submit_task")["properties"]
    assert "acceptanceCoverage" in llm_props


def test_validate_tool_args_accepts_acceptanceCoverage():
    """门禁可达：acceptanceCoverage / acceptance_coverage 都不再判 Unknown。"""
    for key in ("acceptanceCoverage", "acceptance_coverage"):
        normalized, err = validate_tool_args(
            "submit_task",
            {"taskId": "t", "summary": "s", "testsPassed": True, key: _SAMPLE},
        )
        assert err is None, f"{key} 被拒：{err}"
        assert normalized["acceptanceCoverage"] == _SAMPLE


def test_parse_acceptance_coverage_roundtrip():
    """声明样例 → parse → 判据形状往返（dict / 裸串 / 坏形状 fail-closed）。"""
    claims = parse_acceptance_coverage({COVERAGE_EVIDENCE_KEY: _SAMPLE})
    assert claims == {
        "1": {"attestation_ids": ["att-1"], "not_applicable_reason": ""},
        "2": {"attestation_ids": [], "not_applicable_reason": "本环境无边界数据集"},
    }
    # 简写形态（裸字符串凭证 id）归一成 attestation_ids 声明（无 reason 键）
    assert parse_acceptance_coverage(
        {COVERAGE_EVIDENCE_KEY: {"3": "att-3"}}
    ) == {"3": {"attestation_ids": ["att-3"]}}
    # 坏形状一律忽略 = 未覆盖（fail-closed）
    assert parse_acceptance_coverage({COVERAGE_EVIDENCE_KEY: {"4": {"foo": 1}}}) == {}
    assert parse_acceptance_coverage(None) == {}
