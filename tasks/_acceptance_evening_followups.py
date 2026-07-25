#!/usr/bin/env python3
"""TEST11 evening follow-up acceptance runner.

Covers:
  #3 Soft-warn — first gate hit soft-pass, second hard-reject
  #2 Evidence verifiable — files_changed + acceptance path tokens
  R2 / R4 / R7 / R8 — unit suite + optional live probes

Usage:
  # Offline (always safe):
  uv run python tasks/_acceptance_evening_followups.py

  # Live R probes (requires backend :4000 + TEST11 activated):
  uv run python tasks/_acceptance_evening_followups.py --live

  # Activate TEST11 then live (GET activate):
  uv run python tasks/_acceptance_evening_followups.py --activate --live
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "apps" / "hiveweave-py"
API = "http://127.0.0.1:4000"
TEST11_NAME = "TEST11"


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(cwd or ROOT))
    return int(r.returncode)


async def _find_test11_id() -> str | None:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{API}/api/projects")
        r.raise_for_status()
        data = r.json()
        projects = data.get("projects", data) if isinstance(data, dict) else data
        if not isinstance(projects, list):
            return None
        for p in projects:
            if isinstance(p, dict) and p.get("name") == TEST11_NAME:
                return str(p["id"])
    return None


async def _activate(pid: str) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.get(f"{API}/api/projects/{pid}/activate")
        print(f"activate {pid}: {r.status_code} {r.text[:200]}")
        r.raise_for_status()


def print_checklist() -> None:
    print(
        """
=== TEST11 evening follow-up acceptance checklist ===

[#3 Soft-warn]
  - Same turn, first WAIT_WITHOUT_ASK / UNREPLIED_ASKS => accept WITH SOFT WARNING
  - Same turn, second hit of that code => commit_turn REJECTED
  - evaluate_turn_exit drops soft-passed codes so exit does not re-block
  Prove: pytest tests/test_soft_warn_evidence.py

[#2 Evidence verifiable]
  - review_task(approve) checks evidence.files_changed exist on disk
  - acceptance_criteria path tokens must be in files_changed OR exist on disk
  - No natural-language intent scanning
  Prove: pytest tests/test_soft_warn_evidence.py::test_evidence_*

[R2 closed+reviewer merge gate]
  Prove: pytest tests/test_r248_real_regression.py + tasks/_drive_r248_live.py

[R4 obligations hard-reject]
  Prove: same as R2 live driver

[R7 browse click timeout floor]
  Prove: tasks/_drive_r7_browse.py (needs browse + local HTML server)

[R8 debug runtime/metrics]
  Prove: live driver hits /api/debug/agents/{id}/runtime + /api/debug/metrics

Live policy:
  - Default: offline unit suite only (no agent wake)
  - --live: requires TEST11 already activated (or pass --activate)
"""
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Run live R2/R4/R8 (+ optional R7)")
    ap.add_argument("--activate", action="store_true", help="Activate TEST11 before live")
    ap.add_argument("--skip-unit", action="store_true")
    ap.add_argument("--with-r7", action="store_true", help="Also run browse R7 driver")
    args = ap.parse_args()

    print_checklist()
    rc = 0

    if not args.skip_unit:
        rc |= _run(
            [
                "uv",
                "run",
                "pytest",
                "tests/test_soft_warn_evidence.py",
                "tests/test_acceptance_checklist_evening.py",
                "tests/test_r248_real_regression.py",
                "tests/test_test11_evening_fixes.py",
                "-q",
            ],
            cwd=PY,
        )

    if args.live:
        pid = await _find_test11_id()
        if not pid:
            print("FAIL: TEST11 project not found via /api/projects")
            return 1
        print(f"TEST11 id={pid}")
        if args.activate:
            await _activate(pid)
        else:
            print("(not activating — pass --activate if agents are down)")
        rc |= _run([sys.executable, str(ROOT / "tasks" / "_drive_r248_live.py")])
        if args.with_r7:
            rc |= _run([sys.executable, str(ROOT / "tasks" / "_drive_r7_browse.py")])

    print("\n=== DONE rc=%s ===" % rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
