"""telemetry — turn_exit_by_violation / turn_exit_by_action counters."""

from hiveweave.services.telemetry import telemetry


def test_turn_exit_gate_counters():
    telemetry.reset_counters_for_tests()
    telemetry.turn_exit_gate(
        "a1", ["ASSIGNEE_MUST_SUBMIT", "UNREPLIED_ASKS"], "repair", gate_round=1
    )
    telemetry.turn_exit_gate("a1", [], "ok")
    snap = telemetry.snapshot_counters()
    assert snap["turn_exit_by_action"]["repair"] == 1
    assert snap["turn_exit_by_action"]["ok"] == 1
    assert snap["turn_exit_by_violation"]["ASSIGNEE_MUST_SUBMIT"] == 1
    assert snap["turn_exit_by_violation"]["UNREPLIED_ASKS"] == 1
