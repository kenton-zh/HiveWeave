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

This module is the SINGLE spawn funnel for the whole repo (mirrors upstream
opencode's ``util/process.ts`` + ``cross-spawn-spawner.ts`` pattern): business
code MUST NOT call ``subprocess.*`` / ``asyncio.create_subprocess_*`` directly
— always go through ``hidden_run`` / ``hidden_popen`` / ``hidden_exec`` /
``hidden_shell`` below. ``tests/test_spawn_funnel_guard.py`` enforces this.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# Re-exports so business code never needs ``import subprocess`` itself
# (tests/test_spawn_funnel_guard.py bans the import outside this module).
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
Popen = subprocess.Popen  # 仅注解用；构造请走 hidden_popen
list2cmdline = subprocess.list2cmdline
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)


def _hidden_startupinfo() -> Any:
    """STARTUPINFO with a hidden console window (Windows only)."""
    si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
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

def _with_hidden_startupinfo(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject hidden-console startupinfo unless the caller supplied its own."""
    if not sys.platform.startswith("win"):
        return kwargs
    if kwargs.get("startupinfo") is not None:
        return kwargs
    out = dict(kwargs)
    out["startupinfo"] = _hidden_startupinfo()
    return out


def hidden_run(*args: Any, **kwargs: Any) -> Any:
    """subprocess.run with forced hidden console on Windows."""
    import subprocess

    return subprocess.run(*args, **_with_hidden_startupinfo(kwargs))


def hidden_popen(*args: Any, **kwargs: Any) -> Any:
    """subprocess.Popen with forced hidden console on Windows."""
    import subprocess

    return subprocess.Popen(*args, **_with_hidden_startupinfo(kwargs))


async def hidden_exec(*args: Any, **kwargs: Any) -> Any:
    """asyncio.create_subprocess_exec with forced hidden console on Windows."""
    import asyncio

    return await asyncio.create_subprocess_exec(*args, **_with_hidden_startupinfo(kwargs))


async def hidden_shell(*args: Any, **kwargs: Any) -> Any:
    """asyncio.create_subprocess_shell with forced hidden console on Windows."""
    import asyncio

    return await asyncio.create_subprocess_shell(*args, **_with_hidden_startupinfo(kwargs))
