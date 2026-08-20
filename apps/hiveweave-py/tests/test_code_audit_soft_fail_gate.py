"""code_audit llm_failed must not keep the code_audit submit kind."""

from __future__ import annotations

from hiveweave.services.attestation import POLICY_REQUIRED_KINDS
from hiveweave.services.code_audit import (
    CODE_AUDIT_KIND,
    kinds_after_code_audit_soft_fail,
    record_audit_attempt,
    reset_ledger,
)


def setup_function(_fn=None):
    reset_ledger("agent-soft")
    reset_ledger("agent-other")


def test_llm_failed_drops_code_audit_keeps_browse_e2e():
    record_audit_attempt("agent-soft", "llm_failed", "19fb6fb9-183e-460c-9397")
    needed = POLICY_REQUIRED_KINDS["code_audit_visual"]
    out, dropped = kinds_after_code_audit_soft_fail(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    )
    assert dropped is True
    assert CODE_AUDIT_KIND not in (out or frozenset())
    assert "visual_check" not in (out or frozenset())
    assert "browse_e2e" in (out or frozenset())


def test_soft_fail_wrong_task_still_requires_audit():
    record_audit_attempt("agent-soft", "llm_failed", "aaaaaaaa-1111-2222-3333")
    needed = frozenset({CODE_AUDIT_KIND, "test_run"})
    out, dropped = kinds_after_code_audit_soft_fail(
        needed, "agent-soft", "bbbbbbbb-1111-2222-3333-444444444444"
    )
    assert dropped is False
    assert out == needed


def test_no_attempt_unchanged():
    needed = frozenset({CODE_AUDIT_KIND})
    out, dropped = kinds_after_code_audit_soft_fail(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    )
    assert dropped is False
    assert out == needed


def test_code_audit_only_policy_becomes_empty():
    record_audit_attempt("agent-soft", "no_model", "task-aaaa-bbbb")
    needed = frozenset({CODE_AUDIT_KIND})
    out, dropped = kinds_after_code_audit_soft_fail(
        needed, "agent-soft", "task-aaaa-bbbb-cccc"
    )
    assert dropped is True
    assert out == frozenset()


def test_unbound_attempt_covers_next_submit():
    record_audit_attempt("agent-soft", "llm_failed", None)
    needed = frozenset({CODE_AUDIT_KIND, "visual_check"})
    out, dropped = kinds_after_code_audit_soft_fail(
        needed, "agent-soft", "19fb6fb9-183e-460c-9397-63529c0b152f"
    )
    assert dropped is True
    assert CODE_AUDIT_KIND not in (out or frozenset())


def test_evidence_stamp_survives_ledger_reset():
    from hiveweave.services.code_audit import drop_code_audit_kind_if_soft

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
