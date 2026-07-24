"""Slice-driven P0: contract_json, ready gate, submitted machine pre-run."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.services.task_contract import (
    check_ready_gate,
    parse_contract,
    run_machine_acceptance,
    validate_contract,
)


PROJECT_ID = "slice-p0-proj"
COORD = "coord-slice"
EXEC = "exec-slice"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)
        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _spec_contract(**overrides):
    c = {
        "id": "spec-ui-render",
        "name": "UI Spec",
        "owner_role": "游戏UI渲染工程师",
        "inputs": [],
        "deliverables": [
            {
                "path": "docs/spec/ui-spec.md",
                "type": "markdown",
                "must_contain": ["组件树", "渲染管线"],
                "min_lines": 3,
            }
        ],
        "acceptance": [
            {
                "id": "AC1",
                "type": "file_exists",
                "path": "docs/spec/ui-spec.md",
            },
            {
                "id": "AC2",
                "type": "content_contains",
                "path": "docs/spec/ui-spec.md",
                "patterns": ["组件树", "渲染管线"],
            },
            {
                "id": "AC3",
                "type": "manual_review",
                "note": "与 PRD 一致性",
            },
        ],
        "rework_limit": 3,
    }
    c.update(overrides)
    return c


def test_validate_contract_ok():
    assert validate_contract(_spec_contract()) is None


def test_validate_contract_rejects_bad_type():
    bad = _spec_contract()
    bad["acceptance"] = [{"id": "X", "type": "vibes"}]
    assert validate_contract(bad) is not None


def test_machine_prerun_pass_and_fail(tmp_path: Path):
    root = tmp_path
    (root / "docs" / "spec").mkdir(parents=True)
    (root / "docs" / "spec" / "ui-spec.md").write_text(
        "组件树\n渲染管线\n更多内容\n",
        encoding="utf-8",
    )
    ok = run_machine_acceptance(_spec_contract(), workspace_root=root)
    assert ok.passed is True
    assert any(r.deferred for r in ok.results)  # manual_review

    fail = run_machine_acceptance(_spec_contract(), workspace_root=tmp_path / "empty")
    (tmp_path / "empty").mkdir(exist_ok=True)
    fail = run_machine_acceptance(_spec_contract(), workspace_root=tmp_path / "empty")
    assert fail.passed is False
    assert fail.blocking_failures()


@pytest.mark.asyncio
async def test_create_slice_starts_ready_without_upstream(env):
    ts = TaskService()
    pid = env["project_id"]
    tid = await ts.create_task(
        pid,
        "UI Spec",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        contract_json=_spec_contract(),
    )
    task = await ts.get_task(pid, tid)
    c = parse_contract(task["contract_json"])
    assert c is not None
    assert c["slice_status"] == "ready"


@pytest.mark.asyncio
async def test_ready_gate_blocks_until_upstream_verified(env):
    ts = TaskService()
    pid = env["project_id"]
    upstream_id = await ts.create_task(
        pid,
        "Domain explore",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        contract_json={
            "id": "domain-explore-ui",
            "acceptance": [
                {"id": "AC1", "type": "manual_review", "note": "ok"}
            ],
        },
    )
    downstream_id = await ts.create_task(
        pid,
        "UI Spec",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        contract_json=_spec_contract(
            inputs=[{"slice": "domain-explore-ui"}],
        ),
    )
    down = await ts.get_task(pid, downstream_id)
    assert parse_contract(down["contract_json"])["slice_status"] == "draft"

    # Cannot start while upstream not verified
    with pytest.raises(ValueError, match="READY GATE"):
        await ts.start_task(pid, downstream_id)

    # Complete upstream path to verified
    await ts.start_task(pid, upstream_id)
    # Upstream has only manual_review → pre-run passes
    await ts.submit_task(
        pid, upstream_id, {"summary": "done", "tests_passed": True}
    )
    await ts.start_review(pid, upstream_id, reviewer_id=COORD)
    await ts.review_task(pid, upstream_id, "approve", reviewer_id=COORD)
    up = await ts.get_task(pid, upstream_id)
    assert parse_contract(up["contract_json"])["slice_status"] == "verified"

    # Now downstream can start
    await ts.start_task(pid, downstream_id)
    down2 = await ts.get_task(pid, downstream_id)
    assert parse_contract(down2["contract_json"])["slice_status"] == "in_progress"


@pytest.mark.asyncio
async def test_submit_prerun_rejects_missing_deliverable(env):
    ts = TaskService()
    pid = env["project_id"]
    ws = env["workspace"]
    tid = await ts.create_task(
        pid,
        "UI Spec",
        "d",
        creator_id=COORD,
        assignee_id=EXEC,
        contract_json=_spec_contract(),
    )
    await ts.start_task(pid, tid)
    with pytest.raises(ValueError, match="SUBMIT PRE-RUN FAILED"):
        await ts.submit_task(
            pid, tid, {"summary": "fake done", "tests_passed": True}
        )

    # Write deliverable into project root (no worktree in this unit test)
    spec = Path(ws) / "docs" / "spec"
    spec.mkdir(parents=True)
    (spec / "ui-spec.md").write_text(
        "# Spec\n组件树\n渲染管线\nline4\n",
        encoding="utf-8",
    )
    await ts.submit_task(
        pid, tid, {"summary": "real done", "tests_passed": True}
    )
    task = await ts.get_task(pid, tid)
    assert task["status"] == "submitted"
    c = parse_contract(task["contract_json"])
    assert c["slice_status"] == "submitted"
    assert c["machine_pre_run"]["passed"] is True


@pytest.mark.asyncio
async def test_check_ready_gate_unit():
    async def lookup_sid(sid):
        if sid == "up":
            return {
                "id": "t1",
                "status": "approved",
                "contract_json": {
                    "id": "up",
                    "slice_status": "verified",
                },
            }
        return None

    async def lookup_tid(_tid):
        return None

    err = await check_ready_gate(
        "p",
        {
            "contract_json": {
                "id": "down",
                "inputs": [{"slice": "missing"}],
                "acceptance": [],
            },
            "depends_on": [],
        },
        lookup_by_slice_id=lookup_sid,
        lookup_by_task_id=lookup_tid,
    )
    assert err is not None

    err2 = await check_ready_gate(
        "p",
        {
            "contract_json": {
                "id": "down",
                "inputs": [{"slice": "up"}],
                "acceptance": [],
            },
            "depends_on": [],
        },
        lookup_by_slice_id=lookup_sid,
        lookup_by_task_id=lookup_tid,
    )
    assert err2 is None
