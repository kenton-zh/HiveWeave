"""Windows subprocess helpers — suppress console flash (SW_HIDE startupinfo).

Why ``STARTUPINFO.wShowWindow = SW_HIDE`` and NOT ``CREATE_NO_WINDOW``:

- ``CREATE_NO_WINDOW`` gives the direct child (e.g. ``cmd.exe``) NO console at
  all. When that child then spawns a console-subsystem grandchild (``node.exe``,
  ``bun.exe``, ``npx``/``.cmd`` shims, ``git.exe``), Windows allocates a BRAND
  NEW visible console for the grandchild — the flashing/persistent black
  console window users see (a long-lived dev server keeps it open forever).
- With ``STARTF_USESHOWWINDOW | SW_HIDE`` the direct child gets a hidden
  console (or attaches to the parent's existing console); console grandchildren
  INHERIT that hidden console, so no window ever appears for the whole process
  tree.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def _hidden_startupinfo() -> Any:
    """STARTUPINFO with a hidden console window (Windows only)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def windows_no_window_kwargs() -> dict[str, Any]:
    """Kwargs for subprocess.Popen / asyncio.create_subprocess_* on Windows.

    Without a hidden console, every ``cmd /c``, ``git``, ``npm``, ``bun``,
    ``browse.exe`` flashes a black console window when agents run tools.
    """
    if not sys.platform.startswith("win"):
        return {}
    return {"startupinfo": _hidden_startupinfo()}


def merge_creationflags(*flags: int) -> int:
    """OR Windows creation flags (no-op on non-Windows).

    Deliberately does NOT add ``CREATE_NO_WINDOW``: it only hides the direct
    child while forcing console grandchildren to allocate new visible console
    windows. Window suppression is handled by ``windows_no_window_kwargs``
    (SW_HIDE startupinfo) instead.
    """
    if not sys.platform.startswith("win"):
        return 0
    out = 0
    for f in flags:
        out |= int(f or 0)
    return out
