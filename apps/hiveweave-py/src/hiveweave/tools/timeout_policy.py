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
# F7（平台修复计划 2026-08-30）：统一三档 —— 30s（快/网络）/ 120s（中）/
# 600s（重）。此前 15/90/120/200/240/500 多档并存、按出现顺序随手定值，
# 超时错误不可机检分类。三档语义：
#   30s   —— 短查询 / 网络（websearch、webfetch）
#   120s  —— 典型工具面（browse、question、生成类）
#   600s  —— 重计算 / 严格上限（game 用例、超长 question）
DECLARED_TIMEOUT_S: dict[str, float] = {
    "webfetch": 30.0,
    "websearch": 30.0,
    "browse": 120.0,
    "browse_main": 120.0,
    "question": 600.0,
    "generate_image": 120.0,
    "game_run_case": 600.0,
    "game_run_case_main": 600.0,
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
