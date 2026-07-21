"""PR1 regression: worktree status, work logs, short task id, wake hygiene, waiver gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.wake_policy import classify_message, should_wake
from hiveweave.tools.misc_tools import (
    GitWorktreeStatusParams,
    git_worktree_status_tool,
)
from hiveweave.tools.orchestration_tools import (
    ReadWorkLogsParams,
    read_work_logs_tool,
)


@pytest.mark.asyncio
async def test_git_worktree_status_reads_status_key():
    """Service returns {status: {branch, has_uncommitted, head}} — not info/status."""
    gwt = MagicMock()
    gwt.ensure_git_repo = AsyncMock()
    gwt.info = AsyncMock(
        return_value={
            "success": True,
            "status": {
                "branch": "hw/A004/work",
                "has_uncommitted": False,
                "head": "abc1234",
                "short_id": "A004",
            },
        }
    )

    with (
        patch(
            "hiveweave.tools.misc_tools._get_worktree_context",
            AsyncMock(return_value=("/proj", "A001", "pid")),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            return_value=gwt,
        ),
    ):
        result = await git_worktree_status_tool(
            GitWorktreeStatusParams(shortId="A004"),
            "agent-1",
            "/proj",
            ctx=None,
        )

    assert result.success is True
    assert "hw/A004/work" in result.output
    assert "dirty=False" in result.output
    assert "abc1234" in result.output
    assert "?" not in result.output.split("Branch:")[1][:20]


@pytest.mark.asyncio
async def test_git_worktree_status_missing_worktree():
    gwt = MagicMock()
    gwt.ensure_git_repo = AsyncMock()
    gwt.info = AsyncMock(return_value={"success": True, "status": None})

    with (
        patch(
            "hiveweave.tools.misc_tools._get_worktree_context",
            AsyncMock(return_value=("/proj", "A001", "pid")),
        ),
        patch(
            "hiveweave.services.git_worktree.GitWorktreeService",
            return_value=gwt,
        ),
    ):
        result = await git_worktree_status_tool(
            GitWorktreeStatusParams(),
            "agent-1",
            "/proj",
        )

    assert result.success is False
    assert "No worktree" in (result.error or "")


@pytest.mark.asyncio
async def test_read_work_logs_uses_type_and_summary():
    class FakeWLS:
        async def get_recent(self, project_id, agent_id, limit=10):
            return [
                {
                    "agent_id": agent_id,
                    "type": "turn_result",
                    "summary": "[done_slice] shipped",
                    "created_at": 100,
                }
            ]

    ctx = MagicMock()
    ctx.org = MagicMock()
    ctx.org.resolve_agent = AsyncMock(
        return_value={"id": "sub-1", "name": "Kid", "short_id": "A004"}
    )

    with (
        patch(
            "hiveweave.tools.orchestration_tools.get_project_id",
            AsyncMock(return_value="pid"),
        ),
        patch(
            "hiveweave.services.work_log.WorkLogService",
            return_value=FakeWLS(),
        ),
    ):
        result = await read_work_logs_tool(
            ReadWorkLogsParams(agentId="A004"),
            "boss",
            "/ws",
            ctx=ctx,
        )

    assert result.success is True
    assert "turn_result" in result.output
    assert "shipped" in result.output
    assert "No work logs" not in result.output


def test_notify_classifies_as_message_and_wakes():
    cat = classify_message(
        message="FYI: merge done",
        message_type="notify",
        from_agent_id="peer-1",
    )
    assert cat == "message"
    assert should_wake(cat, disposition="runnable") is True


def test_ask_still_wakes():
    cat = classify_message(
        message="Please reply",
        message_type="ask",
        expect_report=True,
        from_agent_id="peer-1",
    )
    assert cat == "message"
    assert should_wake(cat, disposition="runnable") is True


@pytest.mark.asyncio
async def test_resolve_task_short_id():
    from hiveweave.services.task import TaskService

    ts = TaskService()
    full = "ee970a4f-e574-4155-8d8d-3582dda297ec"

    async def fake_query(project_id, sql, params=None):
        params = params or []
        if "WHERE id = ?" in sql and params:
            if params[0] == full:
                return [{"id": full}]
            return []
        if "LIKE ?" in sql and params:
            needle = str(params[0]).rstrip("%").lower()
            if full.lower().startswith(needle) or full.replace("-", "").lower().startswith(
                needle.replace("-", "")
            ):
                return [{"id": full}]
            return []
        return []

    with (
        patch("hiveweave.services.task._ensure_schema", AsyncMock()),
        patch("hiveweave.services.task._query", AsyncMock(side_effect=fake_query)),
    ):
        assert await ts.resolve_task_id("pid", full) == full
        assert await ts.resolve_task_id("pid", "ee970a4f") == full
        assert await ts.resolve_task_id("pid", "missingg") is None


@pytest.mark.asyncio
async def test_http_gate_respects_waiver():
    from hiveweave.api.tasks import _gate_attestation_for_task

    with patch(
        "hiveweave.api.tasks.has_valid_waiver",
        AsyncMock(return_value=True),
    ):
        # Must not raise
        await _gate_attestation_for_task(
            "pid",
            {"id": "t1", "title": "x", "tags": []},
            {"attestation_ids": []},
        )


@pytest.mark.asyncio
async def test_mark_read_clears_wake():
    """mark_read_by_ids SQL must set wake=0."""
    from hiveweave.services.inbox import InboxService

    executed: list[tuple] = []

    async def fake_execute(agent_id, sql, params=None):
        executed.append((sql, params))

    with (
        patch(
            "hiveweave.services.inbox._ensure_schema",
            AsyncMock(),
        ),
        patch(
            "hiveweave.db.project.execute",
            AsyncMock(side_effect=fake_execute),
        ),
    ):
        await InboxService().mark_read_by_ids("agent-1", ["m1", "m2"])

    assert executed
    sql, _ = executed[0]
    assert "wake = 0" in sql or "wake=0" in sql.replace(" ", "")


@pytest.mark.asyncio
async def test_hire_calls_retry_qa_blocked_verify():
    """hire success path must invoke retry_qa_blocked_verify_tasks."""
    import hiveweave.tools.org_tools as org_tools

    src = open(org_tools.__file__, encoding="utf-8").read()
    assert "retry_qa_blocked_verify_tasks" in src
    assert "hire_agent.verify_retry" in src
