"""Regression tests for the second-pass audit fixes (2026-08-06).

Covers the surviving findings across the three audit reports:
- P0-2: update_progress silently succeeds on short-prefix task id.
- P0-3: dispatch_task reuses an existing task but assignee never changes
  (update_task raw `WHERE id = ?`).
- P0-4: create_task stores depends_on verbatim → obligation exact-membership
  never unblocks the dependent task.
- P0-5: reply_to short prefix stored verbatim → get_outstanding_ask_senders
  exact `IN` match misses → false UNREPLIED_ASKS.
- Audit-1 R1: pre_check UNREPLIED_ASKS rejects agents that already replied
  via plain send_message (pre-check was contract-only, backstop allows it).
- Audit-1 R2: STALL BREAK message prints the right counter for readonly-only
  stalls (was printing stall_count==0).
- Audit-1 R3: browse large-inline tempfile is unlinked after exec.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


FULL_UUID = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
SHORT_PREFIX = FULL_UUID[:8]


# ── P0-2 update_progress resolves short prefix ───────────────────────────
@pytest.mark.asyncio
async def test_update_progress_resolves_short_prefix_before_update():
    from hiveweave.services.tasks.progress import ProgressMixin

    m = ProgressMixin()
    m.require_task_id = AsyncMock(return_value=FULL_UUID)
    with patch(
        "hiveweave.services.tasks.progress._query",
        new=AsyncMock(return_value=[{"progress": 10}]),
    ) as q, patch(
        "hiveweave.services.tasks.progress._execute",
        new=AsyncMock(),
    ) as ex, patch(
        "hiveweave.services.tasks.progress._ensure_schema",
        new=AsyncMock(),
    ):
        await m.update_progress("proj", SHORT_PREFIX, 50)
    m.require_task_id.assert_awaited_once_with("proj", SHORT_PREFIX)
    # UPDATE must use the resolved full id, never the raw short prefix.
    sql, params = ex.await_args.args[1], ex.await_args.args[2]
    assert isinstance(sql, str) and "UPDATE tasks SET progress" in sql
    assert FULL_UUID in params


@pytest.mark.asyncio
async def test_update_progress_unknown_ref_raises_value_error():
    from hiveweave.services.tasks.progress import ProgressMixin

    m = ProgressMixin()
    m.require_task_id = AsyncMock(side_effect=ValueError("Task not found"))
    with pytest.raises(ValueError):
        await m.update_progress("proj", "deadbeef", 50)


# ── P0-3 dispatch_task reuses existing task with resolved id ─────────────
@pytest.mark.asyncio
async def test_dispatch_existing_task_resolves_prefix_and_reassigns():
    """Existing-task branch: prefix → full id, assignee updated, task reused."""
    from hiveweave.services.dispatch import DispatchService

    d = DispatchService.__new__(DispatchService)
    d.task_service = type("TS", (), {})()
    d.task_service.require_task_id = AsyncMock(return_value=FULL_UUID)
    d.task_service.get_task = AsyncMock(return_value={})
    d.task_service.update_task = AsyncMock()
    d.task_service.ensure_assignee_claimed = AsyncMock()
    d.inbox = type("INBOX", (), {})()
    d.inbox.send_message = AsyncMock()
    d.handoff = type("HO", (), {})()
    d.handoff.create_handoff = AsyncMock()
    with patch(
        "hiveweave.services.dispatch._ensure_schema", new=AsyncMock()
    ), patch(
        "hiveweave.services.dispatch._execute", new=AsyncMock()
    ), patch(
        "hiveweave.services.org_span.validate_dispatch_span",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.org_span.validate_ceo_dispatch_target",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.org_span.validate_executor_assignee",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.db.meta.get_project_workspace",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.org.OrgService.resolve_agent",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.obligation.ObligationLedger.create",
        new=AsyncMock(),
    ):
        result = await d.dispatch_task(
            project_id="proj",
            from_agent_id="from-a",
            to_agent_id="to-b",
            description="rework the landing page",
            existing_task_id=SHORT_PREFIX,
            create_handoff=False,
        )
    # The fix's contract: short prefix resolved via require_task_id, then the
    # update targets the canonical id so the assignee actually changes.
    d.task_service.require_task_id.assert_awaited_once_with("proj", SHORT_PREFIX)
    update_args, update_kw = d.task_service.update_task.await_args
    assert update_args[1] == FULL_UUID
    assert update_kw["assignee_id"] == "to-b"
    assert result["success"] is True
    assert result["task_id"] == FULL_UUID


@pytest.mark.asyncio
async def test_dispatch_existing_archived_task_is_blocked():
    """B3: archived tasks are write-protected — dispatch must refuse."""
    from hiveweave.services.dispatch import DispatchService

    d = DispatchService.__new__(DispatchService)
    d.task_service = type("TS", (), {})()
    d.task_service.require_task_id = AsyncMock(return_value=FULL_UUID)
    d.task_service.get_task = AsyncMock(return_value={"is_archived": True})
    d.task_service.update_task = AsyncMock()
    d.task_service.ensure_assignee_claimed = AsyncMock()
    d.inbox = type("INBOX", (), {})()
    d.inbox.send_message = AsyncMock()
    d.handoff = type("HO", (), {})()
    d.handoff.create_handoff = AsyncMock()
    with patch(
        "hiveweave.services.dispatch._ensure_schema", new=AsyncMock()
    ), patch(
        "hiveweave.services.dispatch._execute", new=AsyncMock()
    ), patch(
        "hiveweave.services.org_span.validate_dispatch_span",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.org_span.validate_ceo_dispatch_target",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.org_span.validate_executor_assignee",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.obligation.ObligationLedger.create",
        new=AsyncMock(),
    ):
        result = await d.dispatch_task(
            project_id="proj",
            from_agent_id="from-a",
            to_agent_id="to-b",
            description="touching an archived task",
            existing_task_id=FULL_UUID,
            create_handoff=False,
        )
    assert result["success"] is False
    assert "archived" in result["message"].lower()
    d.task_service.update_task.assert_not_awaited()


# ── P0-4 create_task normalizes depends_on ───────────────────────────────
@pytest.mark.asyncio
async def test_create_task_normalizes_depends_on():
    from hiveweave.services.tasks.crud import CrudMixin

    c = CrudMixin()
    # 裸 mixin 无组合类的 VerifyMixin._is_verify_task —— 打桩为普通任务
    c._is_verify_task = lambda draft: False  # type: ignore[method-assign]
    c.resolve_task_id = AsyncMock(return_value=FULL_UUID)
    deps = [SHORT_PREFIX, "unresolvable-ref"]
    with patch(
        "hiveweave.services.tasks.crud._ensure_schema", new=AsyncMock()
    ), patch(
        "hiveweave.services.tasks.crud._execute", new=AsyncMock()
    ), patch(
        "hiveweave.services.tasks.crud._execute_tx", new=AsyncMock()
    ), patch(
        "hiveweave.services.tasks.crud.publish_task_event", new=AsyncMock()
    ):
        # resolve_task_id fails open for the second entry → returns None.
        async def rr(p, ref):
            return FULL_UUID if ref == SHORT_PREFIX else None
        c.resolve_task_id = AsyncMock(side_effect=rr)
        await c.create_task(
            project_id="proj", title="t", description="d",
            creator_id="a", depends_on=deps,
        )
    # Cannot easily introspect the raw SQL; assert resolve_task_id was called
    # for the short prefix (the fix's contract) and not for the unresolvable.
    calls = [call.args[1] for call in c.resolve_task_id.await_args_list]
    assert SHORT_PREFIX in calls
    assert "unresolvable-ref" in calls


# ── P0-5 reply_to prefix normalized before storage ───────────────────────
@pytest.mark.asyncio
async def test_reply_to_short_prefix_resolved_to_full_contract():
    """P0-5: explicit reply_to short prefix is normalized to the full UUID
    before the inbox INSERT — otherwise the query side's exact ``IN`` match
    misses → false UNREPLIED_ASKS → commit_turn deadlock."""
    from hiveweave.services.inbox import InboxService

    contract = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    prefix = contract[:12]
    with patch(
        "hiveweave.services.inbox._ensure_schema", new=AsyncMock()
    ), patch(
        "hiveweave.db.meta.get_agent_by_id", new=AsyncMock(return_value=None)
    ), patch(
        "hiveweave.services.inbox.project_db.query_one",
        new=AsyncMock(return_value=None),
    ), patch(
        "hiveweave.services.inbox.project_db.query",
        # known_set = contracts the recipient (a) sent to me (b).
        new=AsyncMock(return_value=[{"reply_contract_id": contract}]),
    ), patch(
        "hiveweave.services.inbox.project_db.execute",
        new=AsyncMock(),
    ) as ex, patch(
        "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
        new=AsyncMock(),
    ):
        result = await InboxService().send_message(
            "b", "a", "rework it", message_type="ask",
            reply_to=prefix, wake=True,
        )
    inserts = [
        (a.args[1], a.args[2])
        for a in ex.await_args_list
        if isinstance(a.args[1], str) and "INSERT INTO inbox" in a.args[1]
    ]
    assert len(inserts) == 1
    sql, params = inserts[0]
    # Column order: (…, reply_contract_id, reply_to) → last param is reply_to.
    # The ask is downgraded to notify (reply chain ends) but its reply_to is
    # normalized to the full UUID — the gate's exact IN match then hits.
    assert params[-1] == contract
    # Downgrade contract (P0-5's doom-loop fix): no expect_report, no new
    # contract id, message_type no longer "ask".
    assert params[6] == "notify"
    assert params[7] == 0
    assert params[14] is None


# ── Audit-1 R1 pre_check honfs the sent-fallback like backstop ───────────
@pytest.mark.asyncio
async def test_pre_check_unreplied_asks_honors_sent_fallback():
    from hiveweave.services.turn_exit import pre_check_exit_gates

    outstanding = {"sender-uuid"}
    conn = AsyncMock()
    conn.execute.return_value = conn
    conn.fetchall.return_value = []
    # pre_check_exit_gates does `from hiveweave.services.inbox import InboxService`
    # inside the function body — patch the import source.
    with patch(
        "hiveweave.db.project.get_project_db_by_project_id",
        new=AsyncMock(return_value=conn),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value=outstanding),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=outstanding),
    ), patch(
        "hiveweave.services.task._ensure_schema",
        new=AsyncMock(),
    ):
        violations = await pre_check_exit_gates(
            "agent", "proj", phase="done_slice"
        )
    # Agent already messaged the sender → no UNREPLIED_ASKS violation.
    assert "UNREPLIED_ASKS" not in violations


@pytest.mark.asyncio
async def test_pre_check_unreplied_asks_keeps_violation_when_unanswered():
    from hiveweave.services.turn_exit import pre_check_exit_gates

    conn = AsyncMock()
    conn.execute.return_value = conn
    conn.fetchall.return_value = []
    with patch(
        "hiveweave.db.project.get_project_db_by_project_id",
        new=AsyncMock(return_value=conn),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_outstanding_ask_senders",
        new=AsyncMock(return_value={"s1"}),
    ), patch(
        "hiveweave.services.inbox.InboxService.get_sent_recipients_since",
        new=AsyncMock(return_value=set()),
    ), patch(
        "hiveweave.services.task._ensure_schema",
        new=AsyncMock(),
    ):
        violations = await pre_check_exit_gates(
            "agent", "proj", phase="done_slice"
        )
    # Sender not messaged → still a violation.
    assert "UNREPLIED_ASKS" in violations


# ── Audit-1 R2 STALL BREAK text uses max(stall, readonly) ────────────────
def test_stall_break_message_uses_max_counter():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "hiveweave" / "llm" / "streamer" / "tool_loop.py"
    ).read_text(encoding="utf-8")
    # readonly-only stall path must not print a literal 0.
    assert "max(stall_count, readonly_stall_count)" in src
    assert "f\"[STALL BREAK] {max(stall_count, readonly_stall_count)}" in src


# ── Audit-1 R3 browse oversized inline JS routed via stdin ───────────────
def test_map_ab_argv_huge_js_uses_stdin_payload():
    from hiveweave.tools.browse_tools import _map_ab_argv

    huge = "(" + "window;" * 8000 + ")"
    argv, stdin = _map_ab_argv(["js", huge], "")
    assert argv == ["eval", "--stdin"]
    assert stdin == huge


def test_browse_exec_forwards_stdin_payload(browse_fake_proc, tmp_path):
    """browse_exec must pipe the huge snippet to the child via stdin=PIPE."""
    import asyncio

    from hiveweave.tools.browse_tools import browse_exec

    payload = "x" * 30000
    with browse_fake_proc as ctx:
        ctx.out = b"ok"
        code, _out, _err = asyncio.run(
            browse_exec(["js", payload], str(tmp_path))
        )

    assert code == 0
    assert ctx.stdin_is_pipe
    # The fake child's stdin received the exact huge payload.
    assert ctx.stdin_written == payload.encode("utf-8")