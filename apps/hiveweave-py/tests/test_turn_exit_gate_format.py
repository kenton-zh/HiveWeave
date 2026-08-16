"""TEST21 M8 — GATE= lines in turn exit hints."""

from __future__ import annotations

from hiveweave.services.turn_exit import _build_gate_hint
from hiveweave.services.turn_result import TurnResult, WaitingOnItem


def test_gate_hint_wait_without_ask_format():
    tr = TurnResult(
        phase="waiting",
        summary="waiting",
        waiting_on=[WaitingOnItem(kind="agent", ref="A002")],
    )
    hint = _build_gate_hint(
        ["WAIT_WITHOUT_ASK"],
        [],
        tr,
        wait_without_ask_refs=["A002"],
    )
    assert "GATE=WAIT_WITHOUT_ASK REF=A002" in hint
    assert "MISSING=ask_agent or send_message to REF" in hint


def test_gate_hint_unreplied_asks_includes_sender_ref():
    hint = _build_gate_hint(
        ["UNREPLIED_ASKS"],
        [{"from_name": "知远", "message": "please reply"}],
        None,
    )
    assert "GATE=UNREPLIED_ASKS REF=知远" in hint
    assert "MISSING=ask_agent or send_message to sender REF" in hint


def test_gate_hint_unreplied_empty_preview_uses_reply_to():
    hint = _build_gate_hint(
        ["UNREPLIED_ASKS"],
        [{
            "from_name": "柚子",
            "from_agent_id": "sender-1",
            "message": "",
            "reply_contract_id": "abcdef1234567890",
        }],
        None,
    )
    assert "GATE=UNREPLIED_ASKS REF=柚子" in hint
    assert "body not in this turn" in hint
    assert "replyTo=abcdef123456" in hint


def test_gate_hint_generic_violation():
    hint = _build_gate_hint(["MISSING_COMMIT_TURN"], [], None)
    assert "GATE=MISSING_COMMIT_TURN REF=-" in hint
    assert "MISSING=commit_turn" in hint
