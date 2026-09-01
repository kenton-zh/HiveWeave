"""P0-3 fail-loud (TEST_DSH_38): llm_failed no longer drops the submit kind.

Submit keeps code_audit required and the tool path rejects with an
explicit-waive hint; only the approve/HTTP re-check keeps accepting a
legacy evidence stamp (drop_code_audit_kind_if_soft).
"""

from __future__ import annotations

from hiveweave.services.attestation import POLICY_REQUIRED_KINDS
from hiveweave.services.code_audit import (
    CODE_AUDIT_KIND,
    code_audit_soft_fail_pending,
    drop_code_audit_kind_if_soft,
    record_audit_attempt,
    reset_ledger,
)


def setup_function(_fn=None):
    reset_ledger("agent-soft")
    reset_ledger("agent-other")


def test_llm_failed_pending_but_kind_stays_required():
    record_audit_attempt("agent-soft", "llm_failed", "19fb6fb9-183e-460c-9397")
    needed = POLICY_REQUIRED_KINDS["code_audit_visual"]
    assert code_audit_soft_fail_pending(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    ) is True
    # The kind must NOT be silently removed from the required set.
    assert CODE_AUDIT_KIND in needed


def test_soft_fail_wrong_task_not_pending():
    record_audit_attempt("agent-soft", "llm_failed", "aaaaaaaa-1111-2222-3333")
    needed = frozenset({CODE_AUDIT_KIND, "test_run"})
    assert code_audit_soft_fail_pending(
        needed, "agent-soft", "bbbbbbbb-1111-2222-3333-444444444444"
    ) is False


def test_no_attempt_not_pending():
    needed = frozenset({CODE_AUDIT_KIND})
    assert code_audit_soft_fail_pending(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    ) is False


def test_unbound_attempt_pending():
    record_audit_attempt("agent-soft", "no_model", None)
    needed = frozenset({CODE_AUDIT_KIND, "visual_check"})
    assert code_audit_soft_fail_pending(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    ) is True


def test_pending_false_when_kind_not_required():
    record_audit_attempt("agent-soft", "llm_failed", "task-1")
    assert code_audit_soft_fail_pending(
        frozenset({"browse_e2e"}), "agent-soft", "task-1"
    ) is False
    assert code_audit_soft_fail_pending(None, "agent-soft", "task-1") is False


def test_evidence_stamp_survives_ledger_reset_approve_compat():
    """Approve/HTTP re-check still accepts a legacy/waived evidence stamp."""
    record_audit_attempt("agent-soft", "llm_failed", "task-1")
    reset_ledger("agent-soft")
    needed = POLICY_REQUIRED_KINDS["code_audit_visual"]
    evidence = {"code_audit_soft_fail": {"reason": "llm_failed", "task_id": "task-1"}}
    out, dropped = drop_code_audit_kind_if_soft(
        needed, agent_id="agent-soft", task_id="task-1", evidence=evidence
    )
    assert dropped is True
    assert CODE_AUDIT_KIND not in (out or frozenset())
    assert "browse_e2e" in (out or frozenset())
    assert "visual_check" not in (out or frozenset())
