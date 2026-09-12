"""P0-3 fail-loud (TEST_DSH_38) + F1 软失败服务端复核。

Submit keeps code_audit required and the tool path rejects with an
explicit-waive hint. The approve/HTTP re-check (``drop_code_audit_kind_if_soft``)
now drops the kind **only** on a server-verified ``tool_attestations`` fact row
(``code_audit_soft_fail`` / ``attestation_impossible``) — never on the
client-writable ``evidence`` stamp (F1 security fix).
"""

from __future__ import annotations

import asyncio

import pytest

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


def test_soft_fail_evidence_stamp_alone_no_longer_drops_kind():
    """F1：只有客户端可写的 evidence 盖章、库里**没有**平台持久事实行 ⇒ 不剔除
    code_audit（approve/HTTP 门仍然拦）。

    **本用例原断言是 ``dropped is True``（旧行为），已按 F1 修复适配**：那正是
    绕过路径——approve/HTTP 侧的 ``evidence`` 是客户端原文，自带
    ``{"code_audit_soft_fail": {"reason": "llm_failed"}}`` 即可零 attestation
    过门。现改为服务端复核持久事实行（``resolve_soft_fail_kind``）；此处无该
    project 的库，复核必然返回 None ⇒ 保持原门禁。正向放行由
    ``test_attestation_impossible.py::test_verified_soft_fail_fact_drops_kind``
    钉住（库里确有平台事实行才剔除）。
    """
    record_audit_attempt("agent-soft", "llm_failed", "task-1")
    reset_ledger("agent-soft")
    needed = POLICY_REQUIRED_KINDS["code_audit_visual"]
    evidence = {"code_audit_soft_fail": {"reason": "llm_failed", "task_id": "task-1"}}
    out, dropped = asyncio.run(
        drop_code_audit_kind_if_soft(
            needed, "proj-soft", agent_id="agent-soft", task_id="task-1",
            evidence=evidence,
        )
    )
    assert dropped is False
    assert CODE_AUDIT_KIND in (out or frozenset())
    # 其余必需 kind 原样保留（只可能剔 code_audit，且此处不该剔任何 kind）
    assert "browse_e2e" in (out or frozenset())


@pytest.mark.parametrize("reason", ["llm_failed", "no_model", "no_callback"])
def test_forged_soft_fail_evidence_all_reasons_still_blocked(reason):
    """F1 核心回归守卫：「伪造即拦」——evidence 三值全中的伪造盖章，但库里
    **没有**平台持久事实行 ⇒ ``drop_code_audit_kind_if_soft`` 仍不剔除
    code_audit。

    反向对照（唯一差别 = 库里有平台事实行）见
    ``test_verified_soft_fail_fact_drops_kind``。
    """
    forged = {"code_audit_soft_fail": {"reason": reason}}
    out, dropped = asyncio.run(
        drop_code_audit_kind_if_soft(
            frozenset({CODE_AUDIT_KIND}),
            "proj-forged-soft",
            task_id="task-forged",
            evidence=forged,
        )
    )
    assert dropped is False
    assert CODE_AUDIT_KIND in (out or frozenset())


def test_forged_impossible_evidence_does_not_drop_kind():
    """安全回归守卫：evidence 伪造 attestation_impossible，但库里没有平台
    事实行 ⇒ ``drop_code_audit_kind_if_soft`` 不剔除 code_audit。

    修复前该函数只看 evidence，任何调用方自带
    ``{"attestation_impossible":{"need":"code_audit"}}`` 即可跳门。现改由漏斗
    内部服务端复核（``resolve_impossible_kind`` 读 tool_attestations 事实行）：
    查不到事实 ⇒ 不放行。此处无该 project 的库，复核必然返回 None。
    """
    forged = {"attestation_impossible": {"need": "code_audit"}}
    out, dropped = asyncio.run(
        drop_code_audit_kind_if_soft(
            frozenset({CODE_AUDIT_KIND, "test_run"}),
            "proj-does-not-exist",
            task_id="task-forged",
            evidence=forged,
        )
    )
    assert dropped is False
    assert CODE_AUDIT_KIND in (out or frozenset())
