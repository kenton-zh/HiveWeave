"""Regression: review 工具无文件/非结构化输出必须是中性态（passed=None），
不是假绿灯（passed=True），也不是硬 FAIL（passed=False）。

根因：_execute_single_review 找不到文件时返回 passed=True，
_parse_review_result 对非 JSON 输出 fallback 为 passed=True，
agent 看到 "Passed: True" 但实际什么都没审。
"""

from __future__ import annotations

from hiveweave.tools import review as review_mod


def _no_llm(*args, **kwargs):
    raise AssertionError("LLM must not be called when no files are readable")


async def _no_llm_async(*args, **kwargs):
    raise AssertionError("LLM must not be called when no files are readable")


# ── 1. no readable files → neutral, LLM never called ─────────────────────

async def test_single_review_no_files_is_neutral(tmp_path):
    result = await review_mod._execute_single_review(
        "code_review",
        source_files=["missing.py", "also_missing.py"],
        test_files=[],
        workspace_path=str(tmp_path),
        call_llm=_no_llm_async,
    )
    assert result["passed"] is None
    assert result["no_files"] is True
    assert result["score"] == 0
    assert "No files found to review" in result["summary"]
    assert result["issues"] == []


async def test_single_review_unreadable_paths_neutral(tmp_path):
    # path escaping the workspace counts as unreadable → no files
    result = await review_mod._execute_single_review(
        "security_audit",
        source_files=["C:/Windows/system32/kernel32.dll"],
        test_files=[],
        workspace_path=str(tmp_path),
        call_llm=_no_llm_async,
    )
    assert result["passed"] is None
    assert result["no_files"] is True


# ── 2. unstructured LLM output → neutral fallback ────────────────────────

def test_parse_unstructured_output_neutral():
    result = review_mod._parse_review_result(
        "I looked at the code and everything seems fine, ship it!"
    )
    assert result["passed"] is None
    assert result["issues"] == []
    assert "no automated verdict" in result["summary"]


def test_parse_json_output_still_works():
    result = review_mod._parse_review_result(
        '{"passed": false, "score": 40, "summary": "needs work", "issues": []}'
    )
    assert result["passed"] is False
    assert result["score"] == 40


# ── 3. run_full_review: None verdicts excluded, not forced FAIL ──────────

async def test_full_review_all_no_files_neutral(tmp_path):
    full = await review_mod.run_full_review(
        workspace_path=str(tmp_path),
        file_paths=["ghost.py"],
        test_files=[],
        call_llm=_no_llm_async,
    )
    for key in ("codeReview", "securityAudit", "testReview", "perfAudit"):
        assert full[key]["passed"] is None
        assert full[key]["no_files"] is True
    assert full["overallPassed"] is None
    assert full["overallScore"] == 0


async def test_full_review_mixed_none_and_verdicts(monkeypatch, tmp_path):
    """passed=None 子结果被排除；True/False 正常聚合。"""
    canned = {
        "code_review": {"passed": None, "score": 0, "no_files": True,
                        "summary": "No files found", "issues": []},
        "security_audit": {"passed": True, "score": 80, "summary": "ok",
                           "issues": []},
        "test_review": {"passed": True, "score": 85, "summary": "ok",
                        "issues": []},
        "perf_audit": {"passed": True, "score": 90, "summary": "ok",
                       "issues": []},
    }

    for name in canned:
        async def fake(*args, _name=name, **kwargs):
            return canned[_name]
        monkeypatch.setattr(review_mod, f"run_{name}", fake)

    full = await review_mod.run_full_review(
        workspace_path=str(tmp_path), file_paths=["x.py"], test_files=[],
        call_llm=_no_llm_async,
    )
    assert full["overallPassed"] is True  # None excluded, not counted as fail


async def test_full_review_false_verdict_propagates(monkeypatch, tmp_path):
    canned = {
        "code_review": {"passed": None, "score": 0, "no_files": True,
                        "summary": "No files found", "issues": []},
        "security_audit": {"passed": False, "score": 30, "summary": "bad",
                           "issues": []},
        "test_review": {"passed": True, "score": 85, "summary": "ok",
                        "issues": []},
        "perf_audit": {"passed": True, "score": 90, "summary": "ok",
                       "issues": []},
    }

    for name in canned:
        async def fake(*args, _name=name, **kwargs):
            return canned[_name]
        monkeypatch.setattr(review_mod, f"run_{name}", fake)

    full = await review_mod.run_full_review(
        workspace_path=str(tmp_path), file_paths=["x.py"], test_files=[],
        call_llm=_no_llm_async,
    )
    assert full["overallPassed"] is False


# ── 4. formatting: None must never render as "Passed: True/None" ─────────

def test_format_result_no_files_neutral():
    out = review_mod._format_result({
        "passed": None, "score": 0, "no_files": True,
        "summary": "No files found to review. Checked:\n  - ghost.py",
        "issues": [],
    })
    assert "Passed: N/A — no files reviewed (no verdict)" in out
    assert "No files found to review" in out
    assert "Passed: None" not in out


def test_format_result_unstructured_neutral():
    out = review_mod._format_result({
        "passed": None, "score": None,
        "summary": "Review completed (unstructured output — "
                   "no automated verdict): blah",
        "issues": [],
    })
    assert "Passed: N/A — no automated verdict" in out


async def test_execute_review_full_neutral_not_fail(tmp_path):
    result = await review_mod.execute_review(
        review_type="full_review",
        file_paths=["ghost.py"],
        test_files=[],
        workspace_path=str(tmp_path),
        call_llm=_no_llm_async,
    )
    assert result["success"] is True
    assert "no files reviewed (no verdict)" in result["output"]
    assert "N/A — no automated verdict" in result["output"]
    # 中性态绝不能渲染成 PASS 或 FAIL
    assert "Overall Score: 0/100 — PASS" not in result["output"]
    assert "Overall Score: 0/100 — FAIL" not in result["output"]
