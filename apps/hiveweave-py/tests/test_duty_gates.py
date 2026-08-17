"""Duty gates: submitGate, depends_on blocked, no per-merge VERIFY, sqlite lock."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from hiveweave.db import project as project_db
from hiveweave.db.project import _execute_write_with_retry, is_sqlite_lock_error
from hiveweave.services import task as task_module
from hiveweave.services.attestation import (
    BROWSE_E2E_KIND,
    CODE_AUDIT_KIND,
    POLICY_REQUIRED_KINDS,
    REVIEWER_KIND,
    REVIEWER_REQUIRED_KINDS,
    VISUAL_CHECK_KIND,
    ledger_policy_id,
    policy_from_submit_gate,
    format_attestation_mismatch_hint,
)
from hiveweave.services.task import TaskService
from hiveweave.tools.tasks.create import CreateTaskParams, create_task_tool
from hiveweave.tools.tasks.dispatch import DispatchTaskParams, dispatch_task_tool

PROJECT_ID = "test-duty-gates"
COORD = "coord-1"
EXEC = "exec-1"
QA = "qa-1"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {
                "project_id": PROJECT_ID,
                "workspace_path": workspace_path,
            }

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def test_policy_from_submit_gate_maps_and_rejects():
    assert policy_from_submit_gate("docs") == "docs_only"
    assert policy_from_submit_gate("unit") == "generic_tests"
    assert policy_from_submit_gate("module_visual") == "ui_browser_e2e"
    assert policy_from_submit_gate("code_audit+unit") == "code_audit_unit"
    with pytest.raises(ValueError, match="required"):
        policy_from_submit_gate(None)
    with pytest.raises(ValueError, match="unknown"):
        policy_from_submit_gate("e2e-everything")


def test_llm_schemas_expose_submit_gate():
    from hiveweave.tools.executor import get_tool_schema_for_llm

    created = get_tool_schema_for_llm("create_task")
    assert "submitGate" in created["properties"]
    assert "submitGate" in created["required"]
    assert "milestoneVerify" in created["properties"]
    dispatched = get_tool_schema_for_llm("dispatch_task")
    assert "submitGate" in dispatched["properties"]
    assert "submitGate" in dispatched["required"]
    assert "milestoneVerify" in dispatched["properties"]
    assert "dependsOn" in dispatched["properties"]
    merge = get_tool_schema_for_llm("git_worktree_merge")
    assert "Does not auto-spawn VERIFY" in (merge.get("description") or "")
    review = get_tool_schema_for_llm("review_task")
    assert "submitGate" in (review.get("description") or "")


def test_create_task_params_require_submit_gate():
    with pytest.raises(ValidationError):
        CreateTaskParams(title="x", description="y")


def test_module_visual_reviewer_accepts_browse_without_test_run():
    """Reviewer kinds are OR — browse/visual unlocks module_visual, not test_run-only."""
    kinds = REVIEWER_REQUIRED_KINDS["ui_browser_e2e"]
    assert kinds is not None
    assert BROWSE_E2E_KIND in kinds
    assert VISUAL_CHECK_KIND in kinds
    submit = POLICY_REQUIRED_KINDS["ui_browser_e2e"]
    assert submit is not None
    assert REVIEWER_KIND not in submit
    unit_reviewer = REVIEWER_REQUIRED_KINDS["generic_tests"]
    assert unit_reviewer == frozenset({REVIEWER_KIND})
    unit_submit = POLICY_REQUIRED_KINDS["generic_tests"]
    assert unit_submit == frozenset({REVIEWER_KIND})
    audit_unit_submit = POLICY_REQUIRED_KINDS["code_audit_unit"]
    assert audit_unit_submit == frozenset({CODE_AUDIT_KIND, REVIEWER_KIND})


def test_sqlite_lock_error_matches_clone_timeout_wording():
    assert is_sqlite_lock_error(sqlite3.OperationalError("database is locked"))
    assert is_sqlite_lock_error(sqlite3.OperationalError("database is busy"))
    assert is_sqlite_lock_error(
        sqlite3.OperationalError("write operation timed out")
    )
    assert not is_sqlite_lock_error(sqlite3.OperationalError("no such table"))
    assert not is_sqlite_lock_error(ValueError("locked"))


@pytest.mark.asyncio
async def test_execute_write_retries_then_succeeds():
    class FakeConn:
        def __init__(self) -> None:
            self.n = 0

        async def execute(self, sql: str, params=None):
            self.n += 1
            if self.n < 3:
                raise sqlite3.OperationalError("database is locked")

        async def commit(self):
            return None

        async def rollback(self):
            return None

    conn = FakeConn()
    await _execute_write_with_retry(conn, "UPDATE t SET x=1", [])
    assert conn.n == 3


@pytest.mark.asyncio
async def test_depends_on_unmet_starts_blocked(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    child = await ts.create_task(
        pid,
        "Child",
        "d",
        COORD,
        assignee_id=EXEC,
        depends_on=[parent],
        policy_id="generic_tests",
    )
    t = await ts.get_task(pid, child)
    assert t["status"] == "blocked"
    assert t["assignee_id"] == EXEC
    assert t.get("claimed_at") is None
    assert (t.get("wait_kind") or "") == "dependency"


@pytest.mark.asyncio
async def test_verify_title_not_auto_blocked_on_depends_on(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    vid = await ts.create_task(
        pid,
        "VERIFY: milestone",
        "qa on main",
        COORD,
        assignee_id=QA,
        depends_on=[parent],
        source="system",
        policy_id="ui_browser_e2e",
    )
    t = await ts.get_task(pid, vid)
    assert t["status"] == "created"
    assert t["assignee_id"] == QA


@pytest.mark.asyncio
async def test_agent_cannot_forge_verify_title(env):
    ts = TaskService()
    with pytest.raises(ValueError, match="milestoneVerify"):
        await ts.create_task(
            env["project_id"],
            "VERIFY: sneaky",
            "d",
            COORD,
            source="agent",
        )


@pytest.mark.asyncio
async def test_dispatch_tool_skips_wake_when_blocked():
    trigger = AsyncMock()
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.tools.helpers.resolve_agent_id",
            AsyncMock(return_value=EXEC),
        ),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_similar_open_task",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_structured_open_dup",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.dispatch.DispatchService.dispatch_task",
            AsyncMock(
                return_value={
                    "success": True,
                    "task_id": "t-blocked",
                    "to_agent_id": EXEC,
                    "blocked": True,
                }
            ),
        ),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
    ):
        result = await dispatch_task_tool(
            DispatchTaskParams(
                target="A004",
                task="queued work",
                submitGate="unit",
                dependsOn=["parent-1"],
            ),
            COORD,
            "/ws",
        )
    assert result.success is True
    assert "not woken" in (result.output or "")
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_new_task_rejects_missing_submit_gate():
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.tools.helpers.resolve_agent_id",
            AsyncMock(return_value=EXEC),
        ),
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_similar_open_task",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_structured_open_dup",
            AsyncMock(return_value=None),
        ),
    ):
        result = await dispatch_task_tool(
            DispatchTaskParams(target="A004", task="no gate"),
            COORD,
            "/ws",
        )
    assert result.success is False
    assert "submitGate" in (result.error or result.output or "")


@pytest.mark.asyncio
async def test_merge_does_not_auto_spawn_verify():
    approved = {
        "id": "parent-1",
        "title": "Ship UI",
        "status": "approved",
        "assignee_id": EXEC,
        "parent_task_id": None,
        "created_at": 1,
    }
    spawn = AsyncMock(return_value="verify-should-not")
    with (
        patch.object(
            TaskService, "list_tasks", AsyncMock(return_value=[approved])
        ),
        patch(
            "hiveweave.services.worktree_review.select_tasks_for_merged_work",
            return_value=[approved],
        ),
        patch(
            "hiveweave.tools.tasks.verify_merge._stamp_merge_fact_on_parent_tasks",
            AsyncMock(),
        ),
        patch(
            "hiveweave.tools.tasks.verify_spawn._spawn_post_approve_verify_task",
            spawn,
        ),
        patch(
            "hiveweave.tools.tasks.verify_merge._nudge_one_verify_task",
            AsyncMock(return_value=False),
        ),
    ):
        from hiveweave.tools.tasks.verify_merge import (
            nudge_verify_tasks_after_merge,
        )

        n = await nudge_verify_tasks_after_merge(
            PROJECT_ID, COORD, merged_agent_id=EXEC
        )
    spawn.assert_not_awaited()
    assert n == 0


@pytest.mark.asyncio
async def test_create_task_tool_wires_policy_id():
    create = AsyncMock(return_value="task-1")
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_structured_open_dup",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.task.TaskService.find_similar_open_task",
            AsyncMock(return_value=None),
        ),
        patch.object(TaskService, "create_task", create),
        patch.object(
            TaskService,
            "get_task",
            AsyncMock(return_value={"status": "created"}),
        ),
    ):
        result = await create_task_tool(
            CreateTaskParams(
                title="docs slice", description="write spec", submitGate="docs"
            ),
            COORD,
            "/ws",
        )
    assert result.success
    assert create.await_args.kwargs["policy_id"] == "docs_only"


def test_ledger_policy_id_ignores_hung_evidence():
    task = {
        "policy_id": "code_audit",
        "title": "x",
        "tags": [],
        "description": "",
        "evidence": {"policy_id": "coordinator_review"},
    }
    assert ledger_policy_id(task) == "code_audit"


@pytest.mark.asyncio
async def test_unblock_refuses_unmet_depends_on(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    child = await ts.create_task(
        pid,
        "Child",
        "d",
        COORD,
        assignee_id=EXEC,
        depends_on=[parent],
        policy_id="generic_tests",
    )
    with pytest.raises(ValueError, match="unmet"):
        await ts.unblock_task(pid, child)


@pytest.mark.asyncio
async def test_reassign_keeps_blocked_when_depends_unmet(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    child = await ts.create_task(
        pid,
        "Child",
        "d",
        COORD,
        assignee_id=EXEC,
        depends_on=[parent],
        policy_id="generic_tests",
    )
    other = "exec-2"
    info = await ts.reassign_task(
        pid, child, new_assignee_id=other, reassigned_by=COORD
    )
    assert info["status"] == "blocked"
    after = await ts.get_task(pid, child)
    assert after["status"] == "blocked"
    assert after["assignee_id"] == other


def test_mismatch_hint_does_not_tell_reviewer_to_self_test():
    empty = format_attestation_mismatch_hint([], target_task_id="t-1")
    assert "Run tests with taskId" not in empty
    assert "review_task(rework)" in empty
    held = format_attestation_mismatch_hint(
        [{"id": "a1", "kind": "test_run", "task_id": "other"}],
        target_task_id="t-1",
    )
    assert "Run tests with taskId" not in held
    assert "Do not re-run tests yourself" in held
    assert "review_task(rework)" in held


@pytest.mark.asyncio
async def test_apply_depends_on_parks_claimed_draft(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    draft = await ts.create_task(
        pid, "Draft", "d", COORD, assignee_id=EXEC, policy_id="generic_tests"
    )
    row = await ts.get_task(pid, draft)
    assert row["status"] == "claimed"
    blocked = await ts.apply_depends_on(pid, draft, [parent])
    assert blocked is True
    after = await ts.get_task(pid, draft)
    assert after["status"] == "blocked"
    assert (after.get("wait_kind") or "") == "dependency"


@pytest.mark.asyncio
async def test_dispatch_existing_with_dependson_blocks(env):
    ts = TaskService()
    pid = env["project_id"]
    parent = await ts.create_task(pid, "Parent", "d", COORD, assignee_id=EXEC)
    draft = await ts.create_task(
        pid, "Draft", "d", COORD, policy_id="generic_tests"
    )
    trigger = AsyncMock()
    from hiveweave.services.dispatch import DispatchService

    svc = DispatchService()
    with (
        patch(
            "hiveweave.services.org_span.validate_dispatch_span",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_ceo_dispatch_target",
            AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.org_span.validate_executor_assignee",
            AsyncMock(return_value=None),
        ),
        patch.object(svc.inbox, "send_message", AsyncMock(return_value="m1")),
        patch("hiveweave.agents.trigger.trigger_subordinate", trigger),
    ):
        result = await svc.dispatch_task(
            pid,
            COORD,
            EXEC,
            "queued",
            existing_task_id=draft,
            depends_on=[parent],
        )
    assert result["success"] is True
    assert result["blocked"] is True
    row = await ts.get_task(pid, draft)
    assert row["status"] == "blocked"
    assert row["assignee_id"] == EXEC
