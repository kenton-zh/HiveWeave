"""TEST9 P1: restart must recover agent_waits timeout wakeups.

game_time._process_wait_contracts (timeout sweep + [WAIT_TIMEOUT] + wake)
only runs inside the per-project tick loop.  Restart clears is_started, so
until someone starts the project again no tick runs and expired waits never
fire.  recover_wait_timeouts() must, on startup/recovery:

- immediately process waits whose expires_at is already past
  (clear + [WAIT_TIMEOUT] inbox + watchdog wake), and
- arm one-shot timers for still-pending waits so their timeout fires even
  while the project tick loop is not running.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import hiveweave.services.game_time as game_time_mod
import hiveweave.services.wait_contract as wait_mod
import hiveweave.services.inbox as inbox_mod
from hiveweave.services.game_time import GameTimeService
from hiveweave.services.wait_contract import WaitContractService
from tests.test_idle_architecture_p0 import COORD, EXEC, task_env  # noqa: F401


@pytest.fixture(autouse=True)
def _clear_migrated():
    wait_mod._migrated.clear()
    inbox_mod._migrated.clear()
    yield
    wait_mod._migrated.clear()
    inbox_mod._migrated.clear()


@pytest.fixture(autouse=True)
def _cleanup_recovery(task_env):
    pid = task_env["project_id"]
    yield
    svc = GameTimeService(pid)
    svc.cancel_wait_recovery_timers(pid)
    game_time_mod._states.pop(pid, None)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def test_recovery_processes_already_expired_wait(task_env, monkeypatch):
    """Expired wait must be cleared + [WAIT_TIMEOUT] sent + wake triggered."""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    await wc.replace_waits(
        pid, EXEC, [{"kind": "agent", "ref": COORD, "expires_at": _now_ms() - 60_000}],
        phase="waiting",
    )

    send = AsyncMock(return_value="msg-x")
    trigger = AsyncMock()
    monkeypatch.setattr("hiveweave.services.inbox.InboxService.send_message", send)
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", trigger)

    handled = await svc.recover_wait_timeouts(pid)

    assert handled["expired_processed"] is True
    assert handled["armed"] == 0
    # Wait cleared and timeout notice sent to the waiting agent.
    assert await wc.list_all_active(pid) == []
    send.assert_awaited_once()
    kw = send.await_args.kwargs
    assert kw["to_agent_id"] == EXEC and kw["message_type"] == "system"
    assert "[WAIT_TIMEOUT]" in kw["message"]
    trigger.assert_awaited_once()  # wake via game-event


async def test_recovery_arms_timer_for_pending_wait(task_env):
    """Still-pending wait: not processed now, but a one-shot timer is armed."""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    await wc.replace_waits(
        pid, EXEC, [{"kind": "agent", "ref": COORD, "expires_at": _now_ms() + 600_000}],
        phase="waiting",
    )

    handled = await svc.recover_wait_timeouts(pid)

    assert handled["expired_processed"] is False
    assert handled["armed"] == 1
    # Wait still active — nothing spuriously fired.
    assert len(await wc.list_all_active(pid)) == 1

    # Idempotent: a second recovery does not double-arm.
    handled2 = await svc.recover_wait_timeouts(pid)
    assert handled2["armed"] == 0

    svc.cancel_wait_recovery_timers(pid)
    assert not game_time_mod._states[pid]["armed_wait_ids"]


async def test_armed_timer_fires_timeout(task_env, monkeypatch):
    """The armed one-shot timer must actually clear the wait and wake."""
    pid = task_env["project_id"]
    svc = GameTimeService(pid)
    wc = WaitContractService()
    await wc.replace_waits(
        pid, EXEC, [{"kind": "agent", "ref": COORD, "expires_at": _now_ms() + 300}],
        phase="waiting",
    )

    send = AsyncMock(return_value="msg-x")
    trigger = AsyncMock()
    monkeypatch.setattr("hiveweave.services.inbox.InboxService.send_message", send)
    monkeypatch.setattr(GameTimeService, "_watchdog_trigger", trigger)

    await svc.recover_wait_timeouts(pid)
    await asyncio.sleep(1.0)

    assert await wc.list_all_active(pid) == []
    send.assert_awaited_once()
    trigger.assert_awaited_once()
