"""TEST18 audit fixes — NEW-1 / P0-1 / P0-2 / P0-4 / P0-5."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── NEW-1: phase must be initialized before park/exhaust tail ───────────────


def test_completion_initializes_phase_before_gate_tail():
    """Source guard: phase = None sits next to gate_retrigger_hint init."""
    from pathlib import Path

    here = Path(__file__).resolve()
    completion = here.parents[1] / "src" / "hiveweave" / "agents" / "completion.py"
    text = completion.read_text(encoding="utf-8")
    assert "phase: str | None = None" in text
    # Init must be adjacent to gate_retrigger_hint (public-tail safety)
    hint_pos = text.index("gate_retrigger_hint: str | None = None")
    phase_pos = text.index("phase: str | None = None")
    assert abs(phase_pos - hint_pos) < 400
    # Productive-continue tail still references phase
    assert "_arm_productive_continue" in text
    assert 'phase == "in_progress"' in text


# ── P0-1: review escalate only when task awaiting review ────────────────────


@pytest.mark.asyncio
async def test_scan_overdue_skips_review_while_running():
    from hiveweave.services.obligation import ObligationLedger

    overdue = [
        {
            "id": "ob1",
            "owner_agent_id": "reviewer",
            "obligation_type": "review",
            "task_id": "task-1",
            "escalation_count": 0,
            "escalated_at": 0,
            "deadline": 1,
        }
    ]
    notify = AsyncMock()
    with (
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(return_value=overdue),
        ),
        patch("hiveweave.services.obligation._execute", AsyncMock()),
        patch.object(
            ObligationLedger,
            "_task_status",
            AsyncMock(return_value="running"),
        ),
        patch.object(
            ObligationLedger,
            "_find_escalation_target",
            AsyncMock(return_value="ceo"),
        ),
        patch.object(ObligationLedger, "_notify_escalation", notify),
    ):
        out = await ObligationLedger().scan_overdue("p1")

    assert out == []
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_overdue_escalates_review_when_submitted():
    from hiveweave.services.obligation import ObligationLedger

    overdue = [
        {
            "id": "ob1",
            "owner_agent_id": "reviewer",
            "obligation_type": "review",
            "task_id": "task-1",
            "escalation_count": 0,
            "escalated_at": 0,
            "deadline": 1,
        }
    ]
    notify = AsyncMock()
    execute = AsyncMock()
    with (
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(return_value=overdue),
        ),
        patch("hiveweave.services.obligation._execute", execute),
        patch.object(
            ObligationLedger,
            "_task_status",
            AsyncMock(return_value="submitted"),
        ),
        patch.object(
            ObligationLedger,
            "_find_escalation_target",
            AsyncMock(return_value="ceo"),
        ),
        patch.object(ObligationLedger, "_notify_escalation", notify),
    ):
        out = await ObligationLedger().scan_overdue("p1")

    assert len(out) == 1
    notify.assert_awaited_once()
    # task_status passed through to notify
    assert notify.await_args.kwargs.get("task_status") == "submitted"


@pytest.mark.asyncio
async def test_review_create_submit_resets_deadline():
    """Dispatch parks the clock; submit activates + resets deadline."""
    import time

    from hiveweave.services.obligation import (
        REVIEW_DEADLINE_MS,
        ObligationLedger,
    )

    execute = AsyncMock()
    existing = [{"id": "ob-old", "owner_agent_id": "dispatcher"}]
    before = int(time.time() * 1000)
    with (
        patch(
            "hiveweave.services.obligation._normalize_task_id",
            AsyncMock(return_value="task-full"),
        ),
        patch(
            "hiveweave.services.obligation._query",
            AsyncMock(return_value=existing),
        ),
        patch("hiveweave.services.obligation._execute", execute),
    ):
        ob_id = await ObligationLedger().create(
            "p1",
            "pinned-reviewer",
            "review",
            task_id="task-full",
            context={"source": "submit", "activated": True},
        )
    after = int(time.time() * 1000)

    assert ob_id == "ob-old"
    assert execute.await_count == 1
    args = execute.await_args.args
    assert "deadline" in args[1]
    deadline_val = args[2][2]
    # Active clock ≈ now + 15min — NOT parked 1-year offset
    assert before + REVIEW_DEADLINE_MS - 2000 <= deadline_val <= after + REVIEW_DEADLINE_MS + 2000
    assert args[2][0] == "pinned-reviewer"  # retargeted owner


# ── P0-2: prefer fam=qa over same-parent executor ───────────────────────────


@pytest.mark.asyncio
async def test_find_independent_qa_prefers_qa_family_over_same_parent():
    from hiveweave.tools.tasks.verify_spawn import _find_independent_qa

    original = "impl-1"
    same_parent_exec = {
        "id": "lucas",
        "parent_id": "tide",
        "status": "active",
        "role": "平台工程师",
        "permission_type": "executor",
    }
    other_qa = {
        "id": "qingniao",
        "parent_id": "muyu",
        "status": "active",
        "role": "游戏测试工程师",
        "permission_type": "executor",
    }
    original_row = {
        "id": original,
        "parent_id": "tide",
        "status": "active",
        "role": "平台工程师",
        "permission_type": "executor",
    }

    def _family(agent: dict) -> str:
        role = (agent.get("role") or "")
        if "测试" in role:
            return "qa"
        return "executor"

    with (
        patch(
            "hiveweave.services.org.OrgService.list_agents",
            AsyncMock(
                return_value=[original_row, same_parent_exec, other_qa]
            ),
        ),
        patch(
            "hiveweave.services.policy.infer_role_family",
            side_effect=_family,
        ),
        patch(
            "hiveweave.services.policy.has_capability",
            return_value=True,
        ),
    ):
        picked = await _find_independent_qa(
            "p1",
            original_assignee=original,
            required_capabilities=["test_run", "source_read"],
        )

    assert picked == "qingniao"


# ── P0-4: REVIEW helper gets unbound tip ────────────────────────────────────


@pytest.mark.asyncio
async def test_attestation_bind_tips_review_helper():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    open_task = {
        "id": "t-review",
        "status": "submitted",
        "creator_id": "tide",
        "reviewer_id": "tide",
        "title": "render module",
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(return_value=[open_task]),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            AsyncMock(
                return_value={
                    "id": "muyu",
                    "role": "架构师",
                    "permission_type": "coordinator",
                }
            ),
        ),
        patch(
            "hiveweave.services.policy.has_capability",
            return_value=True,
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "muyu", None
        )

    assert tid is None
    assert "taskId" in note
    assert "t-review" in note
    assert "UNBOUND" in note


@pytest.mark.asyncio
async def test_attestation_bind_uses_pinned_reviewer():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    open_task = {
        "id": "t-pinned",
        "status": "reviewing",
        "creator_id": "tide",
        "reviewer_id": "muyu",
        "title": "ui",
    }
    with patch(
        "hiveweave.services.task.TaskService.list_tasks",
        AsyncMock(return_value=[open_task]),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "muyu", None
        )

    assert tid == "t-pinned"
    assert note == ""


# ── P0-5: account rate limit project throttle ───────────────────────────────


def test_account_rate_limit_detection_and_project_arm():
    from hiveweave.agents.helpers.rate_limit import (
        arm_project_rate_limit,
        is_account_rate_limit,
        project_rate_limit_remaining,
        _project_rate_limit_until,
    )

    assert is_account_rate_limit(
        Exception("AccountRateLimitExceeded: too many")
    )
    assert not is_account_rate_limit(Exception("temporary 503"))

    _project_rate_limit_until.clear()
    left = arm_project_rate_limit("proj-x", 30.0)
    assert left > 25
    assert project_rate_limit_remaining("proj-x") > 0
    _project_rate_limit_until.clear()
    assert project_rate_limit_remaining("proj-x") == 0


def test_resume_cooldown_ignores_circuit_open():
    """Circuit OPEN must not permanently block autonomous resume."""
    import time

    from hiveweave.agents.agent import Agent

    agent = object.__new__(Agent)
    agent._resume_cooldown_until = 0.0
    agent.project_id = "p-circuit"
    with (
        patch(
            "hiveweave.agents.helpers.rate_limit.project_rate_limit_remaining",
            return_value=0.0,
        ),
        patch(
            "hiveweave.agents.helpers.rate_limit.circuit_open_for_agent",
            return_value=True,
        ),
    ):
        assert agent._in_resume_cooldown() is False

    agent._resume_cooldown_until = time.monotonic() + 60
    assert agent._in_resume_cooldown() is True


# ── P0-3: VERIFY force main cwd + refuse worktree stamp ─────────────────────


@pytest.mark.asyncio
async def test_verify_test_workspace_forced_to_main():
    from hiveweave.tools.bash import _resolve_verify_test_workspace

    verify_task = {
        "id": "v-1",
        "title": "[VERIFY] parent",
        "status": "running",
        "assignee_id": "qa-1",
    }
    with (
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("v-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=verify_task),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
        patch(
            "hiveweave.services.attestation.is_test_command",
            return_value=True,
        ),
    ):
        exec_ws, note, tid = await _resolve_verify_test_workspace(
            "p1",
            "qa-1",
            "v-1",
            "npm test",
            "D:/proj/.hiveweave/worktrees/A009",
        )

    assert tid == "v-1"
    assert exec_ws.replace("\\", "/") == "D:/proj/main"
    assert "forced cwd=main" in note


@pytest.mark.asyncio
async def test_verify_attestation_rejects_worktree_exec():
    from hiveweave.tools.bash import _issue_test_run_attestation

    verify_task = {
        "id": "v-1",
        "title": "[VERIFY] parent",
        "status": "running",
    }
    create = AsyncMock(return_value="att-1")
    with (
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("v-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=verify_task),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
        patch(
            "hiveweave.services.attestation.is_test_command",
            return_value=True,
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            create,
        ),
    ):
        note = await _issue_test_run_attestation(
            project_id="p1",
            agent_id="qa-1",
            command="npm test",
            workspace="D:/proj/.hiveweave/worktrees/A009",
            stdout="ok",
            exit_code=0,
            task_id="v-1",
        )

    assert "VERIFY ATTEST REJECTED" in note
    create.assert_not_awaited()


# ── P0-3 re-audit: real bind (no mock) for VERIFY created / multi ───────────


@pytest.mark.asyncio
async def test_bind_sole_verify_created_without_task_id():
    """VERIFY skips assign=claim — sole created VERIFY must still bind."""
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    verify = {
        "id": "v-created",
        "title": "VERIFY: parent module",
        "status": "created",
        "assignee_id": "qa-1",
        "tags": ["verify"],
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[
                [],  # all_tasks for reviewer path
                [verify],  # mine as assignee
            ]),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            side_effect=lambda t: "verify" in (t.get("tags") or [])
            or str(t.get("title") or "").startswith("VERIFY:"),
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "qa-1", None
        )

    assert tid == "v-created"
    assert note == ""


@pytest.mark.asyncio
async def test_bind_prefers_verify_over_other_running():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    verify = {
        "id": "v-1",
        "title": "VERIFY: parent",
        "status": "created",
        "assignee_id": "qa-1",
        "tags": ["verify"],
    }
    other = {
        "id": "t-other",
        "title": "side chore",
        "status": "running",
        "assignee_id": "qa-1",
        "tags": [],
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[[], [verify, other]]),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            side_effect=lambda t: "verify" in (t.get("tags") or []),
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "qa-1", None
        )

    assert tid == "v-1"
    assert note == ""


@pytest.mark.asyncio
async def test_bind_multi_verify_tips_task_ids():
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    v1 = {
        "id": "v-1",
        "title": "VERIFY: a",
        "status": "created",
        "tags": ["verify"],
    }
    v2 = {
        "id": "v-2",
        "title": "VERIFY: b",
        "status": "running",
        "tags": ["verify"],
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[[], [v1, v2]]),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1", "qa-1", None
        )

    assert tid is None
    assert "v-1" in note and "v-2" in note
    assert "VERIFY" in note


@pytest.mark.asyncio
async def test_multi_verify_binds_from_command_text():
    """TEST18 P0-2: command-text taskId extraction rescues multi-open VERIFY."""
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    v1 = {
        "id": "69e1d14a-498e-44b5-962f-3374da5fd401",
        "title": "VERIFY: a",
        "status": "created",
        "tags": ["verify"],
    }
    v2 = {
        "id": "a85959bd-aa21-4256-b81b-ad0b8c4a484a",
        "title": "VERIFY: b",
        "status": "running",
        "tags": ["verify"],
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[[], [v1, v2]]),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1",
            "qa-1",
            None,
            command="cd /w && npx vitest run taskId=69e1d14a-498e-44b5-962f-3374da5fd401",
        )

    assert tid == v1["id"]
    assert "command text" in note


@pytest.mark.asyncio
async def test_multi_verify_command_text_ignores_unknown():
    """Extracted taskId not in open VERIFY candidates → still refuse."""
    from hiveweave.tools.bash import _resolve_test_attestation_task_id

    v1 = {
        "id": "69e1d14a-498e-44b5-962f-3374da5fd401",
        "title": "VERIFY: a",
        "status": "created",
        "tags": ["verify"],
    }
    v2 = {
        "id": "a85959bd-aa21-4256-b81b-ad0b8c4a484a",
        "title": "VERIFY: b",
        "status": "running",
        "tags": ["verify"],
    }
    with (
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[[], [v1, v2]]),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
    ):
        tid, note = await _resolve_test_attestation_task_id(
            "p1",
            "qa-1",
            None,
            command="npx vitest run taskId=deadbeef-dead-beef-dead-beefdeadbeef",
        )

    assert tid is None
    assert "UNBOUND" in note
    assert "deadbeef" not in note  # candidates listed, not the garbage value


@pytest.mark.asyncio
async def test_force_main_via_real_bind_created_verify():
    """End-to-end: created VERIFY + no taskId → force main (no bind mock)."""
    from hiveweave.tools.bash import _resolve_verify_test_workspace

    verify = {
        "id": "v-created",
        "title": "VERIFY: parent",
        "status": "created",
        "assignee_id": "qa-1",
        "tags": ["verify"],
    }
    with (
        patch(
            "hiveweave.services.attestation.is_test_command",
            return_value=True,
        ),
        patch(
            "hiveweave.services.task.TaskService.list_tasks",
            AsyncMock(side_effect=[[], [verify]]),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=verify),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            side_effect=lambda t: "verify" in (t.get("tags") or []),
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
    ):
        exec_ws, note, tid = await _resolve_verify_test_workspace(
            "p1",
            "qa-1",
            None,  # no explicit taskId
            "npm test",
            "D:/proj/.hiveweave/worktrees/A013",
        )

    assert tid == "v-created"
    assert exec_ws.replace("\\", "/") == "D:/proj/main"
    assert "forced cwd=main" in note


@pytest.mark.asyncio
async def test_attestation_accepts_exec_cwd_under_main():
    from hiveweave.tools.bash import _issue_test_run_attestation

    verify_task = {
        "id": "v-1",
        "title": "VERIFY: parent",
        "status": "running",
        "tags": ["verify"],
    }
    create = AsyncMock(return_value="att-ok")
    with (
        patch(
            "hiveweave.tools.bash._resolve_test_attestation_task_id",
            AsyncMock(return_value=("v-1", "")),
        ),
        patch(
            "hiveweave.services.task.TaskService.get_task",
            AsyncMock(return_value=verify_task),
        ),
        patch(
            "hiveweave.services.task.TaskService._is_verify_task",
            return_value=True,
        ),
        patch(
            "hiveweave.services.worktree_review.project_main_workspace",
            AsyncMock(return_value="D:/proj/main"),
        ),
        patch(
            "hiveweave.services.attestation.is_test_command",
            return_value=True,
        ),
        patch(
            "hiveweave.services.attestation.attestation_service.create",
            create,
        ),
        patch(
            "hiveweave.services.task.TaskService.emit_task_event",
            AsyncMock(),
        ),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as proc_factory,
    ):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"abc123deadbeef\n", b"")
        )
        proc_factory.return_value = proc
        note = await _issue_test_run_attestation(
            project_id="p1",
            agent_id="qa-1",
            command="npm test",
            workspace="D:/proj/main",
            stdout="ok",
            exit_code=0,
            task_id="v-1",
            exec_cwd="D:/proj/main/apps/web",
        )

    assert "REJECTED" not in note
    assert "stamped_from=main" in note
    create.assert_awaited_once()


def test_completion_phase_init_not_duplicated():
    from pathlib import Path

    here = Path(__file__).resolve()
    completion = here.parents[1] / "src" / "hiveweave" / "agents" / "completion.py"
    text = completion.read_text(encoding="utf-8")
    assert text.count("phase: str | None = None") == 1


# ── NEW-9: waive tip names concrete REVIEW holders ──────────────────────────


@pytest.mark.asyncio
async def test_post_waive_tip_lists_named_reviewers():
    from hiveweave.tools.tasks.waive import _format_post_waive_approve_tip

    async def _get_agent(aid: str):
        return {
            "guiling": {
                "id": "guiling",
                "name": "归零",
                "short_id": "A001",
                "role": "CEO",
            },
            "tide": {
                "id": "tide",
                "name": "潮汐",
                "short_id": "A007",
                "role": "游戏逻辑架构师",
            },
        }.get(aid)

    with (
        patch(
            "hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
            AsyncMock(return_value=False),
        ),
        patch(
            "hiveweave.services.unblock_soft.list_review_capable_agent_ids",
            AsyncMock(return_value=["guiling", "tide"]),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            AsyncMock(side_effect=_get_agent),
        ),
    ):
        tip = await _format_post_waive_approve_tip(
            "p1",
            waived_by="muyu",
            assignee_id="qingniao",
        )

    assert "归零" in tip and "A001" in tip
    assert "潮汐" in tip and "A007" in tip
    assert "CANNOT approve" in tip
    assert "fake rework" in tip


@pytest.mark.asyncio
async def test_post_waive_tip_sole_reviewer_may_self_approve():
    from hiveweave.tools.tasks.waive import _format_post_waive_approve_tip

    with patch(
        "hiveweave.services.unblock_soft.is_small_team_sole_reviewer",
        AsyncMock(return_value=True),
    ):
        tip = await _format_post_waive_approve_tip(
            "p1",
            waived_by="muyu",
            assignee_id="qingniao",
        )

    assert "MAY review_task(approve) yourself" in tip
    assert "sole REVIEW" in tip
