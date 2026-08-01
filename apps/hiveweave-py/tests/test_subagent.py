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
