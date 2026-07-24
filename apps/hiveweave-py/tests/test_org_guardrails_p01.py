"""DESIGN-3 org blast-radius guards + DESIGN-2 tool-loop stall counter."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.llm.streamer import (
    TOOL_LOOP_STALL_LIMIT,
    round_made_progress,
)
from hiveweave.services.agent_router import agent_router
from hiveweave.services.git_worktree import GitWorktreeService
from hiveweave.services.org import OrgService
from hiveweave.services.org_guardrails import (
    DISMISS_QUOTA_PER_GAME_DAY,
    check_dismiss_quota,
    check_same_role_rehire,
    record_dismiss,
)


# ── DESIGN-2: stall counter unit ─────────────────────────


def test_round_made_progress_readonly_is_stall():
    calls = [
        {"id": "1", "name": "get_tasks"},
        {"id": "2", "name": "read_file"},
    ]
    assert round_made_progress(calls) is False


def test_round_made_progress_mutating_success():
    calls = [
        {"id": "1", "name": "get_tasks"},
        {"id": "2", "name": "write_file"},
    ]
    assert round_made_progress(calls) is True


def test_round_made_progress_mutating_failed_is_stall():
    calls = [{"id": "1", "name": "write_file"}]
    assert round_made_progress(calls, error_ids={"1"}) is False
    assert round_made_progress(calls, duplicate_ids={"1"}) is False


def test_stall_limit_matches_magentic_one():
    assert TOOL_LOOP_STALL_LIMIT == 2


# ── DESIGN-3: dismiss quota + same-role rehire ───────────


@pytest.fixture
async def org_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = str(Path(tmpdir).resolve())
        pid = "org-guard-proj"

        async def fake_ws(p: str):
            return ws if p == pid else None

        with (
            patch("hiveweave.db.meta.get_project_workspace", fake_ws),
            patch(
                "hiveweave.services.org_guardrails.current_game_day",
                AsyncMock(return_value=7),
            ),
        ):
            yield {"project_id": pid, "workspace": ws}

        agent_router.clear_project(pid)
        async with project_db._ensure_lock:
            conn = project_db._cache.pop(ws, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _seed_leaf(org: OrgService, pid: str, *, exec_id: str, role: str):
    ceo = await org.create_agent(
        {
            "id": f"ceo-{exec_id}",
            "project_id": pid,
            "name": "归零",
            "role": "ceo",
            "permission_type": "coordinator",
            "status": "active",
        },
        bootstrap=True,
    )
    coord = await org.create_agent(
        {
            "id": f"coord-{exec_id}",
            "project_id": pid,
            "name": "知远",
            "role": "frontend-architect",
            "permission_type": "coordinator",
            "status": "active",
            "parent_id": ceo["id"],
        },
        bootstrap=True,
    )
    exec_ = await org.create_agent(
        {
            "id": exec_id,
            "project_id": pid,
            "name": f"花-{exec_id[-4:]}",
            "role": role,
            "permission_type": "executor",
            "status": "active",
            "parent_id": coord["id"],
        },
        bootstrap=True,
    )
    return ceo, coord, exec_


async def _quiet_dismiss(org: OrgService, pid: str, agent_id: str) -> dict:
    with (
        patch.object(
            GitWorktreeService,
            "quarantine_for_review",
            new=AsyncMock(return_value={"quarantined": True}),
        ),
        patch.object(
            GitWorktreeService,
            "delete",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.send_message",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "hiveweave.services.roster.RosterService.update",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "hiveweave.services.game_time.GameTimeService.cancel_alarms_for_agent",
            new=AsyncMock(return_value=0),
        ),
    ):
        return await org.dismiss_agent(pid, agent_id, dismissed_by="hr-1")


@pytest.mark.asyncio
async def test_dismiss_quota_blocks_after_limit(org_env):
    pid = org_env["project_id"]
    org = OrgService()

    # Seed quota-1 worth of dismiss log entries directly
    for i in range(DISMISS_QUOTA_PER_GAME_DAY):
        await record_dismiss(
            pid,
            agent_id=f"old-{i}",
            role=f"旧模块{i}工程师",
            dismissed_by="hr-1",
            short_id=f"A{i:03d}",
            name=f"旧{i}",
        )

    err = await check_dismiss_quota(pid)
    assert err is not None
    assert "quota exhausted" in err.lower() or "Dismiss quota" in err

    # Live dismiss should also hard-reject
    _, _, exec_ = await _seed_leaf(
        org, pid, exec_id="exec-quota", role="签到排行榜工程师"
    )
    result = await _quiet_dismiss(org, pid, exec_["id"])
    assert result["success"] is False
    assert "quota" in (result.get("message") or "").lower()


@pytest.mark.asyncio
async def test_same_role_rehire_blocked_after_dismiss(org_env):
    pid = org_env["project_id"]
    org = OrgService()
    role = "认证API工程师"
    _, _, exec_ = await _seed_leaf(org, pid, exec_id="exec-rehire", role=role)

    result = await _quiet_dismiss(org, pid, exec_["id"])
    assert result["success"] is True

    err = await check_same_role_rehire(pid, role)
    assert err is not None
    assert "Same-role rehire blocked" in err
    assert "transfer_agent" in err or "bind_skill" in err

    with pytest.raises(ValueError, match="Same-role rehire blocked"):
        await org.create_agent(
            {
                "project_id": pid,
                "name": "青禾",
                "role": role,
                "permission_type": "executor",
                "status": "active",
                "parent_id": f"coord-exec-rehire",
            }
        )


@pytest.mark.asyncio
async def test_different_role_hire_allowed_after_dismiss(org_env):
    pid = org_env["project_id"]
    org = OrgService()
    _, coord, exec_ = await _seed_leaf(
        org, pid, exec_id="exec-diff", role="签到排行榜工程师"
    )
    result = await _quiet_dismiss(org, pid, exec_["id"])
    assert result["success"] is True

    # Different role is fine
    assert await check_same_role_rehire(pid, "支付回调工程师") is None
    hired = await org.create_agent(
        {
            "project_id": pid,
            "name": "潮汐",
            "role": "支付回调工程师",
            "permission_type": "executor",
            "status": "active",
            "parent_id": coord["id"],
        }
    )
    assert hired["id"]
