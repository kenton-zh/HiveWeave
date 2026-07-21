"""Hire reporting — remind + gate; do not auto-send for the agent."""

from __future__ import annotations

from hiveweave.services.turn_exit import (
    ExitContext,
    evaluate_turn_exit,
    hire_without_report,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)


def test_hire_without_report_detects_missing_message():
    assert hire_without_report(
        [{"function": {"name": "hire_agent", "arguments": "{}"}}]
    )
    assert not hire_without_report(
        [
            {"function": {"name": "hire_agent", "arguments": "{}"}},
            {
                "function": {
                    "name": "send_message",
                    "arguments": '{"recipients":["归零"]}',
                }
            },
        ]
    )
    assert not hire_without_report(
        [{"function": {"name": "read_file", "arguments": "{}"}}]
    )


def test_exit_gate_blocks_hire_without_report():
    clear_pending_turn_result("hr-1")
    set_pending_turn_result(
        "hr-1",
        {
            "phase": "done_slice",
            "summary": "招聘完成",
            "result": {"status": "partial", "artifacts": []},
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id="hr-1",
            project_id="p1",
            tool_calls=[
                {"function": {"name": "hire_agent", "arguments": "{}"}},
                {"function": {"name": "commit_turn", "arguments": "{}"}},
            ],
        )
    )
    assert not decision.ok
    assert "HIRE_UNREPORTED" in decision.violations
    assert decision.should_repair
    clear_pending_turn_result("hr-1")


def test_exit_gate_allows_hire_with_send_message():
    clear_pending_turn_result("hr-1")
    set_pending_turn_result(
        "hr-1",
        {
            "phase": "done_slice",
            "summary": "招聘完成并已通知",
            "result": {"status": "partial", "artifacts": []},
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id="hr-1",
            project_id="p1",
            tool_calls=[
                {"function": {"name": "hire_agent", "arguments": "{}"}},
                {
                    "function": {
                        "name": "send_message",
                        "arguments": '{"recipients":["归零"],"message":"ok"}',
                    }
                },
                {"function": {"name": "commit_turn", "arguments": "{}"}},
            ],
        )
    )
    assert decision.ok
    assert "HIRE_UNREPORTED" not in decision.violations
    clear_pending_turn_result("hr-1")


def test_hire_tool_result_mentions_next_action_not_autosend():
    import hiveweave.tools.org_tools as org_tools

    src = open(org_tools.__file__, encoding="utf-8").read()
    assert "NEXT ACTION" in src
    assert "_platform_notify_hire" not in src
    assert "hire_agent.platform_notify" not in src
