"""P0-2: get_platform_state() — verified / claimed / unknown snapshot."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services.agent_router import agent_router
from hiveweave.services.org import OrgService
from hiveweave.services.platform_state import (
    build_platform_state,
    format_platform_state,
)
from hiveweave.services.task import TaskService
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.org_tools import (
    GetPlatformStateParams,
    get_platform_state_tool,
)


@pytest.fixture
async def ps_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())
        pid = "platform-state-proj"

        async def fake_ws(p: str):
            return ws if p == pid else None

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": pid, "workspace": ws}

        agent_router.clear_project(pid)
        clear_pending_turn_result("ps-agent-1")
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_build_platform_state_splits_epistemology(ps_env):
    pid = ps_env["project_id"]
    org = OrgService()
    agent = await org.create_agent(
        {
            "id": "ps-agent-1",
            "project_id": pid,
            "name": "墨白",
            "role": "签到排行榜工程师",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )
    ts = TaskService()
    tid = await ts.create_task(
        pid,
        "Slice A",
        "d",
        creator_id=agent["id"],
        assignee_id=agent["id"],
    )
    await ts.start_task(pid, tid)

    clear_pending_turn_result(agent["id"])
    set_pending_turn_result(
        agent["id"],
        {
            "phase": "waiting",
            "summary": "I believe HIRE_UNREPORTED is blocking me",
            "gates": [],
            "waiting_on": [{"kind": "human", "ref": "user"}],
            "end_turn": True,
        },
    )

    fake_live = MagicMock()
    fake_live.status = MagicMock(value="idle")
    fake_live.disposition = "waiting_human"
    fake_live._no_progress_streak = 0

    with patch(
        "hiveweave.agents.supervisor.agent_manager.get_agent",
        return_value=fake_live,
    ):
        snap = await build_platform_state(
            agent_id=agent["id"], project_id=pid
        )

    epi = snap["epistemology"]
    verified_keys = {r["key"] for r in epi["verified"]}
    claimed_keys = {r["key"] for r in epi["claimed"]}
    unknown_keys = {r["key"] for r in epi["unknown"]}

    assert "agent.disposition" in verified_keys
    assert "ledger.obligations" in verified_keys
    assert "gates.pending_turn" in verified_keys
    assert "org.snapshot" in verified_keys

    assert "pending_turn.summary" in claimed_keys
    assert "pending_turn.waiting_on" in claimed_keys
    # Hallucinated gate name must NOT appear as verified
    for row in epi["verified"]:
        blob = str(row.get("value"))
        assert "HIRE_UNREPORTED" not in blob

    assert (
        "slices.active_obligations" in verified_keys
        or "slices" in unknown_keys
    )

    text = format_platform_state(snap)
    assert "## VERIFIED" in text
    assert "## CLAIMED" in text
    assert "## UNKNOWN" in text
    assert "HIRE_UNREPORTED" in text  # only under claimed summary
    assert "trust the platform" in text.lower() or "clues only" in text.lower()


@pytest.mark.asyncio
async def test_get_platform_state_tool_registered(ps_env):
    assert get_tool_def("get_platform_state") is not None
    pid = ps_env["project_id"]
    org = OrgService()
    agent = await org.create_agent(
        {
            "id": "ps-agent-tool",
            "project_id": pid,
            "name": "潮汐",
            "role": "支付回调工程师",
            "permission_type": "executor",
            "status": "active",
        },
        bootstrap=True,
    )

    with (
        patch(
            "hiveweave.tools.org_tools.get_project_id",
            AsyncMock(return_value=pid),
        ),
        patch(
            "hiveweave.agents.supervisor.agent_manager.get_agent",
            return_value=None,
        ),
    ):
        result = await get_platform_state_tool(
            GetPlatformStateParams(),
            agent["id"],
            workspace=".",
        )
    assert result.success is True
    assert "## VERIFIED" in result.output
    assert result.extra.get("platform_state") is not None
    assert result.extra["platform_state"]["schema_version"] == 1


def test_get_platform_state_in_base_tools():
    from hiveweave.services.permission import _BASE_TOOLS

    assert "get_platform_state" in _BASE_TOOLS


def test_get_platform_state_doom_readonly():
    from hiveweave.llm.streamer import DOOM_LOOP_READONLY_TOOLS

    assert "get_platform_state" in DOOM_LOOP_READONLY_TOOLS
