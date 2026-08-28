"""Per-tool cooperative timeout.

Only tools that *declare* a budget get a streamer ``wait_for``. bash / read /
write / edit / apply_patch deliberately declare none: no session wrap, no
blanket 120s. Foreground bash uses its own ``timeout`` param; background bash
uses 0 (run until done, ``job_kill``, or cancel/reap).

The deadline only *notifies* via ``asyncio.wait_for``; the tool still has to
stop (httpx timeout, bash process kill). Undeclared tools are not wrapped.
"""
from __future__ import annotations

# Seconds. Absent name → no streamer-side deadline.
DECLARED_TIMEOUT_S: dict[str, float] = {
    "webfetch": 30.0,
    "websearch": 15.0,
    "browse": 90.0,
    "browse_main": 90.0,
    "question": 200.0,
    "generate_image": 120.0,
    "game_run_case": 120.0,
    "game_run_case_main": 120.0,
}

# Hang net while an undeclared tool is in-flight (zombie quiet cap).
# Not a streamer wait_for and not a session wall clock.
UNDECLARED_ACTIVE_QUIET_S = 1800.0

# File / shell / patch: no ToolDefinition timeout.
UNDECLARED_SESSION_TOOLS: frozenset[str] = frozenset({
    "bash",
    "bash_main",
    "pwsh",
    "run_command",
    "read_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "spawn_subagent",
})


def declared_timeout_s(tool_name: str) -> float | None:
    """Return the cooperative budget in seconds, or None if undeclared."""
    name = (tool_name or "").strip()
    if not name:
        return None
    return DECLARED_TIMEOUT_S.get(name)
