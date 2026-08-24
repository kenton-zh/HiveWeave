"""Agent timeout / stall / rate-limit constants (契约 04).

Extracted from agent.py — behavior-preserving mechanical split (P1).
"""

from __future__ import annotations

# ── 常量（契约 04）──────────────────────────────────────────

SAFETY_TIMEOUT_MS = 600_000
"""Legacy 10-minute constant (Elixir ``@safety_timeout_ms``).

Not a live-turn kill: ``_start_safety_timer`` is a no-op. Call sites may
still pass this as an unused orphan-sweep argument. Do not treat it as
a session wall clock.
"""

EMPTY_RETRY_DELAYS = [5_000, 15_000, 45_000]
"""空响应退避序列（5s/15s/45s）。契约 04。"""

MAX_EMPTY_RETRIES = 3
"""空响应最大重试次数。超过则升级上级。"""

TRIGGER_DELAY_MS = 100
"""触发前延迟，等 DB 写入落盘。"""

SELF_RETRIGGER_DELAY_MS = 500
"""自检 retrigger 前的延迟。"""

TIMEOUT_RESUME_COOLDOWN_S = 90.0
"""超时/可恢复错误后，禁止立即重触发同一 agent 的冷却时间。

防止「inbox 未 ACK → watcher 立刻再 trigger → 再超时」的 doom loop，
同时保留消息未读，冷却结束后由 watcher / stall watchdog 恢复信息链。
"""

ERROR_RESUME_COOLDOWN_S = 30.0
"""可恢复 LLM 错误后的短冷却。"""

RATE_LIMIT_RESUME_COOLDOWN_S = 120.0
"""429 soft cooldown floor（不计入放弃计数）。"""

RATE_LIMIT_SOFT_MAX_S = 600.0
"""reset ≤ 10min → soft cooldown; beyond → hard quota park."""

RATE_LIMIT_BACKOFF_STEPS_S = (120.0, 600.0, 1800.0)
"""No reset header: exponential soft cooldown; 3rd → escalate + park."""

RATE_LIMIT_SOFT_STREAK_ESCALATE = 3
"""Soft 429s without recovery → escalate superior + park."""

# P0-3: Cross-turn stall break ledger — tracks STALL BREAK events per agent
# across turns. 2nd break within the window → park + escalate to org parent.
STALL_BREAK_WINDOW_MS = 30 * 60 * 1000  # 30 min window for counting breaks
STALL_BREAK_PARK_THRESHOLD = 2           # 2nd break → park + escalate
# TEST21 M6: don't park if a successful run finished within this window
STALL_BREAK_RECENT_OK_MS = 5 * 60 * 1000

DEFAULT_MAX_TOOL_ROUNDS = 600
"""所有角色统一的 tool loop 最大轮次。不再按角色区别对待。"""

# ── 工具结果展示截断（Chat 面板块序列渲染契约）──────────────
# 唯二截断点：落库（metadata.segments）用 PERSIST，流式广播
# （tool_call_end / tool_result 事件）用 STREAM。前端 ToolCallRow
# 再做一次 PERSIST 级二次截断兜底。改阈值须三处同步（本处为唯一来源）。
TOOL_RESULT_PERSIST_EXCERPT = 2000
TOOL_RESULT_STREAM_EXCERPT = 500
