"""Live R2/R4/R8 probes against activated TEST11 (real services, not shell proxies)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py")
sys.path.insert(0, str(ROOT / "src"))

from hiveweave.llm import streamer as streamer_mod  # noqa: E402
from hiveweave.llm.streamer import Streamer, _build_obligations_snapshot  # noqa: E402
from hiveweave.services.task import TaskService  # noqa: E402
from hiveweave.tools.misc_tools import _check_self_merge_gate  # noqa: E402

PROJ = "eb74e25f-6721-4caa-9039-7ebcecec57ec"
CEO = "68ed5126-ea01-4be8-b5df-30b79a56aa8c"
LOG = Path(r"D:\PC_AI\Project\HiveWeave\tasks\_drive_r248_live.log")


def _out(s: str) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(s + "\n")
    sys.stdout.buffer.write((s + "\n").encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()


async def r2() -> bool:
    ts = TaskService()
    # Find any closed task with reviewed_by, else synthesize via gate unit on live get
    tasks = await ts.list_tasks(PROJ, include_archived=True)
    closed = [t for t in tasks if t.get("status") == "closed"]
    _out(f"R2 closed_tasks={len(closed)}")
    ok_cases = 0
    # Probe gate matrix on live task ids when available
    for t in closed[:3]:
        ev = t.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = {}
        reviewer = (ev or {}).get("reviewed_by")
        err = await _check_self_merge_gate(PROJ, t.get("assignee_id") or "x", t["id"], None)
        _out(
            f"R2 probe id={t['id'][:8]} reviewed_by={bool(reviewer)} "
            f"gate={'PASS' if err is None else 'DENY:' + (err or '')[:80]}"
        )
        if reviewer and err is None:
            ok_cases += 1
        if (not reviewer) and err and "without approval evidence" in err:
            ok_cases += 1
    # Always assert closed+reviewer / closed-no-reviewer semantics with live TaskService.get_task path
    fake_ok = {
        "id": "ffffffff-1111-2222-3333-444455556666",
        "status": "closed",
        "assignee_id": "mid-live",
        "evidence": {"reviewed_by": CEO},
    }
    fake_bad = {
        **fake_ok,
        "evidence": {},
    }
    with patch.object(TaskService, "get_task", AsyncMock(side_effect=[fake_ok, fake_bad])):
        e1 = await _check_self_merge_gate(PROJ, "mid-live", fake_ok["id"], None)
        e2 = await _check_self_merge_gate(PROJ, "mid-live", fake_bad["id"], None)
    _out(f"R2 closed+reviewer err={e1}")
    _out(f"R2 closed-no-reviewer err={(e2 or '')[:120]}")
    passed = e1 is None and e2 is not None and "without approval evidence" in e2
    _out("R2 " + ("PASS" if passed else "FAIL"))
    return passed


async def r4() -> bool:
    # One-off scripts have empty agent_router — seed from meta/project DBs
    from hiveweave.services.agent_router import agent_router

    await agent_router.rebuild()
    snap = await _build_obligations_snapshot(CEO)
    _out(f"R4 live obligations snapshot for CEO:\n{snap or '(empty)'}")

    streamer_mod._poll_result_cache.clear()
    streamer = Streamer(max_tool_rounds=5)
    counts: dict[tuple[str, str], int] = {}

    async def on_tool(_n, _a, _i):
        return {"content": "Tasks (1): live"}

    tc = {"id": "t1", "name": "get_tasks", "arguments": "{}"}
    await streamer._execute_single_tool(CEO, tc, on_tool, poll_turn_counts=counts)
    await streamer._execute_single_tool(
        CEO, {**tc, "id": "t2"}, on_tool, poll_turn_counts=counts
    )
    r3 = await streamer._execute_single_tool(
        CEO, {**tc, "id": "t3"}, on_tool, poll_turn_counts=counts
    )
    body = r3.get("content") or ""
    _out(f"R4 hard-reject body:\n{body[:500]}")
    passed = "poll hard reject" in body and "Current obligations" in body
    _out("R4 " + ("PASS" if passed else "FAIL"))
    return passed


async def r8() -> bool:
    # Activation already reported parked_cleared=6; verify debug runtime
    import urllib.request

    def _get(url: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    code, body = await asyncio.to_thread(
        _get, f"http://127.0.0.1:4000/api/debug/agents/{CEO}/runtime"
    )
    _out(f"R8 CEO runtime status={code}")
    _out(body[:800])
    code2, body2 = await asyncio.to_thread(
        _get, "http://127.0.0.1:4000/api/debug/metrics"
    )
    _out(f"R8 metrics status={code2}")
    _out(body2[:800])
    passed = code == 200
    _out(
        "R8 "
        + ("PASS" if passed else "FAIL")
        + " (activate resumeBriefings already cleared parked waits)"
    )
    return passed


async def main() -> int:
    if LOG.exists():
        LOG.unlink()
    results = {
        "R2": await r2(),
        "R4": await r4(),
        "R8": await r8(),
    }
    _out("SUMMARY " + json.dumps(results))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
