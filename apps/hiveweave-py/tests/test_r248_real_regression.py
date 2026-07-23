"""TEST11 evening R2/R4/R7/R8 regression — hit REAL fix points (not shell proxies).

R2: git_worktree_merge self-merge gate — closed ≡ approved successor
R4: poll hard-reject must attach obligations snapshot
R7: browse click with timeoutSec=10 must floor to ≥30s (no 10s premature kill)
R8: HiveWeave-level recovery (streaming finalize + expired wait clear)
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.llm import streamer as streamer_mod
from hiveweave.llm.streamer import Streamer
from hiveweave.services import task as task_module
from hiveweave.services.task import TaskService
from hiveweave.tools.browse_tools import BrowseParams, browse_tool
from hiveweave.tools.misc_tools import _check_self_merge_gate


# ── R2: merge gate — closed with reviewed_by allowed ─────────


@pytest.mark.asyncio
async def test_r2_self_merge_gate_accepts_closed_with_reviewer():
    """aacdd0f: closed is approved successor when reviewed_by present."""
    task = {
        "id": "aabbccdd-1111-2222-3333-444455556666",
        "status": "closed",
        "assignee_id": "mid-1",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    with patch.object(TaskService, "get_task", AsyncMock(return_value=task)):
        err = await _check_self_merge_gate(
            "proj-r2", "mid-1", task["id"], None
        )
    assert err is None, f"closed+reviewed_by must pass gate, got: {err}"


@pytest.mark.asyncio
async def test_r2_self_merge_gate_rejects_closed_without_reviewer():
    task = {
        "id": "aabbccdd-1111-2222-3333-444455556666",
        "status": "closed",
        "assignee_id": "mid-1",
        "evidence": {},
    }
    with patch.object(TaskService, "get_task", AsyncMock(return_value=task)):
        err = await _check_self_merge_gate(
            "proj-r2", "mid-1", task["id"], None
        )
    assert err is not None
    assert "without approval evidence" in err


@pytest.mark.asyncio
async def test_r2_self_merge_gate_rejects_running():
    task = {
        "id": "aabbccdd-1111-2222-3333-444455556666",
        "status": "running",
        "assignee_id": "mid-1",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    with patch.object(TaskService, "get_task", AsyncMock(return_value=task)):
        err = await _check_self_merge_gate(
            "proj-r2", "mid-1", task["id"], None
        )
    assert err is not None
    assert "not approved" in err


# ── R4: poll hard-reject + obligations snapshot ──────────────


@pytest.mark.asyncio
async def test_r4_poll_hard_reject_includes_obligations_snapshot():
    """aacdd0f: 3rd identical get_tasks → hard reject WITH obligations lines."""
    streamer_mod._poll_result_cache.clear()
    streamer = Streamer(max_tool_rounds=5)
    counts: dict[tuple[str, str], int] = {}
    calls = {"n": 0}

    async def on_tool(_name: str, _args: str, _id: str) -> dict:
        calls["n"] += 1
        return {"content": "Tasks (0): none"}

    with patch(
        "hiveweave.llm.streamer._build_obligations_snapshot",
        new_callable=AsyncMock,
        return_value=(
            "\nCurrent obligations (act directly, do NOT re-poll):\n"
            "  - [reviewer/submitted] taskId=deadbeef Review milestone"
        ),
    ):
        tc = {"id": "t1", "name": "get_tasks", "arguments": "{}"}
        await streamer._execute_single_tool(
            "agent-r4", tc, on_tool, poll_turn_counts=counts
        )
        await streamer._execute_single_tool(
            "agent-r4", {**tc, "id": "t2"}, on_tool, poll_turn_counts=counts
        )
        r3 = await streamer._execute_single_tool(
            "agent-r4", {**tc, "id": "t3"}, on_tool, poll_turn_counts=counts
        )

    assert "poll hard reject" in r3["content"]
    assert "Current obligations" in r3["content"], (
        "hard reject must attach obligations snapshot — previous R4 was invalid"
    )
    assert "deadbeef" in r3["content"]
    assert calls["n"] <= 2


@pytest.mark.asyncio
async def test_r4_get_tasks_tool_appends_live_obligations():
    """Evening fix: get_tasks response ends with obligations snapshot."""
    from hiveweave.tools.task_tools import GetTasksParams, get_tasks_tool

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = str(Path(tmp).resolve())
        pid = "proj-r4-live"
        aid = "agent-r4-live"

        async def fake_ws(p: str):
            return ws if p == pid else None

        task_module._migrated.discard(pid)
        try:
            with (
                patch("hiveweave.db.meta.get_project_workspace", fake_ws),
                patch(
                    "hiveweave.tools.task_tools.get_project_id",
                    new_callable=AsyncMock,
                    return_value=pid,
                ),
                patch(
                    "hiveweave.llm.streamer._build_obligations_snapshot",
                    new_callable=AsyncMock,
                    return_value=(
                        "\nCurrent obligations: none — "
                        "safe to commit_turn(waiting)."
                    ),
                ),
            ):
                ts = TaskService()
                await ts.create_task(
                    pid,
                    title="r4 sample",
                    description="d",
                    creator_id=aid,
                    assignee_id=aid,
                )
                result = await get_tasks_tool(GetTasksParams(), aid, ws)
            assert result.success
            out = result.output or ""
            assert "Current obligations" in out
            assert "short=" in out or "full UUID" in out or "Tip:" in out
        finally:
            async with project_db._ensure_lock:
                conn = project_db._cache.pop(ws, None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            task_module._migrated.discard(pid)


# ── R7: browse click timeout floor ≥30s ──────────────────────


@pytest.mark.asyncio
async def test_r7_browse_click_floors_timeout_to_30s(tmp_path: Path):
    """Evening P3-4: timeoutSec=10 on click must not kill at 10s."""
    captured: dict = {}
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout=None):
        # Only record the outer browse timeout (skip tiny drains)
        if timeout is not None and timeout >= 5:
            captured["timeout"] = timeout
            # Don't run the long communicate — just time out
            if hasattr(coro, "close"):
                coro.close()
            raise asyncio.TimeoutError()
        return await real_wait_for(coro, timeout=timeout)

    class FakeProc:
        returncode = -1

        async def communicate(self):
            await asyncio.sleep(100)
            return b"", b""

        def kill(self):
            pass

    async def fake_exec(*_a, **_k):
        return FakeProc()

    with (
        patch(
            "hiveweave.tools.browse_tools.resolve_browse_bin",
            return_value="browse-fake.exe",
        ),
        patch(
            "hiveweave.util.win_subprocess.windows_no_window_kwargs",
            return_value={},
        ),
        patch("asyncio.create_subprocess_exec", new=fake_exec),
        patch("asyncio.wait_for", new=fake_wait_for),
    ):
        result = await browse_tool(
            BrowseParams(
                args=["click", "runAll"],
                timeout_sec=10,
            ),
            "agent-r7",
            str(tmp_path),
        )

    assert captured.get("timeout") is not None
    assert captured["timeout"] >= 30, (
        f"click with timeoutSec=10 must floor to ≥30, got {captured['timeout']}"
    )
    assert result.success is False
    text = (result.output or "") + (result.error or "")
    assert "timed out after" in text
    assert "30" in text  # error message must report floored value


# ── R8: HiveWeave-level recovery (not shell timeout) ─────────


@pytest.mark.asyncio
async def test_r8_finalize_streaming_clears_orphan_flag():
    """R8 real path: streaming message finalize must clear is_streaming."""
    from hiveweave.services.chat_message import ChatMessageService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = str(Path(tmp).resolve())
        aid = "agent-r8"
        try:
            with patch(
                "hiveweave.db.meta.get_agent_project_id",
                new_callable=AsyncMock,
                return_value="proj-r8",
            ), patch(
                "hiveweave.db.meta.get_project_workspace",
                new_callable=AsyncMock,
                return_value=ws,
            ):
                cms = ChatMessageService()
                saved = await cms.save_message(
                    {
                        "agent_id": aid,
                        "role": "assistant",
                        "content": "",
                        "is_streaming": True,
                    }
                )
                mid = saved["id"]
                ok = await cms.finalize_streaming_message(
                    aid, mid, {"content": "recovered after interrupt"}
                )
                assert ok is True
                rows = await project_db.query(
                    aid,
                    "SELECT is_streaming, content FROM chat_messages "
                    "WHERE id = ?",
                    [mid],
                )
                assert rows
                assert int(rows[0]["is_streaming"] or 0) == 0
                assert "recovered" in (rows[0]["content"] or "")
        finally:
            async with project_db._ensure_lock:
                conn = project_db._cache.pop(ws, None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass


@pytest.mark.asyncio
async def test_r8_expired_wait_cleared_by_contract_service():
    """R8: expired agent_waits cleared by clear_expired (restart recovery path)."""
    from hiveweave.services.wait_contract import wait_contract_service

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = str(Path(tmp).resolve())
        pid = "proj-r8-wait"
        aid = "agent-r8-wait"

        async def fake_ws(p: str):
            return ws if p == pid else None

        try:
            with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
                now = int(time.time() * 1000)
                created = await wait_contract_service.replace_waits(
                    pid,
                    aid,
                    [
                        {
                            "kind": "agent",
                            "ref": "peer-1",
                            "expires_at": now - 60_000,
                        }
                    ],
                    phase="waiting",
                )
                assert created
                cleared = await wait_contract_service.clear_expired(pid, aid)
                assert any(
                    c.get("id") == created[0]["id"] for c in cleared
                ), (
                    "expired waits must be cleared — HiveWeave recovery path "
                    "previous R8 never tested (it only ran shell timeout)"
                )
                active = await wait_contract_service.list_active(pid, aid)
                assert active == []
        finally:
            async with project_db._ensure_lock:
                conn = project_db._cache.pop(ws, None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
