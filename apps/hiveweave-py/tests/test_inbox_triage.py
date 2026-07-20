"""Inbox triage — platform digest + ready batch (TEST4 follow-up)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services.inbox_triage import (
    ORDER_HINT,
    build_platform_digest,
    derive_wake_category,
    format_digest_block,
    inbox_triage_service,
    needs_message_detail,
)


def _msg(
    mid: str,
    *,
    content: str,
    category: str = "command",
    priority: str = "normal",
    from_id: str = "a1",
    task_id: str | None = None,
    expect_report: bool = False,
    created_at: int = 1000,
) -> dict:
    return {
        "id": mid,
        "message": content,
        "wake_category": category,
        "message_type": "normal",
        "priority": priority,
        "from_agent_id": from_id,
        "task_id": task_id,
        "expect_report": expect_report,
        "created_at": created_at,
    }


def test_digest_order_ask_before_progress():
    msgs = [
        _msg("m1", content="FYI done", category="progress", created_at=1),
        _msg(
            "m2",
            content="请确认接口",
            category="ask",
            expect_report=True,
            created_at=2,
        ),
        _msg(
            "m3",
            content="[TASK SUBMITTED] x",
            category="task_transition",
            task_id="task-abc",
            created_at=3,
        ),
    ]
    dig = build_platform_digest(msgs, name_by_id={"a1": "潮汐"})
    assert dig["order_hint"] == ORDER_HINT
    assert dig["counts"]["ask"] == 1
    assert dig["counts"]["progress"] == 1
    assert dig["counts"]["task_transition"] == 1
    cats = [it["category"] for it in dig["items"]]
    assert cats[0] == "ask"
    assert "task_transition" in cats[:2]
    assert cats[-1] == "progress"


def test_digest_folds_duplicate_progress():
    msgs = [
        _msg("m1", content="全部完成", category="progress", from_id="x"),
        _msg("m2", content="全部完成", category="progress", from_id="x"),
        _msg("m3", content="请回复", category="ask", expect_report=True),
    ]
    dig = build_platform_digest(msgs)
    assert len(dig["folded_ids"]) == 1
    assert dig["total"] == 3
    assert len(dig["items"]) == 2  # ask + one progress


def test_format_digest_block_contains_counts():
    dig = build_platform_digest(
        [_msg("m1", content="hi", category="command", priority="urgent")]
    )
    block = format_digest_block(dig)
    assert "## Inbox digest (platform)" in block
    assert "command:1" in block
    assert "urgent:1" in block
    assert "order_hint:" in block


def test_order_messages_by_digest():
    msgs = [
        _msg("m1", content="p", category="progress"),
        _msg("m2", content="a", category="ask"),
    ]
    dig = build_platform_digest(msgs)
    ordered = inbox_triage_service.order_messages_by_digest(msgs, dig)
    assert ordered[0]["id"] == "m2"


@pytest.mark.asyncio
async def test_prepare_ready_reuses_same_set():
    msgs = [_msg("m1", content="x", category="command")]
    batch_row = {
        "id": "batch-1",
        "status": "ready",
        "digest_json": json.dumps(
            {
                **build_platform_digest(msgs),
                "message_ids": ["m1"],
            }
        ),
        "created_at": 1,
    }

    with (
        patch(
            "hiveweave.services.inbox_triage.ensure_triage_schema",
            new_callable=AsyncMock,
        ),
        patch.object(
            inbox_triage_service,
            "_latest_batch",
            new_callable=AsyncMock,
            return_value=batch_row,
        ),
    ):
        dig = await inbox_triage_service.prepare_ready("agent-1", msgs)
    assert dig is not None
    assert dig["_batch_id"] == "batch-1"
    assert dig["_status"] == "ready"


@pytest.mark.asyncio
async def test_prepare_ready_skips_when_running():
    import time

    now = int(time.time() * 1000)
    with (
        patch(
            "hiveweave.services.inbox_triage.ensure_triage_schema",
            new_callable=AsyncMock,
        ),
        patch.object(
            inbox_triage_service,
            "_latest_batch",
            new_callable=AsyncMock,
            return_value={
                "id": "batch-run",
                "status": "running",
                "digest_json": None,
                "created_at": now,
            },
        ),
    ):
        dig = await inbox_triage_service.prepare_ready(
            "agent-1",
            [_msg("m1", content="x", category="command")],
        )
    assert dig is None


def test_derive_wake_category_picks_highest():
    assert (
        derive_wake_category(
            [
                _msg("m1", content="p", category="progress"),
                _msg("m2", content="t", category="task_transition"),
                _msg("m3", content="a", category="ask", expect_report=True),
            ]
        )
        == "ask"
    )
    assert derive_wake_category([]) is None


def test_needs_message_detail_skips_progress_when_digest():
    assert needs_message_detail("progress", has_digest=True) is False
    assert needs_message_detail("command", has_digest=True) is False
    assert needs_message_detail("ask", has_digest=True) is True
    assert needs_message_detail("task_transition", has_digest=True) is True
    assert needs_message_detail("approval", has_digest=True) is True
    assert (
        needs_message_detail(
            "command",
            {"expect_report": True},
            has_digest=True,
        )
        is True
    )
    # No digest → keep full detail path
    assert needs_message_detail("progress", has_digest=False) is True


@pytest.mark.asyncio
async def test_prepare_ready_expires_stale_running():
    import time

    stale = int(time.time() * 1000) - 120_000  # past RUNNING_TTL_MS
    msgs = [_msg("m1", content="x", category="command")]
    executed: list[tuple] = []

    async def _exec(agent_id, sql, params=None):
        executed.append((sql, params))
        return None

    with (
        patch(
            "hiveweave.services.inbox_triage.ensure_triage_schema",
            new_callable=AsyncMock,
        ),
        patch(
            "hiveweave.services.inbox_triage.project_db.execute",
            side_effect=_exec,
        ),
        patch.object(
            inbox_triage_service,
            "_latest_batch",
            new_callable=AsyncMock,
            return_value={
                "id": "old-run",
                "status": "running",
                "digest_json": None,
                "created_at": stale,
            },
        ),
    ):
        dig = await inbox_triage_service.prepare_ready("agent-1", msgs)

    assert dig is not None
    assert dig["_status"] == "ready"
    expire_calls = [
        p
        for sql, p in executed
        if sql and "status = ?" in sql and p and p[0] == "expired"
    ]
    assert expire_calls, "stale running batch should be marked expired"


@pytest.mark.asyncio
async def test_prepare_ready_fail_closed_returns_none():
    from hiveweave.hooks import INBOX_TRIAGE_ENRICH, hooks

    hooks.clear()

    @hooks.on(INBOX_TRIAGE_ENRICH, fail="closed", name="deny")
    async def deny(inp, out):
        raise PermissionError("no enrich")

    msgs = [_msg("m1", content="x", category="command")]
    try:
        with (
            patch(
                "hiveweave.services.inbox_triage.ensure_triage_schema",
                new_callable=AsyncMock,
            ),
            patch(
                "hiveweave.services.inbox_triage.project_db.execute",
                new_callable=AsyncMock,
            ),
            patch.object(
                inbox_triage_service,
                "_latest_batch",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            dig = await inbox_triage_service.prepare_ready("agent-1", msgs)
        assert dig is None
    finally:
        hooks.clear()


@pytest.mark.asyncio
async def test_prepare_ready_sets_truncated_flag():
    msgs = [_msg("m1", content="x", category="command")]
    with (
        patch(
            "hiveweave.services.inbox_triage.ensure_triage_schema",
            new_callable=AsyncMock,
        ),
        patch(
            "hiveweave.services.inbox_triage.project_db.execute",
            new_callable=AsyncMock,
        ),
        patch.object(
            inbox_triage_service,
            "_latest_batch",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        dig = await inbox_triage_service.prepare_ready(
            "agent-1",
            msgs,
            truncated=True,
            total_unread=100,
        )
    assert dig is not None
    assert dig.get("truncated") is True
    assert dig.get("total_unread") == 100
    assert "truncated" in (dig.get("instruction") or "").lower()
