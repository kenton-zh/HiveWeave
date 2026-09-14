"""`ROLLED_BACK` 出口的状态守卫（独立审计 P0，2026-09-14）。

背景：`audit_outcome_state` 原先只判 `result["audited"]`。而 `ROLLED_BACK`
出口会 `audited=True` 但**不发凭证**（回执自己写着「不发新的 PASS 凭证」）
⇒ 被判 `ready` ⇒ `success=True` ⇒ `round_made_progress` 判真
（`doom_loop.py`）⇒ `tool_loop` 清零全部 stall 计数 ⇒ **同源无限重发**
——正是本批要治的病，在另一个出口原样存在。

修法：判 `ready` 必须**同时**有 `attestation_id`（三个真发凭证的出口
PASS / 缓存命中 / 常规都带它，故不误伤正常路径）。

阳性对照：把判据改回 `if result.get("audited"):` → 本文件转红。
"""
from __future__ import annotations

from hiveweave.services.code_audit import (
    AUDIT_STATE_ACCEPTED_PENDING,
    AUDIT_STATE_FAILED,
    AUDIT_STATE_READY,
    audit_outcome_state,
)


def test_rolled_back_without_attestation_is_not_ready():
    """`audited=True` 但无凭证 ⇒ **不得**判 ready（否则等于骗 agent 说成功）。"""
    assert (
        audit_outcome_state({"audited": True, "verdict": "ROLLED_BACK"})
        == AUDIT_STATE_FAILED
    )
    # 更严：任何"自称 audited 却没有 attestation_id"的形态都不许 ready
    assert audit_outcome_state({"audited": True}) == AUDIT_STATE_FAILED


def test_ready_requires_attestation_id():
    """真有凭证的出口都带 `attestation_id` ⇒ 仍判 ready（不误伤）。"""
    assert (
        audit_outcome_state({"audited": True, "attestation_id": "att-1"})
        == AUDIT_STATE_READY
    )


def test_pending_and_unknown_states():
    """已入队未耗尽 ⇒ pending；判定不出的终局 ⇒ failed（fail-closed）。"""
    assert (
        audit_outcome_state({"retry_queued": True, "retry_exhausted": False})
        == AUDIT_STATE_ACCEPTED_PENDING
    )
    assert audit_outcome_state({}) == AUDIT_STATE_FAILED
    assert (
        audit_outcome_state({"retry_queued": True, "retry_exhausted": True})
        == AUDIT_STATE_FAILED
    )
