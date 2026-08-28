"""T2.1 merge 失败文案归因测试 —— 每个 hint 的分支归属钉死。

审计实测（TEST_DSH_35）：「分支零匹配」与「precondition_failed」都被拼上
MERGE_CONFLICT_HINT，引导 coordinator 对不存在的冲突 rework。选择器
``format_merge_failure_message`` 是工具失败兜底的唯一入口，这里按
deepseek-harness render.ts:85-92 纪律钉住：if/elif 排他，绝不拼接。
"""
from __future__ import annotations

from hiveweave.services.worktree_review import (
    BRANCH_LOOKUP_FAILED_HINT,
    MERGE_CONFLICT_HINT,
    MERGE_FAILED_HINT,
    PRECONDITION_FAILED_HINT,
    UNTRACKED_ON_TARGET_HINT,
    format_merge_failure_message,
)


def _msg(reason: str = "", conflicts=None, untracked=None) -> str:
    return format_merge_failure_message(
        reason=reason,
        branch="hw/A001/task",
        target="main",
        conflicts=conflicts,
        untracked=untracked,
    )


# ── 常量自描述（防 banner 被改掉后失去归因价值） ─────────────────────────


def test_branch_lookup_hint_is_not_conflict():
    assert "NOT A MERGE CONFLICT" in BRANCH_LOOKUP_FAILED_HINT
    assert "git log" in BRANCH_LOOKUP_FAILED_HINT  # 指向查证，不是 rework


def test_precondition_hint_is_not_conflict():
    assert "NOT A MERGE CONFLICT" in PRECONDITION_FAILED_HINT
    assert "dry_run" in PRECONDITION_FAILED_HINT


def test_conflict_hint_keeps_aborted_attribution():
    """既有防线：MERGE_CONFLICT_HINT 本体仍含 aborted（test_worktree_review 同款）。"""
    assert "aborted" in MERGE_CONFLICT_HINT.lower()
    assert "EXECUTOR FIXES IN WORKTREE" in MERGE_CONFLICT_HINT


# ── 选择器排他归属 ───────────────────────────────────────────────────────


def test_real_conflict_reasons_get_conflict_hint():
    for reason in ("merge_conflict", "conflict_markers_landed"):
        msg = _msg(reason, conflicts=["a.ts"])
        assert "MERGE CONFLICT" in msg and "NOT A MERGE CONFLICT" not in msg


def test_empty_reason_with_conflicts_gets_conflict_hint():
    msg = _msg("", conflicts=["a.ts"])
    assert "MERGE CONFLICT" in msg


def test_untracked_reason_gets_untracked_hint_only():
    msg = _msg("untracked_on_target", untracked=["x.json"])
    assert "UNTRACKED ON MAIN" in msg
    assert MERGE_CONFLICT_HINT not in msg


def test_precondition_reason_gets_precondition_hint_only():
    msg = _msg("precondition_failed")
    assert "MERGE PRECONDITION FAILED" in msg
    assert MERGE_CONFLICT_HINT not in msg
    assert UNTRACKED_ON_TARGET_HINT not in msg


def test_unknown_reason_gets_merge_failed_hint_only():
    for reason in ("merge_failed", "some_new_reason", ""):
        msg = _msg(reason)
        assert "CAUSE NOT CONFIRMED AS CONFLICT" in msg
        assert MERGE_CONFLICT_HINT not in msg
        assert MERGE_FAILED_HINT in msg


def test_branch_lookup_failure_text_has_no_conflict_banner():
    """拼接点①回归：工具层「No worktree branch found」响应不再拼冲突提示。"""
    simulated = f"No worktree branch found for agent A001\n\n{BRANCH_LOOKUP_FAILED_HINT}"
    assert MERGE_CONFLICT_HINT not in simulated
    assert "NOT A MERGE CONFLICT" in simulated
