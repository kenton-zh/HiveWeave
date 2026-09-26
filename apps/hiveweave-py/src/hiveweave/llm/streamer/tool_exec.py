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

# 副作用工具：执行中抛异常时可能已产生外部副作用（写文件/跑命令/发消息），
# 结果无法确认。参考 DSH TOOL_OUTCOME_UNKNOWN —— 明确告诉模型「结果未知」，
# 只读/幂等可安全重试，可能有副作用的操作需先验证实际状态，避免盲目重试
# 造成重复副作用。与 doom_loop 的「重试容忍度」分组语义不同：这里按
# 「写/命令/外发/外部状态变更」界定，而非按重试次数。
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    # 命令/代码执行（可能执行一半产生副作用）
    "bash", "bash_main", "run_command", "python_script",
    "start_dev_server", "stop_dev_server", "job_kill",
    # 文件/目录写入
    "apply_patch", "write_file", "edit_file", "move_file",
    "delete_file", "create_directory", "delete_directory",
    # 外发消息/外部副作用（DB 已写入后抛异常 = 最高危重复副作用）
    "send_message", "ask_agent", "notify_agent", "message_user",
    "browse", "browse_main", "spawn_subagent", "generate_image",
})


def _unknown_outcome_content(
    tool_name: str, err_type: str, err_msg: str
) -> str:
    """工具执行中抛异常时的回执文案。

    - 副作用工具：注入 [TOOL OUTCOME UNKNOWN]，提示模型先验证再行动；
    - 其余工具：保留既有 [Tool Error] 语义（未执行，可安全重试）。
    """
    base = f"[Tool Error] {err_type}: {err_msg}"
    if tool_name in _SIDE_EFFECT_TOOLS:
        return (
            f"{base}\n\n[TOOL OUTCOME UNKNOWN] 工具 '{tool_name}' 在执行中"
            "抛异常，无法确认是否已产生副作用。请先验证实际状态（文件是否"
            "已写、命令是否已跑、消息是否已发）再做下一步：只读/幂等操作可"
            "安全重试，可能有副作用的操作不要盲目重试。"
        )
    return base


# ── 批 D 第 2 步：异常出口的结构化事实位 ─────────────────────────
# 病灶（审计 §6 第 2 步①）：异常出口只造文案，行级无结构化位 ——
# 73 项目 38 条 `_ConfinedDevProc` AttributeError（平台 shim 的代码 bug）
# 在 run_steps 里与工具业务失败同形，「平台侧 bug」只能靠人工读文案猜。
# 字段形态对齐 DSH 的**具名恢复码**（`packages/core/session/src/repair.ts:16-21`，
# HEAD 477b4f420）：TOOL_NOT_STARTED / TOOL_OUTCOME_UNKNOWN 是**闭合枚举**、
# 按事实（调用生命周期）而非措辞归类 —— 这里同理：`exception_type` = 异常类名
# （事实），`is_platform_bug` = 按类名的闭合判定表归因，不读错误文案。

#: 判定为**平台侧缺陷**的异常类型（代码 bug / 平台环境问题，模型无责）。
#: OSError 含全部子类（FileNotFoundError / PermissionError…）：工具实现在
#: OS 边界上炸了，属平台侧可修（哪怕是环境问题，也不是模型的参数错）。
_PLATFORM_BUG_EXC_TYPES: tuple[type[BaseException], ...] = (
    AttributeError,
    KeyError,
    IndexError,
    TypeError,
    AssertionError,
    NotImplementedError,
    NameError,
    UnboundLocalError,
    RecursionError,
    SystemError,
    OSError,
)

#: 判定为**工具业务失败**的异常类型（调用方可改变结果 —— 换参数/换输入能过）。
_TOOL_BUSINESS_EXC_TYPES: tuple[type[BaseException], ...] = (
    ValueError,
    LookupError,  # 注意 KeyError 是其子类，已被上方平台表优先命中
)


def is_platform_bug_exception(exc: BaseException) -> bool | None:
    """异常是否平台侧缺陷（批 D 第 2 步任务 1 的**单一判定实现**）。

    返回三态：True = 平台侧（is_platform_bug=1）；False = 工具业务失败；
    None = 不在判定表内（未判定，调用方**不落库** —— 宁可留 NULL 不臆断，
    同 run_steps 事实位的「未确定不写」纪律）。

    判定按**异常类名**（MRO 逐级匹配子类），与错误文案/语言无关 ——
    对齐本仓「状态判据优先于文本判据」的一等纪律。
    """
    for cls in type(exc).__mro__:
        if cls in _PLATFORM_BUG_EXC_TYPES:
            return True
        if cls in _TOOL_BUSINESS_EXC_TYPES:
            return False
    return None


