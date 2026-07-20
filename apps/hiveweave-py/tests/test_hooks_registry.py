"""Lifecycle HookRegistry — OpenCode-style (input, output) chains."""

from __future__ import annotations

import pytest

from hiveweave.hooks import INBOX_TRIAGE_ENRICH, hooks
from hiveweave.hooks.registry import HookRegistry


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear()
    yield
    hooks.clear()


@pytest.mark.asyncio
async def test_run_noop_without_handlers():
    out = {"digest": {"total": 1}}
    await hooks.run(INBOX_TRIAGE_ENRICH, {"agent_id": "a"}, out)
    assert out["digest"]["total"] == 1


@pytest.mark.asyncio
async def test_handler_mutates_output_in_order():
    @hooks.on(INBOX_TRIAGE_ENRICH, priority=10, name="first")
    async def first(inp, out):
        out["digest"]["n"] = out["digest"].get("n", 0) + 1

    @hooks.on(INBOX_TRIAGE_ENRICH, priority=20, name="second")
    async def second(inp, out):
        out["digest"]["n"] = out["digest"].get("n", 0) + 10

    out = {"digest": {}}
    await hooks.run(INBOX_TRIAGE_ENRICH, {}, out)
    assert out["digest"]["n"] == 11
    assert hooks.list_handlers(INBOX_TRIAGE_ENRICH) == ["first", "second"]


@pytest.mark.asyncio
async def test_fail_open_continues():
    @hooks.on(INBOX_TRIAGE_ENRICH, priority=10, fail="open")
    async def boom(inp, out):
        raise RuntimeError("enrich failed")

    @hooks.on(INBOX_TRIAGE_ENRICH, priority=20, fail="open")
    async def ok(inp, out):
        out["digest"]["ok"] = True

    out = {"digest": {}}
    await hooks.run(INBOX_TRIAGE_ENRICH, {}, out)
    assert out["digest"]["ok"] is True


@pytest.mark.asyncio
async def test_fail_closed_raises():
    from hiveweave.hooks import HookClosedError

    reg = HookRegistry()

    @reg.on("x.gate", fail="closed")
    async def deny(inp, out):
        raise PermissionError("denied")

    with pytest.raises(HookClosedError) as ei:
        await reg.run("x.gate", {}, {})
    assert ei.value.handler == "deny"
    assert isinstance(ei.value.cause, PermissionError)


@pytest.mark.asyncio
async def test_inbox_prepare_invokes_enrich_hook():
    from hiveweave.services.inbox_triage import inbox_triage_service

    @hooks.on(INBOX_TRIAGE_ENRICH, priority=50)
    async def tag(inp, out):
        d = out["digest"]
        d["extensions"] = {"tagged": True}
        d["source"] = "platform+hook"

    msgs = [
        {
            "id": "m1",
            "message": "hello",
            "wake_category": "command",
            "priority": "normal",
            "from_agent_id": "a",
            "created_at": 1,
        }
    ]

    with pytest.MonkeyPatch.context() as mp:
        # Avoid real DB — stub schema + writes
        async def _ok(*_a, **_k):
            return None

        async def _no_batch(*_a, **_k):
            return None

        mp.setattr(
            "hiveweave.services.inbox_triage.ensure_triage_schema",
            _ok,
        )
        mp.setattr(
            "hiveweave.services.inbox_triage.project_db.execute",
            _ok,
        )
        mp.setattr(
            inbox_triage_service,
            "_latest_batch",
            _no_batch,
        )
        dig = await inbox_triage_service.prepare_ready("agent-1", msgs)

    assert dig is not None
    assert dig.get("extensions", {}).get("tagged") is True
    assert dig.get("source") == "platform+hook"
