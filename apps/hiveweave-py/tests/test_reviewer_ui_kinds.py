"""Reviewer attestation kinds — UI OR vs code-task test_run-only."""

from hiveweave.services.attestation import (
    BROWSE_E2E_KIND,
    REVIEWER_KIND,
    VISUAL_CHECK_KIND,
    required_attestation_kinds,
    reviewer_required_kinds,
)


def test_reviewer_ui_browser_e2e_includes_browse_and_visual():
    kinds = reviewer_required_kinds("ui_browser_e2e")
    assert kinds is not None
    assert kinds == frozenset(
        {REVIEWER_KIND, BROWSE_E2E_KIND, VISUAL_CHECK_KIND}
    )
    assert "test_run" in kinds
    assert "browse_e2e" in kinds
    assert "visual_check" in kinds


def test_reviewer_generic_tests_is_test_run_only():
    kinds = reviewer_required_kinds("generic_tests")
    assert kinds == frozenset({REVIEWER_KIND})
    assert kinds == frozenset({"test_run"})


def test_reviewer_coordinator_review_is_test_run_only():
    assert reviewer_required_kinds("coordinator_review") == frozenset(
        {REVIEWER_KIND}
    )


def test_submit_ui_kinds_remain_and_of_browse_and_visual():
    """Submit-side ui_browser_e2e stays AND (visual_check + browse_e2e)."""
    needed = required_attestation_kinds("ui_browser_e2e")
    assert needed == frozenset({VISUAL_CHECK_KIND, BROWSE_E2E_KIND})
    assert REVIEWER_KIND not in (needed or frozenset())


def test_worktree_nested_under_project_is_not_main(tmp_path):
    """VERIFY UI gate must use equality — under-or-same would never fire."""
    from hiveweave.tools.bash import _is_same_workspace, _is_under_or_same

    main = tmp_path / "proj"
    wt = main / ".hiveweave" / "worktrees" / "sid"
    wt.mkdir(parents=True)
    assert _is_under_or_same(str(wt), str(main)) is True
    assert _is_same_workspace(str(wt), str(main)) is False
    assert _is_same_workspace(str(main), str(main)) is True
    assert _is_same_workspace(str(main), str(main.resolve())) is True
