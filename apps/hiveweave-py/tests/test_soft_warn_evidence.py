"""TEST11 evening follow-ups: commit_turn soft-warn + evidence verifiability."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.turn_exit import ExitContext, evaluate_turn_exit
from hiveweave.services.turn_result import TURN_RESULT_SCHEMA_VERSION
from hiveweave.services.turn_session import (
    classify_commit_gate_soft_warn,
    clear_pending_turn_result,
    filter_soft_passed_violations,
    set_pending_turn_result,
)
from hiveweave.services.worktree_review import (
    check_evidence_verifiable,
    extract_acceptance_path_refs,
)
from hiveweave.tools.turn_tools import CommitTurnParams, commit_turn_tool


@pytest.fixture(autouse=True)
def _clear_turn_session():
    clear_pending_turn_result("agent-soft")
    yield
    clear_pending_turn_result("agent-soft")


# ── Soft-warn ledger ────────────────────────────────────────


def test_soft_warn_first_pass_second_hard():
    soft1, hard1 = classify_commit_gate_soft_warn(
        "agent-soft", ["WAIT_WITHOUT_ASK"]
    )
    assert soft1 == ["WAIT_WITHOUT_ASK"]
    assert hard1 == []
    soft2, hard2 = classify_commit_gate_soft_warn(
        "agent-soft", ["WAIT_WITHOUT_ASK"]
    )
    assert soft2 == []
    assert hard2 == ["WAIT_WITHOUT_ASK"]


def test_filter_soft_passed_drops_codes():
    classify_commit_gate_soft_warn("agent-soft", ["UNREPLIED_ASKS"])
    kept = filter_soft_passed_violations(
        "agent-soft", ["UNREPLIED_ASKS", "ASSIGNEE_MUST_SUBMIT"]
    )
    assert kept == ["ASSIGNEE_MUST_SUBMIT"]


@pytest.mark.asyncio
async def test_commit_turn_soft_pass_then_hard_reject():
    """First WAIT_WITHOUT_ASK soft-passes; second hard-rejects."""
    params = CommitTurnParams(
        phase="waiting",
        summary="waiting on peer",
        waiting_on=[{"kind": "agent", "ref": "流火"}],
    )

    with patch(
        "hiveweave.db.meta.get_agent_project_id",
        new_callable=AsyncMock,
        return_value="proj-soft",
    ), patch(
        "hiveweave.services.turn_exit.pre_check_exit_gates",
        new_callable=AsyncMock,
        return_value=["WAIT_WITHOUT_ASK"],
    ):
        r1 = await commit_turn_tool(params, "agent-soft", ".")
        assert r1.success is True
        assert "SOFT WARNING" in (r1.output or "")
        assert r1.extra.get("soft_pass") == ["WAIT_WITHOUT_ASK"]

        # Different payload so we don't hit the same-args ALREADY short-circuit;
        # same gate code → second offense hard-rejects.
        params2 = CommitTurnParams(
            phase="waiting",
            summary="still waiting (retry)",
            waiting_on=[{"kind": "agent", "ref": "流火"}],
        )
        r2 = await commit_turn_tool(params2, "agent-soft", ".")
        assert r2.success is False
        assert "REJECTED" in (r2.error or "")


@pytest.mark.asyncio
async def test_evaluate_turn_exit_respects_soft_pass():
    classify_commit_gate_soft_warn("agent-soft", ["WAIT_WITHOUT_ASK"])
    set_pending_turn_result(
        "agent-soft",
        {
            "schema_version": TURN_RESULT_SCHEMA_VERSION,
            "phase": "waiting",
            "summary": "waiting",
            "waiting_on": [{"kind": "agent", "ref": "流火"}],
            "result": {},
            "extensions": {},
        },
    )
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id="agent-soft",
            project_id="proj-soft",
            tool_calls=[],
            pending_inbox_msgs=[],
            unreplied_asks=[],
            open_task_obligations=[],
            tasks_advanced=set(),
            messaged_refs=set(),
            outbound_ask_refs=set(),
            name_by_id={},
        )
    )
    assert decision.ok is True
    assert "WAIT_WITHOUT_ASK" not in decision.violations


# ── Evidence path extraction + verifiability ─────────────────


def test_extract_acceptance_path_refs_structural_only():
    refs = extract_acceptance_path_refs(
        [
            "src/auth/login.ts must exist",
            "请回复上级确认",  # free text — no path token
            "docs/api.md",
            "README.md",
        ]
    )
    assert "src/auth/login.ts" in refs
    assert "docs/api.md" in refs
    assert "README.md" in refs
    assert all("回复" not in r for r in refs)


@pytest.mark.asyncio
async def test_evidence_verifiable_rejects_missing_files(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "ok.py").write_text("x", encoding="utf-8")

    task = {
        "id": "t1",
        "title": "impl",
        "assignee_id": "exec-1",
        "acceptance_criteria": ["src/ok.py", "src/missing.py"],
        "tags": [],
    }
    evidence = {"files_changed": ["src/ok.py", "src/ghost.py"]}

    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new_callable=AsyncMock,
        return_value=str(project),
    ), patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "hiveweave.services.task.TaskService._is_verify_task",
        return_value=False,
    ):
        err = await check_evidence_verifiable("pid", task, evidence)
    assert err is not None
    assert "ghost.py" in err or "missing.py" in err


@pytest.mark.asyncio
async def test_evidence_verifiable_passes_when_files_exist(tmp_path: Path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.py").write_text("a", encoding="utf-8")

    task = {
        "id": "t1",
        "title": "impl",
        "assignee_id": "exec-1",
        "acceptance_criteria": ["src/a.py"],
        "tags": [],
    }
    evidence = {"files_changed": ["src/a.py"]}

    with patch(
        "hiveweave.services.worktree_review.project_main_workspace",
        new_callable=AsyncMock,
        return_value=str(project),
    ), patch(
        "hiveweave.services.worktree_review.agent_worktree_path",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "hiveweave.services.task.TaskService._is_verify_task",
        return_value=False,
    ):
        err = await check_evidence_verifiable("pid", task, evidence)
    assert err is None
