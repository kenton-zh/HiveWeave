"""TEST11 evening-review fixes: MSYS paths, task short-id, agent 花名, policy."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.org import OrgService
from hiveweave.services.policy import write_path_allowed
from hiveweave.services.task import TaskService
from hiveweave.tools.file import (
    normalize_input_path,
    read_file,
    resolve_for_read,
    _resolve_safe,
)


PROJECT_ID = "test11-evening"


@pytest.fixture
async def task_env():
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


# ── P1: MSYS2 path normalization ─────────────────────────────


def test_normalize_msys_drive_path():
    assert normalize_input_path("/d/PC_AI/Project/TEST11/a.txt") == (
        "D:/PC_AI/Project/TEST11/a.txt"
    )
    assert normalize_input_path("/c/Users/x") == "C:/Users/x"
    assert normalize_input_path("src/foo.py") == "src/foo.py"
    assert normalize_input_path(r"D:\PC_AI\x") == "D:/PC_AI/x"


@pytest.mark.asyncio
async def test_msys_path_read_within_project(tmp_path: Path):
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A004"
    (wt / "src").mkdir(parents=True)
    target = wt / "src" / "own.txt"
    target.write_text("hello-msys", encoding="utf-8")

    # Simulate Git Bash absolute path back into read_file
    drive = project.resolve().drive.rstrip(":").lower()  # e.g. 'd'
    rest = str(target.resolve()).replace("\\", "/")
    if ":" in rest:
        rest = rest.split(":", 1)[1]
    msys = f"/{drive}{rest}"

    root = str(project.resolve())
    resolved = resolve_for_read(str(wt), msys, root)
    assert resolved is not None
    assert Path(resolved).read_text(encoding="utf-8") == "hello-msys"

    result = await read_file(
        file_path=msys,
        offset=0,
        limit=10,
        workspace_path=str(wt),
        project_root=root,
    )
    assert result["success"] is True
    assert "hello-msys" in result["output"]


def test_msys_write_sandbox_still_confines(tmp_path: Path):
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A004"
    wt.mkdir(parents=True)
    peer = project / ".hiveweave" / "worktrees" / "A005" / "x.txt"
    peer.parent.mkdir(parents=True)
    peer.write_text("peer")

    drive = peer.resolve().drive.rstrip(":").lower()
    rest = str(peer.resolve()).replace("\\", "/")
    if ":" in rest:
        rest = rest.split(":", 1)[1]
    msys_peer = f"/{drive}{rest}"

    # Absolute peer path must not be writable from A004 worktree
    assert _resolve_safe(str(wt), msys_peer) is None


# ── P2: short task id ───────────────────────────────────────


@pytest.mark.asyncio
async def test_require_task_id_resolves_prefix(task_env):
    ts = TaskService()
    pid = task_env["project_id"]
    full = await ts.create_task(
        pid,
        title="evening short id",
        description="desc",
        creator_id="ceo-1",
        assignee_id="exec-1",
    )
    short = full[:8]
    assert await ts.require_task_id(pid, short) == full
    # Already claimed by create_task (assign=claim); start via short id
    await ts.start_task(pid, short)
    claimed = await ts.get_task(pid, full)
    assert claimed["status"] == "running"


# ── P2: agent 花名 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_agent_ref_by_name(tmp_path: Path):
    ws = str(tmp_path.resolve())
    pid = "proj-name-ref"

    async def fake_ws(p: str):
        return ws if p == pid else None

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        org = OrgService()
        created = await org.create_agent(
            {
                "project_id": pid,
                "name": "流火",
                "role": "签到排行榜工程师",
                "permission_type": "executor",
                "status": "active",
            }
        )
        hit = await org.resolve_agent_ref(pid, "流火")
        assert hit is not None
        assert hit["id"] == created["id"]
        assert await org.resolve_agent_ref(pid, "不存在的人") is None


# ── P3: .hiveweave reports/drafts scope ─────────────────────


def test_write_path_allows_reports_and_drafts_for_ceo():
    ceo = {
        "role": "ceo",
        "permission_type": "coordinator",
        "role_family": "ceo",
    }
    assert write_path_allowed(ceo, ".hiveweave/reports/r.md") is None
    assert write_path_allowed(ceo, ".hiveweave/drafts/d.md") is None
    assert write_path_allowed(ceo, ".hiveweave/shared/s.md") is None
    # Absolute-ish MSYS shared path also allowed via marker
    assert write_path_allowed(
        ceo, "/d/PC_AI/Project/X/.hiveweave/shared/s.md"
    ) is None
    deny = write_path_allowed(ceo, "src/app.py")
    assert deny is not None
    assert "source_write" in deny
