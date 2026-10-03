"""批21 修复1 回归：VERIFY 验收清单门处方化（契约墙最后残余）。

实锤（s3-clone_21 里程碑 QA 任务）：submit 被「verify acceptance checklist」
连拒 3 次 → gate_reject_guard 以 [GATE REJECT LOOP] 自动挂起——拒收回执只有
抽象处方（逐 kind 指引），没有**逐条按缺口 id 实例化**的可照抄声明，模型只能
爬墙猜测。

本文件锁死 T37 处方化范式在本门的落地：
  ① 缺指定条目 ⇒ 拒收回执逐条点名该条目（id + 原文）；
  ② 回执附 COPY-PASTE 模板：库里有本任务合格凭证时实时填真 id，
     **照抄该 JSON 即通过同一张覆盖门**（示例值经 is_placeholder_text
     自查，不落在 _PLACEHOLDER_LEADS 占位前缀上——P0-4 同款纪律）；
  ③ 查不到凭证时退示例值 + 明示「照抄示例 id 不会通过核验」。
"""

from __future__ import annotations

import json

from hiveweave.services.delivery_contract import is_placeholder_text
from hiveweave.services.tasks.acceptance import (
    CoverageAssessment,
    CoverageShapeError,
    format_acceptance_coverage_copyable,
    format_acceptance_coverage_verdict,
    uncovered_acceptance_items,
)

_CRITERIA = ["UI 首屏不白屏", "交互不报错"]
_REAL_ID = "0493344c-test-run-real"


def _assessment(
    *,
    undeclared: list[str] | None = None,
    unverified: list[str] | None = None,
    gap_ids: list[str] | None = None,
    shape_errors: list[CoverageShapeError] | None = None,
) -> CoverageAssessment:
    return CoverageAssessment(
        undeclared=undeclared or [],
        unverified=unverified or [],
        unverified_ids=list(gap_ids or []),
        shape_errors=shape_errors or [],
        unknown_ids=[],
        credential_notes={},
        waived=False,
        gap_lines=(undeclared or []) + (unverified or []),
        gap_ids=list(gap_ids or []),
    )


def _copyable_json(receipt: str) -> dict:
    """从回执 COPY-PASTE 区块解析出可照抄的 acceptanceCoverage JSON。"""
    assert "COPY-PASTE" in receipt
    tail = receipt.split("COPY-PASTE", 1)[1]
    start = tail.index("{")
    end = tail.rindex("}") + 1
    return json.loads(tail[start:end])


def test_reject_receipt_names_each_missing_item():
    """① 缺指定条目 ⇒ 回执逐条点名（id + 原文），不是抽象一句话。"""
    a = _assessment(
        undeclared=[f"条目{i + 1}: {t}" for i, t in enumerate(_CRITERIA)],
        gap_ids=["1", "2"],
    )
    receipt = format_acceptance_coverage_verdict(a, ("test_run",), [])
    for text in _CRITERIA:
        assert text in receipt, f"回执未点名条目原文：{text}"


def test_copy_paste_example_passes_same_gate():
    """② 回执示例照抄 ⇒ 过：模板填真 id，照抄进 sync 覆盖门判全覆盖。"""
    a = _assessment(
        undeclared=[f"条目{i + 1}: {t}" for i, t in enumerate(_CRITERIA)],
        gap_ids=["1", "2"],
    )
    receipt = format_acceptance_coverage_verdict(
        a, ("test_run",), [_REAL_ID]
    )
    copied = _copyable_json(receipt)
    assert copied == {
        "1": {"attestation_ids": [_REAL_ID]},
        "2": {"attestation_ids": [_REAL_ID]},
    }
    # 照抄即过同一张门：模板里每个值都不得落在占位前缀上（P0-4 纪律）
    for claim in copied.values():
        for aid in claim["attestation_ids"]:
            assert not is_placeholder_text(aid), aid
    gaps = uncovered_acceptance_items(
        _CRITERIA,
        {"acceptance_coverage": copied},
        verified_ids={_REAL_ID},
    )
    assert gaps == [], f"照抄回执示例未通过覆盖门：{gaps}"


def test_copy_paste_without_credentials_falls_back_clean():
    """③ 查不到凭证 ⇒ 示例值不落占位前缀，并明示照抄示例 id 不会过核验。"""
    a = _assessment(
        unverified=["条目1: UI 首屏不白屏"],
        gap_ids=["1"],
    )
    receipt = format_acceptance_coverage_verdict(a, ("test_run",), [])
    copied = _copyable_json(receipt)
    for claim in copied.values():
        for aid in claim["attestation_ids"]:
            assert not is_placeholder_text(aid), aid
    assert "不会通过核验" in receipt
    # None（旧调用方）不附加模板 —— 行为不变
    assert "COPY-PASTE" not in format_acceptance_coverage_verdict(a, ("test_run",))


def test_shape_error_items_included_in_template():
    """形状错条目同样进模板（修好形状后 waiver 才接手的语义不回退）。"""
    a = _assessment(
        shape_errors=[
            CoverageShapeError(item_id="2", detail="多键歧义：收到键 ['a', 'b']")
        ],
        gap_ids=["2"],
    )
    receipt = format_acceptance_coverage_verdict(
        a, ("test_run",), [_REAL_ID]
    )
    copied = _copyable_json(receipt)
    assert "2" in copied
    # 形状错按态分解点名不回退（【形状错】区 + 原始形态摘要）
    assert "【形状错】" in receipt and "多键歧义" in receipt


def test_copyable_helper_no_gaps_returns_empty():
    """无缺口条目（如仅 unknown-id 声明）不造模板，回执正文已点名。"""
    a = _assessment()
    assert format_acceptance_coverage_copyable(a, ("test_run",), [_REAL_ID]) == ""
