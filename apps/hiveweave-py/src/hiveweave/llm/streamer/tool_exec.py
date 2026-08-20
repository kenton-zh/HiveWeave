"""Tool execution + doom-loop detection mixin."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from .doom_loop import doom_loop_limit
from .poll import (
    _POLL_HARD_REJECT_LIMIT,
    _POLL_HARD_REJECT_TOOLS,
    _build_obligations_snapshot,
    _poll_cache_get,
    _poll_cache_put,
    _poll_waiting_gate_block_async,
)
from .types import DeltaCallback, ToolCallCallback

log = structlog.get_logger(__name__)


class ToolExecMixin:
    """Tool execution methods for Streamer."""

    if TYPE_CHECKING:
        _fire_delta: Any

    async def _execute_tools(
        self,
        agent_id: str,
        tool_calls: list[dict],
        on_tool_call: ToolCallCallback,
        on_delta: DeltaCallback | None,
        poll_turn_counts: dict[tuple[str, str], int] | None = None,
        budget_s: float | None = None,
    ) -> tuple[list[dict], set[str], set[str], set[str], bool]:
        """执行一批工具调用，返回 (tool result 消息列表, error_ids, blocked_ids, duplicate_ids, end_turn)。

        并行执行独立的工具调用（对齐 Elixir Task.Supervisor.async_nolink）。
        error_ids 保留用于日志/观测（doom 检测已不再使用失败豁免）。
        blocked_ids = error_ids 中标记 blocked 的子集 —— 平台护栏/沙箱/
        权限拒绝（H3），供 stall 检测区分「平台拒环境」与「模型空转」。
        duplicate_ids 标识"同参数已执行过、本次无新效果"的工具调用，供 doom
        tracker 做强制 +1 计数加速触顶。
        end_turn=True 表示本批含已接受的 commit_turn，应硬断工具循环（BUG-3）。
        budget_s 在工具声明了超时时收紧预算（turn 预算写死启用，
        见 constants.py 顶部说明）。
        bash/read/write/edit 不声明，不被 wait_for 包裹。
        """
        counts = poll_turn_counts if poll_turn_counts is not None else {}
        # 广播 tool_use 事件
        for tc in tool_calls:
            await self._fire_delta(on_delta, {
                "type": "tool_use",
                "tool_call_id": tc["id"],
                "tool_name": tc["name"],
                "arguments": tc["arguments"],
            })

        # 并行执行
        tasks = [
            self._execute_single_tool(
                agent_id, tc, on_tool_call, poll_turn_counts=counts,
                budget_s=budget_s,
            )
            for tc in tool_calls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results: list[dict] = []
        error_ids: set[str] = set()
        blocked_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        end_turn = False
        for i, result in enumerate(results):
            tc = tool_calls[i]
            if isinstance(result, BaseException):
                log.error("tool_execution_error",
                          agent_id=agent_id,
                          tool=tc["name"],
                          error=str(result))
                content = f"[Tool Error] {type(result).__name__}: {result}"
                error_ids.add(tc["id"])
            else:
                content = result.get("content", "")
                if (
                    result.get("success") is False
                    or content.startswith(("[Tool Timeout]", "[Tool Error]"))
                    or content.startswith("[poll hard reject]")
                ):
                    error_ids.add(tc["id"])
                # H3: 平台护栏/沙箱/权限拒绝 —— 失败且带 blocked 标记。
                # 既入 error_ids（保持既有失败语义）也独立收 blocked_ids
                # （stall 检测分流用）。
                if result.get("blocked"):
                    blocked_ids.add(tc["id"])
                # duplicate 信号：工具返回 duplicate=True 表示本次调用不会产生
                # 任何新效果（如 commit_turn 同参已接受过）。这是 doom loop 的
                # 强信号，应计入循环检测。
                if result.get("duplicate"):
                    duplicate_ids.add(tc["id"])
                if result.get("end_turn"):
                    end_turn = True
            tool_msg: dict = {
                "role": "tool",
                "content": content,
                "tool_call_id": tc["id"],
            }
            # Multimodal: preserve screenshot pixels for the next LLM round.
            images = None if isinstance(result, BaseException) else result.get("images")
            if images:
                tool_msg["images"] = images
            tool_results.append(tool_msg)
            # 广播 tool_result
            await self._fire_delta(on_delta, {
                "type": "tool_result",
                "tool_call_id": tc["id"],
                "content": content,
            })

        return tool_results, error_ids, blocked_ids, duplicate_ids, end_turn

    async def _execute_single_tool(
        self,
        agent_id: str,
        tool_call: dict,
        on_tool_call: ToolCallCallback,
        *,
        poll_turn_counts: dict[tuple[str, str], int] | None = None,
        budget_s: float | None = None,
    ) -> dict:
        """执行单个工具。仅声明了 timeout 的工具走协作式 wait_for。"""
        tool_name = tool_call["name"]
        arguments = tool_call["arguments"]
        tool_call_id = tool_call["id"]

        log.info("tool_execute",
                 agent_id=agent_id,
                 tool=tool_name,
                 args_len=len(arguments))

        # Waiting-gate: don't burn rounds on status polls while wait contract active
        blocked = await _poll_waiting_gate_block_async(agent_id, tool_name)
        if blocked is not None:
            return {"content": blocked}

        # Per-turn hard reject for identical get_tasks fingerprints (TEST4)
        if tool_name in _POLL_HARD_REJECT_TOOLS and poll_turn_counts is not None:
            key = (tool_name, arguments or "")
            n = poll_turn_counts.get(key, 0) + 1
            poll_turn_counts[key] = n
            if n >= _POLL_HARD_REJECT_LIMIT:
                try:
                    from hiveweave.services.telemetry import telemetry

                    telemetry.poll_hard_reject(agent_id, tool_name)
                except Exception:
                    pass
                # TEST10 修复: hard-reject 附带待办快照。此前 CEO 遇到
                # 「exit-gate 催审查 + poll 防护禁查询」双重夹击时无可行动
                # 信息。快照来自与 exit-gate 相同的 obligations 数据源。
                snapshot = await _build_obligations_snapshot(agent_id)
                return {
                    "content": (
                        f"[poll hard reject] {tool_name} called {n} times "
                        f"with the same arguments this turn. STOP polling — "
                        f"act on the obligations below directly, or call "
                        f"commit_turn(phase='waiting') and wait for "
                        f"event wake (task_transition / ask_reply / timeout)."
                        + snapshot
                    ),
                    "success": False,
                }

        # Short TTL cache for poll tools (TEST3 storm)
        cached = _poll_cache_get(agent_id, tool_name, arguments)
        if cached is not None:
            return {"content": cached}

        from hiveweave.tools.timeout_policy import declared_timeout_s

        tool_timeout = declared_timeout_s(tool_name)
        budget_capped = False
        if tool_timeout is not None and budget_s is not None:
            capped = min(tool_timeout, max(3.0, budget_s))
            budget_capped = capped < tool_timeout
            tool_timeout = capped
        try:
            if tool_timeout is None:
                result = await on_tool_call(
                    tool_name, arguments, tool_call_id
                )
            else:
                result = await asyncio.wait_for(
                    on_tool_call(tool_name, arguments, tool_call_id),
                    timeout=tool_timeout,
                )
            if isinstance(result, dict):
                content = result.get("content")
                if isinstance(content, str):
                    _poll_cache_put(agent_id, tool_name, arguments, content)
            return result
        except TimeoutError:
            log.error("tool_timeout",
                      agent_id=agent_id, tool=tool_name)
            ms = int((tool_timeout or 0) * 1000)
            hint = f" Error: tool call timed out after {ms}ms"
            if budget_capped:
                hint += (
                    " NOTE: the cap was tightened by the remaining turn "
                    "budget — do NOT retry the same long call this wake."
                )
            return {
                "content": (
                    f"[Tool Timeout] {tool_name} did not complete "
                    f"within {tool_timeout:g}s" + hint
                ),
                "success": False,
            }

    # ── Doom loop 检测 ──────────────────────────────────────

    @staticmethod
    def _detect_doom_loop(
        tool_calls: list[dict],
        tracker: dict[str, Any],
    ) -> str | None:
        """检测 doom loop: 同一工具+同一参数连续超过工具专属限制。

        不同工具有不同的容忍度（见 doom_loop_limit）：
        - 只读轮询工具（DOOM_LOOP_READONLY_TOOLS）15 次保险丝 — agent 无订阅
          机制，轮询 get_tasks/read_file 是获取状态的唯一手段，不算 doom
        - 审查工具 6 次 — LLM 可能在纠正输出格式
        - 幂等写入 8 次 — 覆盖写入无害但不应无限
        - 副作用工具 3 次 — bash/apply_patch 严格限制

        失败重试豁免已收窄（修 #1）：同参数连续调用始终计数。合法重试路径是
        "失败后改参数再调"——不同参数走 else 分支重置 count=1。同参数重试说明
        LLM 没有修正任何东西，是 doom loop 的典型模式。duplicate 信号在主循环
        中额外强制 +1 计数，进一步加速触顶。

        遇到不同调用时重置计数。更新 tracker 并返回触发 doom loop 的工具名，或 None。
        """
        last_key = tracker.get("last_key")
        count = tracker.get("count", 0)
        for tc in tool_calls:
            key = (tc["name"], tc["arguments"])
            if key == last_key:
                # 同参数连续调用：始终计数。失败后改参数重试走 else 分支
                # （count=1），同参数重试不豁免。
                count += 1
            else:
                last_key = key
                count = 1
            limit = doom_loop_limit(tc["name"])
            if count >= limit:
                tracker["last_key"] = last_key
                tracker["count"] = count
                return tc["name"]
        tracker["last_key"] = last_key
        tracker["count"] = count
        return None

