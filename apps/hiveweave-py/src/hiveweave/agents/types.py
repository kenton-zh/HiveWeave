"""Agent state enum and callback type aliases.

Extracted from agent.py — behavior-preserving mechanical split (P1).
"""

from __future__ import annotations

from enum import Enum
from typing import Awaitable, Callable


class AgentState(Enum):
    """Agent 状态。对齐 Elixir agent.ex 的 %{} state.status 字段。"""

    IDLE = "idle"
    PROCESSING = "processing"


StatusCallback = Callable[[str, str, dict], Awaitable[None] | None]
"""状态变更回调: (agent_id, status, extra) → None。
批次 4 会连接到 WebSocket 广播。"""

StreamEventCallback = Callable[[str, dict], Awaitable[None] | None]
"""流事件回调: (agent_id, event) → None。
批次 4 会连接到 WebSocket 广播。"""
