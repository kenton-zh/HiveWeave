"""VERIFY multiplication knives: in-flight attestation bind + merge scope stamp."""

from __future__ import annotations

import json

import pytest

from hiveweave.services.task import TaskService
from hiveweave.tools.bash import _resolve_test_attestation_task_id
from hiveweave.tools.tasks.verify_merge import (
    _stamp_merge_fact_on_parent_tasks,
    nudge_verify_tasks_after_merge,
)
from hiveweave.tools.tasks.verify_spawn import _spawn_post_approve_verify_task

from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


async def _mint_verify(ts: TaskService, pid: str, title: str) -> str:
    parent = await ts.create_task(
        pid, title, "d", creator_id=COORD, assignee_id=EXEC
    )
    return await ts.create_task(
        pid,
        f"VERIFY: {title}",
        "verify",
        creator_id=COORD,
        assignee_id=EXEC,
        parent_task_id=parent,
        tags=["verify", "mandatory"],
        source="system",
    )


@pytest.mark.asyncio
async def test_bind_prefers_in_flight_when_multiple_verifies(task_env):
    """Queued created VERIFY must not leave test_run unbound (s3-clone_01)."""
    ts = TaskService()
    pid = task_env["project_id"]
    queued = await _mint_verify(ts, pid, "Face A")
    flying = await _mint_verify(ts, pid, "Face B")
    await ts.claim_task(pid, flying, EXEC)
    await ts.start_task(pid, flying)

    tid, note = await _resolve_test_attestation_task_id(pid, EXEC)
    assert tid == flying
    assert "in-flight" in note
    queued_row = await ts.get_task(pid, queued)
    assert queued_row["status"] == "created"


@pytest.mark.asyncio
async def test_bind_does_not_steal_running_impl_when_verify_submitted(task_env):
    """Submitted in-flight VERIFY must not stamp a running implementation task."""
    ts = TaskService()
    pid = task_env["project_id"]
    flying = await _mint_verify(ts, pid, "Face B")
    await ts.claim_task(pid, flying, EXEC)
    await ts.start_task(pid, flying)
    await ts.submit_task(
        pid,
        flying,
        evidence={"verdict": "PASS", "tests_passed": True, "test_output": "ok"},
    )
    # _mint_verify also leaves the parent claimed on EXEC — park it so the
    # only active assignee work is the implementation task under test.
    from hiveweave.services import task as task_module

    for t in await ts.list_tasks(pid, assignee_id=EXEC):
        if str(t.get("id") or "") != flying and (t.get("status") or "") in (
            "created",
            "claimed",
            "running",
        ):
            await task_module._execute(
                pid, "UPDATE tasks SET status = 'closed' WHERE id = ?", [t["id"]]
            )
    impl = await ts.create_task(
        pid, "Face C impl", "d", creator_id=COORD, assignee_id=EXEC
    )
    await ts.claim_task(pid, impl, EXEC)
    await ts.start_task(pid, impl)

    tid, _note = await _resolve_test_attestation_task_id(pid, EXEC)
    assert tid == impl


@pytest.mark.asyncio
async def test_bind_still_refuses_when_no_in_flight_holder(task_env):
    """Two created VERIFYs and nothing in-flight → still refuse silent bind."""
    ts = TaskService()
    pid = task_env["project_id"]
    await _mint_verify(ts, pid, "Face A")
    await _mint_verify(ts, pid, "Face B")

    tid, note = await _resolve_test_attestation_task_id(pid, EXEC)
    assert tid is None
    assert "UNBOUND" in note
    assert "multiple open VERIFY" in note


@pytest.mark.asyncio
async def test_stamp_fills_files_changed_when_empty(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    row = await ts.get_task(pid, tid)
    await _stamp_merge_fact_on_parent_tasks(
        pid,
        [row],
        merged_by=COORD,
        merge_commit="a" * 40,
        merged_files=["app/foo.py", "tests/test_foo.py"],
    )
    ev = (await ts.get_task(pid, tid))["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    assert ev["merge_commit"] == "a" * 40
    assert "app/foo.py" in ev["files_changed"]
    assert "tests/test_foo.py" in ev["files_changed"]


@pytest.mark.asyncio
async def test_stamp_does_not_overwrite_existing_files_changed(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    tid = await ts.create_task(
        pid, "Feature", "d", creator_id=COORD, assignee_id=EXEC
    )
    row = await ts.get_task(pid, tid)
    await _stamp_merge_fact_on_parent_tasks(
        pid,
        [row],
        merged_by=COORD,
        merge_commit="a" * 40,
        merged_files=["keep/me.py"],
    )
    row = await ts.get_task(pid, tid)
    await _stamp_merge_fact_on_parent_tasks(
        pid,
        [row],
        merged_by=COORD,
        merge_commit="b" * 40,
        merged_files=["other/path.py"],
    )
    ev = (await ts.get_task(pid, tid))["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    assert "keep/me.py" in ev["files_changed"]
    assert "other/path.py" not in ev["files_changed"]
    assert ev["merge_commit"] == "b" * 40


@pytest.mark.asyncio
async def test_nudge_after_merge_does_not_revive_closed_qa_parent(task_env):
    from unittest.mock import AsyncMock, patch

    ts = TaskService()
    pid = task_env["project_id"]
    qa_id = "qa-nudge-closed"
    parent_id = await ts.create_task(
        pid, "Integration suite", "d", creator_id=COORD, assignee_id=qa_id
    )
    await ts.claim_task(pid, parent_id, qa_id)
    await ts.start_task(pid, parent_id)
    await ts.submit_task(
        pid, parent_id, evidence={"tests_passed": True, "test_output": "ok"}
    )
    await ts.start_review(pid, parent_id)
    await ts.review_task(pid, parent_id, "approve")
    parent = await ts.get_task(pid, parent_id)

    async def fake_get(aid, *a, **k):
        aid = str(aid)
        role = "qa_engineer" if aid == qa_id else "executor"
        return {
            "id": aid,
            "role": role,
            "status": "active",
            "permission_type": "executor",
        }

    with patch(
        "hiveweave.services.org.OrgService.get_agent",
        new=AsyncMock(side_effect=fake_get),
    ):
        assert await _spawn_post_approve_verify_task(ts, pid, COORD, parent) is None
        await nudge_verify_tasks_after_merge(
            pid,
            COORD,
            merged_agent_id=qa_id,
            merged_files=["tests/test_foo.py"],
            merge_commit="c" * 40,
        )
    parent2 = await ts.get_task(pid, parent_id)
    assert parent2["status"] == "closed"
