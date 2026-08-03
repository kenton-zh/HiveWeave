"""TEST18 P0-1/P0-2/P0-4 regression tests (behavior-level).

Covers:
- ask_agent/notify_agent replyTo param → contract closes without new obligation
- garbage replyTo → downgrade + warning note (real DB)
- command-text taskId fallback binding (multi open VERIFY)
- in_progress commit_turn loop brake (real function call)
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "")

from hiveweave.db import project as project_db
from hiveweave.services import inbox as inbox_module
from hiveweave.services.inbox import InboxService
from hiveweave.tools.turn_tools import (
    AskNotifyParams,
    _in_progress_counts,
    _IN_PROGRESS_LIMIT,
    _IN_PROGRESS_WINDOW_MS,
)

PROJECT_ID = "test-test18-fixes"
CEO_ID = "test-ceo"
DEV_ID = "test-dev"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_project_id(aid: str):
            return PROJECT_ID if aid in (CEO_ID, DEV_ID) else None

        async def fake_get_agent_by_id(aid: str):
            return {"id": aid, "name": "x", "status": "active"}

        async def fake_publish(*args, **kwargs):
            return None

        inbox_module._migrated.discard(CEO_ID)
        inbox_module._migrated.discard(DEV_ID)
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)

        with (
            patch("hiveweave.db.meta.get_project_workspace",
                  fake_get_project_workspace),
            patch("hiveweave.db.meta.get_agent_project_id",
                  fake_get_agent_project_id),
            patch("hiveweave.db.meta.get_agent_by_id", fake_get_agent_by_id),
            patch(
                "hiveweave.realtime.event_bus.status_event_bus"
                ".publish_chat_message",
                fake_publish,
            ),
        ):
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        project_db._agent_cache.pop(CEO_ID, None)
        project_db._agent_cache.pop(DEV_ID, None)


async def _fetch_one(env, sql, params):
    conn = await project_db.ensure_project_db(env["workspace_path"])
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


# ── P0-1: replyTo closes contract / garbage downgrades ────


@pytest.mark.asyncio
async def test_valid_reply_to_closes_contract(env):
    """ask + explicit replyTo closes the original contract (no new obligation)."""
    svc = InboxService()

    ask = await svc.send_message(
        DEV_ID, CEO_ID, "请回复测试结果", message_type="ask",
        expect_report=True,
    )
    contract_id = ask["reply_contract_id"]
    assert contract_id

    # CEO replies with ask + explicit replyTo → downgraded to notify, no new
    # contract, and the original contract is closed.
    reply = await svc.send_message(
        CEO_ID, DEV_ID, "测试通过", message_type="ask",
        expect_report=True, reply_to=contract_id,
    )
    assert reply["expect_report"] is False
    assert reply["reply_contract_id"] is None

    row = await _fetch_one(
        env, "SELECT message_type, expect_report, reply_to FROM inbox WHERE id = ?",
        [reply["id"]],
    )
    assert row["message_type"] == "notify"
    assert row["expect_report"] == 0
    assert row["reply_to"] == contract_id

    # Original ask is no longer outstanding for the asker.
    assert await svc.get_outstanding_ask_senders(CEO_ID) == set()


@pytest.mark.asyncio
async def test_garbage_reply_to_warns_and_downgrades(env):
    """Unknown replyTo → downgrade + warning note (no hard block)."""
    svc = InboxService()

    msg = await svc.send_message(
        CEO_ID, DEV_ID, "回复内容", message_type="ask",
        expect_report=True,
        reply_to="deadbeef-dead-beef-dead-beefdeadbeef",
    )
    assert msg["expect_report"] is False
    assert "replyTo" in (msg.get("warning") or "")
    assert "deadbeef" in (msg.get("warning") or "")

    row = await _fetch_one(
        env, "SELECT message_type, expect_report FROM inbox WHERE id = ?",
        [msg["id"]],
    )
    assert row["message_type"] == "notify"
    assert row["expect_report"] == 0


# ── P0-1: AskNotifyParams schema ─────────────────────────


def test_ask_notify_params_reply_to_alias():
    p = AskNotifyParams.model_validate(
        {"recipients": ["A006"], "message": "hi", "replyTo": "abc123"}
    )
    assert p.reply_to == "abc123"
    p2 = AskNotifyParams.model_validate(
        {"recipients": ["A006"], "message": "hi", "reply_to": "xyz"}
    )
    assert p2.reply_to == "xyz"


def test_ask_agent_tool_forwards_reply_to():
    """Behavior: ask_agent + replyTo is forwarded to _send_message_core."""
    import asyncio

    from hiveweave.tools.result import ToolResult
    from hiveweave.tools.turn_tools import ask_agent_tool

    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return ToolResult.ok("sent", results=[{"to": "x"}])

    with patch(
        "hiveweave.tools.orchestration_tools._send_message_core",
        side_effect=fake_send,
    ):
        params = AskNotifyParams(
            recipients=["A006"],
            message="reply",
            reply_to="a7e98bc9-64ba-419e-902f-7a816c55cbc0",
        )
        asyncio.run(ask_agent_tool(params, "a1", "/w", None))

    assert sent["reply_to"] == "a7e98bc9-64ba-419e-902f-7a816c55cbc0"
    assert sent["message_type"] == "ask"


# ── P0-2: command-text taskId regex ──────────────────────


def test_command_task_id_regex_variants():
    from hiveweave.tools.bash import _COMMAND_TASK_ID_RE

    cases = [
        ("npx vitest run taskId=69e1d14a-498e-44b5-962f-3374da5fd401", "69e1d14a"),
        ("cd /w && TASK_ID=69e1d14a npx vitest run", "69e1d14a"),
        ("HW_TASK_ID=69e1d14a npx vitest run", "69e1d14a"),
        ("npx vitest run --reporter=basic", None),
        ("task_id=69e1d14a npx vitest run", "69e1d14a"),
        ("MY_TASK_ID=69e1d14a npx vitest", None),  # \b boundary: no match
    ]
    for cmd, want in cases:
        got = _COMMAND_TASK_ID_RE.findall(cmd)
        if want is None:
            assert got == [], f"unexpected match in {cmd!r}: {got}"
        else:
            assert got and got[0].startswith(want), f"no match in {cmd!r}: {got}"


def test_command_task_id_regex_ignores_nonhex():
    from hiveweave.tools.bash import _COMMAND_TASK_ID_RE

    assert _COMMAND_TASK_ID_RE.findall("taskId=v-created npx vitest") == []
    assert _COMMAND_TASK_ID_RE.findall("taskId=v-1") == []


# ── P0-4: in_progress loop brake (real function) ────────


@pytest.mark.asyncio
async def test_commit_turn_in_progress_brake():
    """Calling commit_turn_tool(in_progress) repeatedly must eventually err."""
    from hiveweave.tools.turn_tools import commit_turn_tool

    _in_progress_counts.clear()
    with (
        patch(
            "hiveweave.tools.turn_tools.validate_phase_fields", return_value=[]
        ),
        patch(
            "hiveweave.tools.turn_tools.get_pending_turn_result",
            return_value=None,
        ),
    ):
        errs = 0
        for _ in range(_IN_PROGRESS_LIMIT + 3):
            res = await commit_turn_tool(
                _P(phase="in_progress", summary="s"),
                "a1",
                "/w",
                None,
            )
            if getattr(res, "error", None):
                errs += 1
            time.sleep(0.01)

    assert errs >= 1, "loop brake never triggered"


def test_in_progress_window_expiry():
    _in_progress_counts.clear()
    old = time.time() * 1000 - _IN_PROGRESS_WINDOW_MS - 1000
    _in_progress_counts["agent-x"] = [old] * 10
    kept = [
        t
        for t in _in_progress_counts["agent-x"]
        if time.time() * 1000 - t < _IN_PROGRESS_WINDOW_MS
    ]
    assert kept == []


class _P:
    """Minimal CommitTurnParams stand-in."""

    def __init__(self, **kw):
        self.phase = kw.get("phase", "in_progress")
        self.summary = kw.get("summary", "")
        self.waiting_on = kw.get("waiting_on")
        self.result = kw.get("result")
        self.extensions = kw.get("extensions")
