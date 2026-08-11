"""tool.execute.after — code_audit line ledger.

每次成功调用源码写工具，把本次改动行数记入 agent 级账本
（services/code_audit 的 record_change）。submit_task 时若账本行数超阈且
近期无 code_audit 审计凭证，软提醒执行审计（不阻断、不改状态流）。

Fail-open：账本异常绝不影响工具执行 —— 业务逻辑全部包在 try 里吞掉。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from hiveweave.hooks import TOOL_EXECUTE_AFTER, hooks

log = structlog.get_logger(__name__)

_CODE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


async def on_tool_execute_after(
    input: Mapping[str, Any],
    output: MutableMapping[str, Any],
) -> None:
    """Record changed lines for successful code-write tool executions."""
    tool_name = input.get("tool_name")
    if tool_name not in _CODE_WRITE_TOOLS:
        return
    if not input.get("success"):
        return
    agent_id = input.get("agent_id")
    params = input.get("params")
    if not agent_id or not isinstance(params, dict):
        return
    try:
        from hiveweave.services.code_audit import count_change_lines, record_change

        lines = int(count_change_lines(str(tool_name), params) or 0)
        if lines > 0:
            record_change(str(agent_id), lines)
    except Exception as e:  # noqa: BLE001
        log.debug("hook_code_audit_ledger_failed", error=str(e))


def register() -> None:
    hooks.register(
        TOOL_EXECUTE_AFTER,
        on_tool_execute_after,
        priority=25,
        fail="open",
        name="code_audit_ledger",
    )
