"""子代理（spawn_subagent）回归测试。"""
from __future__ import annotations

from hiveweave.llm.streamer.doom_loop import doom_loop_limit
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
    READONLY_TOOLS,
    READWRITE_TOOLS,
)


def test_spawn_subagent_in_all_family_lists():
    for tools in (CEO_TOOLS, COORDINATOR_BUILDER_TOOLS, HR_TOOLS,
                  READONLY_TOOLS, READWRITE_TOOLS):
        assert "spawn_subagent" in tools, tools


def test_spawn_subagent_doom_bucket_tight():
    assert doom_loop_limit("spawn_subagent") == 3


from unittest.mock import AsyncMock, patch


def test_extend_elapsed_budget_shifts_started_at():
    from hiveweave.services.run_ledger import RunLedger

    ledger = RunLedger()
    execute = AsyncMock()
    with patch("hiveweave.services.run_ledger.project_db.execute", execute):
        # 不 await：execute 是 AsyncMock，直接调用返回 coroutine
        import asyncio
        asyncio.run(ledger.extend_elapsed_budget("a1", "r1", 240_000))

    sql = execute.await_args.args[1]
    assert "started_at = started_at - ?" in sql
    assert execute.await_args.args[2] == [240_000, "r1"]
