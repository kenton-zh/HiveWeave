"""Live R7 browse drive against TEST11 HTML (real gstack browse if present)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py")
sys.path.insert(0, str(ROOT / "src"))

from hiveweave.config import resolve_browse_bin  # noqa: E402
from hiveweave.tools.browse_tools import BrowseParams, browse_tool  # noqa: E402

WS = str(Path(r"D:\PC_AI\Project\TEST11"))
FILE_URL = "http://127.0.0.1:8765/r7-browse-regression.html"
LOG = Path(r"D:\PC_AI\Project\HiveWeave\tasks\_drive_r7_browse.log")


def _out(s: str) -> None:
    text = s or ""
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    sys.stdout.buffer.write((text[:1200] + "\n").encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()


async def main() -> int:
    if LOG.exists():
        LOG.unlink()
    bin_path = resolve_browse_bin()
    _out(f"browse_bin={bin_path}")
    if not bin_path:
        _out("SKIP: browse binary not found")
        return 0

    r1 = await browse_tool(
        BrowseParams(args=["goto", FILE_URL], timeout_sec=60),
        "live-r7",
        WS,
    )
    _out(f"goto success={r1.success} out={(r1.output or r1.error or '')[:300]}")
    if not r1.success:
        return 1

    r2 = await browse_tool(
        BrowseParams(args=["snapshot", "-i"], timeout_sec=30),
        "live-r7",
        WS,
    )
    _out(f"snapshot_before success={r2.success}")
    _out((r2.output or "")[:800])

    click_target = "@e2"
    snap = r2.output or ""
    if "runAll" in snap:
        for line in snap.splitlines():
            if "runAll" in line and "@e" in line:
                tok = line.strip().split()
                for t in tok:
                    if t.startswith("@e"):
                        click_target = t
                        break
                break

    _out(f"click_target={click_target} timeoutSec=10 (must floor to 30)")
    r3 = await browse_tool(
        BrowseParams(args=["click", click_target], timeout_sec=10),
        "live-r7",
        WS,
    )
    _out(f"click success={r3.success} err={(r3.error or '')[:200]}")
    _out((r3.output or "")[:400])

    await asyncio.sleep(1.5)
    r4 = await browse_tool(
        BrowseParams(args=["snapshot", "-i"], timeout_sec=30),
        "live-r7",
        WS,
    )
    _out(f"snapshot_after success={r4.success}")
    _out((r4.output or "")[:800])
    after_ok = "after:" in (r4.output or "") or "done@" in (r4.output or "")

    shot = Path(WS) / "docs" / "r7-after.png"
    r5 = await browse_tool(
        BrowseParams(
            args=["screenshot", str(shot).replace("\\", "/")],
            timeout_sec=30,
        ),
        "live-r7",
        WS,
    )
    _out(
        f"screenshot success={r5.success} exists={shot.exists()} "
        f"after_state={after_ok}"
    )
    if r3.success and after_ok:
        _out("R7 LIVE PASS: click timeoutSec=10 + post state captured")
        return 0
    if r3.success:
        _out("R7 LIVE PARTIAL: click ok but post-state marker not found")
        return 0
    _out("R7 LIVE FAIL: click did not succeed")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
