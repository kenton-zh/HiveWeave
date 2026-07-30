"""Streamer callback type aliases."""
from __future__ import annotations

from typing import Awaitable, Callable

DeltaCallback = Callable[[dict], Awaitable[None] | None]
"""SSE delta 回调。收到 {type:"text_delta", content, ...} 等事件时调用。"""

ToolCallCallback = Callable[[str, str, str], Awaitable[dict]]
"""工具执行回调。

签名: async def callback(tool_name: str, arguments: str, tool_call_id: str) -> dict
返回: {"role": "tool", "content": "...", "tool_call_id": "..."}
"""

