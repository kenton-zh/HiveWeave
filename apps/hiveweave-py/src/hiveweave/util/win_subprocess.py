"""Windows subprocess helpers — suppress console flash (CREATE_NO_WINDOW)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def windows_no_window_kwargs() -> dict[str, Any]:
    """Kwargs for subprocess.Popen / asyncio.create_subprocess_* on Windows.

    Without CREATE_NO_WINDOW, every ``cmd /c``, ``git``, ``npm``, ``browse.exe``
    flashes a black console window when agents run tools.
    """
    if not sys.platform.startswith("win"):
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flag}


def merge_creationflags(*flags: int) -> int:
    """OR Windows creation flags (no-op on non-Windows)."""
    if not sys.platform.startswith("win"):
        return 0
    out = 0
    for f in flags:
        out |= int(f or 0)
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return out | no_win
