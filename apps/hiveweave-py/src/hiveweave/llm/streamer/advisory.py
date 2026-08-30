"""Advisory repeat-tool guard (F8 — 平台修复计划 2026-08-30).

背景（r4 #8）：平台已有硬掐断（tool-loop consecutive failures, limit=2），
但有硬门、无软提醒 —— 自然语言劝阻（「不要原地空转或反复重试同一审批」）
已被证明无效（仍重试 6 次）。照搬 DSH ``packages/guard/repeat-tool-reminder``
的设计：**阈值 3 / 5 / 8 次重复时给建议，永不阻断（advice, never a block）**。

与既有硬门的区别：
- 硬门（doom_loop / stall）在阈值触顶时收口整个 turn；
- advisory 只在下一次 LLM 请求前注入一句提醒，Turn 继续跑。
- 计数按 (agent_id) 隔离；同 run 内同一 (tool, canonical args) 计数；
  新 user message / 新 run 会清空（对齐 DSH「new user prompt resets」）。

提醒内容附上**上次失败的归因**（来自 F4 事实位：runner_failed /
command_failed / blocked），让模型知道「是墙的问题还是自己的问题」——
这正是 advisory 与裸劝阻的本质差别。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: 3 / 5 / 8 触发提醒（DSH repeat-tool-reminder 默认阈值）。
ADVISORY_THRESHOLDS = (3, 5, 8)

#: 每 agent 的失败计数缓存上限（防模型刷 args 差异撑爆内存）。
_MAX_COUNTERS = 4096


def canonical_tool_args(args: Any) -> str:
    """Canonical fingerprint for tool arguments（key 顺序无关）。"""
    if isinstance(args, dict):
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return repr(args)
    if isinstance(args, str):
        try:
            obj = json.loads(args)
            if isinstance(obj, dict):
                return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
            return args
        except Exception:
            return args
    return repr(args)


class AdvisoryGuard:
    """Per-agent repeat-tool-failure counter → advisory reminder text."""

    def __init__(self) -> None:
        # agent_id -> {key: count}；key = f"{tool_name}::<hash(canonical_args)>"
        self._counters: dict[str, dict[str, int]] = {}

    def reset_agent(self, agent_id: str) -> None:
        self._counters.pop(agent_id, None)

    def reset_for_user_message(self, agent_id: str) -> None:
        """新 user prompt 重置计数（对齐 DSH：new prompt resets the chain）。"""
        self.reset_agent(agent_id)

    def record_failure(
        self,
        agent_id: str,
        tool_name: str,
        arguments: Any,
        attribution: str,
    ) -> str | None:
        """Record one tool failure; return advisory text when threshold hit.

        ``attribution`` 来自 F4 事实位生成的短归因描述（如
        "runner_failed: 命令未执行" / "command_failed: 命令执行了但失败"），
        无则传空串（提醒只给事实，不臆断）。
        """
        key = f"{tool_name}::{canonical_tool_args(arguments)}"
        per = self._counters.setdefault(agent_id, {})
        if len(per) > _MAX_COUNTERS:
            per.clear()
        n = per.get(key, 0) + 1
        per[key] = n
        if n in ADVISORY_THRESHOLDS:
            return self._build_reminder(tool_name, n, attribution)
        return None

    @staticmethod
    def _build_reminder(tool_name: str, count: int, attribution: str) -> str:
        verb = "again" if count > 3 else "yet again"
        detail = (
            f" Failure attribution: {attribution}" if attribution else ""
        )
        return (
            f"[ADVISORY] `{tool_name}` has now failed {count} consecutive "
            f"times with the same arguments ({verb}). This is a reminder, "
            f"not a block — but repeating the identical call rarely changes "
            f"the outcome.{detail} Read the last error carefully, change the "
            f"approach (different args, different tool, or a different "
            f"sub-goal), or conclude via commit_turn."
        )


# 全局单例（跨 tool_loop 实例共享 per-agent 计数，run 间不丢）。
advisory_guard = AdvisoryGuard()