def exception_fact_flags(exc: BaseException) -> dict:
    """从异常构造结构化事实位（tool_msg 透传 + 落库共用形状）。

    只带能确定的位：`is_platform_bug` 判不出时不写（None 不落）。
    """
    flags: dict = {"exception_type": type(exc).__name__}
    verdict = is_platform_bug_exception(exc)
    if verdict is not None:
        flags["is_platform_bug"] = verdict
    return flags


async def _record_exception_signature(
    agent_id: str,
    tool_name: str,
    exc: BaseException,
    fact_flags: dict,
) -> None:
    """异常出口 → 共享失败签名（批 D 第 2 步任务 6 的写入侧接线）。

    异常路径**不经过** executor 的 F10 钩子（``record_failure_signature``
    只在工具返回失败回执的路径上跑）⇒ 平台 bug（38 条 AttributeError）
    对签名池全盲，distinct_hitters 攒不到阈值 → R7 组织升级空转。

    身份用**结构化签名**（tool + 事实位组合 —— 任务 1 落位后才可用，
    破 failure_signature docstring 里「现在切会塌成只剩 tool」的死结；
    文本签名作 fallback，见 ``record_exception_failure_signature``）。
    best-effort：任何失败只记日志，绝不影响工具回执。
    """
    from hiveweave.services.failure_signature import (
        record_exception_failure_signature,
    )

    await record_exception_failure_signature(
        agent_id=agent_id,
        tool_name=tool_name,
        exc=exc,
        fact_flags=fact_flags,
    )


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

        F4/F7（平台修复计划 2026-08-30）：事实位（blocked/runner_failed/
        command_failed/…）**经返回的 tool_results 同步聚合** —— tool_msg 内
        只保留 role/content/tool_call_id/images（消息契约纯净），事实位
        由调用方 `_round_fact_flags(tool_results...)` 从每条 tool_msg 读取。
        注意：tool_results 里的每条只携带能确定的位，未确定不写（None 不落）。
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
                content = _unknown_outcome_content(
                    tc["name"], type(result).__name__, str(result)
                )
                error_ids.add(tc["id"])
                # 批 D 第 2 步任务 1：异常出口的结构化事实位。
                # 行级落库在 agents/streaming.py 的 except 分支（record_step_end
                # —— 那里有 step_id，是行关闭的唯一点位）；本层职责：
                # ① 事实位透传到 tool_msg（stall 归因/回扫可见）；
                # ② 平台侧异常写共享失败签名（任务 6 —— 异常路径此前根本
                #    不经过 executor 的 F10 钩子，签名池对 38 条
                #    AttributeError 这类平台 bug 全盲，distinct_hitters
                #    攒不到阈值 → R7 空转）。
                _exc_flags = exception_fact_flags(result)
                try:
                    await _record_exception_signature(
                        agent_id, tc["name"], result, _exc_flags
                    )
                except Exception as e:  # noqa: BLE001 — 签名是旁支，绝不影响回执
                    # 静默吞掉 = 「平台 bug 为什么没聚人」无从排查（独立审计
                    # P2-2）：留 debug 摘要，不刷屏但可捞。
                    log.debug("tool_exception_signature_failed",
                              tool=tc["name"],
                              error_type=type(result).__name__,
                              error=str(e)[:200])
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
            # F4（平台修复计划 2026-08-30）：正交事实位透传到 tool_results
            # —— blocked/runner_failed/command_failed 是 F8 advisory 归因的
            # 数据源。这里**选择性**添加（只加证据充分的位；不给成功的调用
            # 乱贴 success 键 —— 持久化 JSON 纯净度同消息契约）。下游
            # provider 组包时白名单剥离；_round_fact_flags 消费它们。
            if not isinstance(result, BaseException):
                for _fk in ("blocked", "runner_failed", "command_failed"):
                    if result.get(_fk):
                        tool_msg[_fk] = True
            else:
                # 批 D 第 2 步任务 1：异常出口事实位随 tool_msg 透传
                # （exception_type / is_platform_bug）。与 F4 同纪律：
                # provider 组包白名单剥离，只带能确定的位。
                tool_msg.update(_exc_flags)
            # Multimodal: preserve screenshot pixels for the next LLM round.
            images = None if isinstance(result, BaseException) else result.get("images")
            if images:
                tool_msg["images"] = images
            tool_results.append(tool_msg)
            # 广播 tool_result（solo/streamer 直连路径的唯一完成信号——
            # 字段对齐 canonical 的 tool_call_end：name/success 必带，
            # 否则 adapter 归一化后前端无法落 ✓/✗。error_ids 在本循环
            # 内先于广播累加，此处读取即最终成败。）
            await self._fire_delta(on_delta, {
                "type": "tool_result",
                "tool_call_id": tc["id"],
                "tool_name": tc["name"],
                "success": tc["id"] not in error_ids,
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

