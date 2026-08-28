"""P0-6: an empty audit completion must log, and must keep the soft-fail reason.

Before this fix the ``if not text`` branch in ``_invoke_audit_llm`` returned
silently — TEST_DSH_35's 6/6 ``llm_failed`` all came through it with an empty
``error`` field, making the failure invisible to operators.
"""

from __future__ import annotations

from hiveweave.services.code_audit import _invoke_audit_llm


async def test_empty_text_is_soft_fail_and_reason_stays_llm_failed() -> None:
    """``_SOFT_FAIL_ATTEMPT_REASONS`` is a whitelist that drives the
    non-blocking wording; renaming the reason would block submit instead."""
    calls: list[tuple[str, str]] = []

    async def _call_llm(system: str, user: str) -> str:
        calls.append((system, user))
        return ""

    text, meta = await _invoke_audit_llm(
        "proj-1",
        "agent-1",
        "system prompt",
        "user prompt",
        call_llm=_call_llm,
        oneshot_llm=None,
    )

    assert calls == [("system prompt", "user prompt")]
    assert text is None
    assert meta["audited"] is False
    assert meta["reason"] == "llm_failed"


async def test_empty_text_branch_does_not_raise_on_scope() -> None:
    """Guards the log call itself: only project_id / agent_id / chosen / source
    are in scope here — `task_id` is not (it is not a parameter)."""
    async def _call_llm(system: str, user: str) -> str:
        return ""

    text, meta = await _invoke_audit_llm(
        "proj-2",
        "agent-2",
        "s",
        "u",
        call_llm=_call_llm,
        oneshot_llm=None,
    )

    assert text is None
    assert meta["reason"] == "llm_failed"
    # The soft-fail branch carries `audited`; the success branch does not.
    assert meta["audited"] is False


async def test_non_empty_text_passes_through() -> None:
    async def _call_llm(system: str, user: str) -> str:
        return "VERDICT: PASS\nno issues"

    text, meta = await _invoke_audit_llm(
        "proj-3",
        "agent-3",
        "s",
        "u",
        call_llm=_call_llm,
        oneshot_llm=None,
    )

    assert text == "VERDICT: PASS\nno issues"
    assert "reason" not in meta
    assert meta["audit_model_source"] == "own"


async def test_whitespace_only_text_is_currently_treated_as_content() -> None:
    """Documents current behaviour, not desired behaviour.

    ``if not text`` does not strip, so a whitespace-only completion is treated
    as real content. Left as-is deliberately: tightening it changes audit
    outcomes and belongs in its own change. Tracked as a follow-up.
    """
    async def _call_llm(system: str, user: str) -> str:
        return "   "

    text, meta = await _invoke_audit_llm(
        "proj-4",
        "agent-4",
        "s",
        "u",
        call_llm=_call_llm,
        oneshot_llm=None,
    )

    assert text == "   "
    assert "reason" not in meta
