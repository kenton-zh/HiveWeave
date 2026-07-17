"""LLM 流式调用 + tool loop — 核心流式层。

契约 01: LLM 流式调用
- SSE 格式解析（data: {...}\\n\\n）
- OpenAI 兼容 API（/chat/completions with stream:true）
- httpx.AsyncClient 流式读取
- 三层超时: connect=10s / read=120s / total=300s
- Tool loop: LLM 返回 tool_calls → 执行工具 → 结果追加 → 重新请求，最多 100 轮（所有角色统一）
- 空响应重试: 无 content 无 tool_calls 时重试，最多 3 次
- Doom loop 检测: 同一工具+同一参数 3 次中断
- 中轮提醒: 80% 轮次时注入「开始收尾」系统提示
- 参考: Elixir streamer.ex + TS agent-runtime.ts

关键设计:
- on_delta 回调: 每个 text_delta/thinking_delta 调用一次，用于实时转发给前端
- on_tool_call 回调: 工具执行入口，返回 {role:"tool", content, tool_call_id} 结果
- Streamer 不直接依赖 ToolExecutor，通过回调解耦
- 所有 provider 统一走 OpenAI 兼容 SSE 格式（见 provider.py）
"""

from __future__ import annotations

import asyncio
import codecs
import json
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import structlog

from hiveweave.llm.circuit_breaker import CircuitBreaker, circuit_breaker
from hiveweave.conversation.token_utils import estimate_tokens_for_messages
from hiveweave.llm.provider import ProviderConfig, ProviderFactory, provider_factory, READ_TIMEOUT_S
from hiveweave.llm.retry import (
    PermanentError,
    RetryHandler,
    RetryableError,
    is_retryable_status,
)

log = structlog.get_logger(__name__)


class CircuitBreakerOpenError(Exception):
    """熔断器已打开，请求被拒绝（C9）。

    当 provider 连续失败达到阈值后抛出。携带 provider 名称和（可选的）
    fallback 名称，供调用方决策是否切换到备用 provider。

    简化方案：当前不实现自动 provider 切换（需要解析 fallback model config），
    直接抛出此异常让调用方知道熔断器已打开，避免原代码「只打日志不 return」
    继续用被熔断的 provider 发请求的死代码行为。
    """

    def __init__(self, provider: str, fallback: str | None = None) -> None:
        self.provider = provider
        self.fallback = fallback
        if fallback:
            msg = (
                f"Circuit breaker open for provider '{provider}' "
                f"(fallback '{fallback}' available but auto-switch "
                f"not implemented)"
            )
        else:
            msg = (
                f"Circuit breaker open for provider '{provider}' "
                f"and no fallback available"
            )
        super().__init__(msg)

# ── 常量（契约 01）──────────────────────────────────────────

MAX_TOOL_ROUNDS = 1_000_000
"""最大 tool loop 轮次 — 仅作极端安全网，实际由 doom loop 按工具分级保护。峰值复现真实死循环(同参数反复调用) 。"""

MAX_TOOLS_PER_ROUND = 5
"""单轮工具调用数上限。对齐 Elixir streamer.ex:488。"""

EMPTY_RESPONSE_MAX_RETRIES = 3
"""空响应重试上限。契约 01: 最多 3 次。"""

EMPTY_RESPONSE_BACKOFF_MS = [5_000, 15_000, 45_000]
"""空响应退避序列（5s/15s/45s）。契约 01。"""

DOOM_LOOP_DEFAULT_LIMIT = 3
"""默认 doom loop 阈值 — 同一工具+同一参数连续 N 次中断。"""

DOOM_LOOP_TOOL_LIMITS: dict[str, int] = {
    # 只读工具 — 高容忍，重复查询是正常的探索行为
    "list_files": 15,
    "read_file": 12,
    "grep": 10,
    "search_files": 10,
    "list_subordinates": 10,
    "read_charter": 10,
    "read_goals": 10,
    "read_work_logs": 10,
    "get_subordinate_logs": 10,
    "list_available_skills": 10,
    "read_skill": 10,
    "view_org_chart": 10,
    # 审查工具 — 中容忍，重试可能是 LLM 纠正输出格式
    "run_code_review": 6,
    "run_security_audit": 6,
    "run_tests": 6,
    "run_perf_audit": 6,
    "run_full_review": 6,
    # 每轮强制出口 — 被出口闸门拒收后必须重试，默认阈值 3 会误杀
    # （井字棋实测：CEO 首条指令即撞 doom，无任何正常输出）
    "commit_turn": 6,
    # 幂等写入 — 中容忍，覆盖写入无害但不应无限重复
    "write_file": 8,
    "save_charter": 8,
    "save_goals": 8,
    "save_memory": 8,
    "update_roster": 8,
    "todowrite": 8,
    "mark_read": 8,
    "write_work_log": 8,
    "update_goals": 8,
    # 外发消息 — 低容忍，避免刷屏
    "send_message": 5,
    "question": 4,
    # 副作用工具 — 最低容忍，防止真实损害
    "bash": 3,
    "apply_patch": 3,
    "websearch": 3,
    "execute_code": 3,
}
"""Per-tool doom loop thresholds. 不同工具不同限制：

- 只读工具 (10-15次): 重复查询可能是 agent 在探索不同角度的正常行为
- 审查工具 (6次): LLM 可能在纠正输出格式，需要更多尝试
- 幂等写入 (8次): 覆盖写入无害，但不应无限重复
- 外发消息 (4-5次): 避免对其他 agent 或用户造成骚扰
- 副作用工具 (3次): 真实命令执行，严格限制防止损害
"""

NO_TEXT_ROUNDS_THRESHOLD = 3
"""连续无文字轮次阈值: 3 轮后注入系统提示。"""

NO_TEXT_HINT_MAX = 5
"""无文字提示注入上限: 超过 5 次后强制结束 tool loop 走总结。

设计意图 (project_memory):
- 连续 3 轮只调工具不说话 → 第 1 次注入提示，计数重置
- 重复 5 次（共 ~18 轮）→ 第 5 次注入触发 break，强制走 _make_max_rounds_summary
- 给 executor 更多空间完成多文件写入（如初始化项目骨架需 10+ write_file）
- 仍可避免卡死的 agent 空转到 60/80 硬上限
"""

DEFAULT_PLACEHOLDER = "好的，开始处理。\n"
"""默认占位文本（UI 提示，不计为真实输出）。"""

MID_ROUND_REMINDER_RATIO = 0.8
"""中轮提醒注入时机: 80% 轮次时。"""

SAFETY_BUFFER_TOKENS = 20_000
"""上下文溢出检查的安全缓冲。

覆盖未计量开销：工具定义 JSON Schema（15-25K tokens）、system prompt 框架文本等。
旧值 4K 远不够，导致 token 估算认为还有空间但实际 API 已超限。
"""

OUTPUT_TOKEN_GLOBAL_CAP = 32_000
"""非 reasoning 模型的 max_tokens 全局上限。"""

CONTINUE_SENTINEL = "(continue)"
"""网关兼容性哨兵：追加在请求末尾的静态 user 消息。

见 _stream_single_round 的 FIX(gateway-tool-id-400) 注释。
保持内容恒定且极短（约 1 token），不改变模型行为。
"""

TOTAL_TIMEOUT_S = 540.0
"""整个 stream 调用的总超时（兜底防线）。

BUG-041: 原 300s 包裹整个 _run_tool_loop，多轮工具调用（每轮含 HTTP +
工具执行）合法场景也会超时。放大到 540s（9分钟），给 agent safety_timeout
(600s) 留 60s 余量。同时超时不再报熔断失败——多轮工具调用超时不是 provider
不稳定的问题。
"""

FIRST_CHUNK_TIMEOUT_S = 90.0
"""首 chunk 超时（TS 防线②，thinking 模型首 token 可能 60-90s）。"""

IDLE_TIMEOUT_S = 60.0
"""后续 chunk idle 超时（TS 防线②）。"""

# ── Bug B fix: 全局 LLM 并发控制 ───────────────────────────
# 防止多 agent 同时打 LLM API 超过 provider 并发限制（默认 8）。
# Semaphore 在 HTTP 请求级别获取/释放，tool 执行期间不占槽。
import os as _os
_LLM_MAX_CONCURRENT = int(_os.environ.get("HIVEWEAVE_LLM_MAX_CONCURRENT", "8"))
_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Lazy-init global LLM semaphore (must be created inside event loop)."""
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(_LLM_MAX_CONCURRENT)
    return _LLM_SEMAPHORE

TOOL_EXECUTION_TIMEOUT_S = 120.0
"""单个工具执行超时。对齐 Elixir Task.yield(task, 120_000)。"""

# ── 类型别名 ────────────────────────────────────────────────

DeltaCallback = Callable[[dict], Awaitable[None] | None]
"""SSE delta 回调。收到 {type:"text_delta", content, ...} 等事件时调用。"""

ToolCallCallback = Callable[[str, str, str], Awaitable[dict]]
"""工具执行回调。

签名: async def callback(tool_name: str, arguments: str, tool_call_id: str) -> dict
返回: {"role": "tool", "content": "...", "tool_call_id": "..."}
"""


# ── SSE 解析 ────────────────────────────────────────────────


def parse_sse(buffer: str) -> tuple[list[dict], str]:
    """解析 SSE 缓冲区，返回 (events, leftover)。

    SSE 格式: 事件之间用空行分隔（\\n\\n 或 \\r\\n\\r\\n），每个事件是
    data: {json} 的行。最后一段可能是不完整的，作为 leftover 返回供下次拼接。

    R1: 同时处理 \\r\\n\\r\\n 和 \\n\\n 分隔符 —— 某些代理/CDN（如 Cloudflare、
    Nginx 默认）会把 SSE 事件的空行分隔符规范化为 CRLF。先做 CRLF→LF 归一化，
    再按 \\n\\n 分割，兼容两种分隔符。

    对齐 Elixir parse_sse/1。
    """
    if not buffer:
        return [], ""

    # R1: 规范化 CRLF → LF，使 \r\n\r\n 成为 \n\n（兼容 CDN/代理的 CRLF 分隔）
    normalized = buffer.replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    # 最后一段可能不完整（无结尾 \n\n）
    *complete, leftover = parts

    events: list[dict] = []
    for part in complete:
        event = _extract_data(part)
        if event is not None:
            events.append(event)

    return events, leftover


def _extract_data(block: str) -> dict | None:
    """从 SSE 事件块提取 data + event 字段并解析 JSON。

    支持 OpenAI SSE（仅 data: 行）和 Anthropic SSE（event: + data: 行）。
    一个事件块可能有多行 data:（多行 JSON 拼接），对齐 Elixir extract_data/1。
    如果有 event: 行，存储为 _event_type 字段供 handler 使用。
    """
    if not block:
        return None

    data_parts: list[str] = []
    event_type: str | None = None
    for line in block.split("\n"):
        if line.startswith("data:"):
            value = line[5:]  # 去掉 "data:" 前缀
            data_parts.append(value.strip())
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        # 忽略 id:/retry: 等其他 SSE 字段

    if not data_parts:
        return None

    data_str = "".join(data_parts)
    if data_str == "[DONE]":
        return {"__done__": True}

    try:
        parsed = json.loads(data_str)
        if isinstance(parsed, dict):
            # Preserve SSE event type for Anthropic-style SSE
            if event_type and "type" not in parsed:
                parsed["_event_type"] = event_type
            return parsed
        return None
    except (json.JSONDecodeError, TypeError):
        return None


# ── SSE event → chunks 转换 ─────────────────────────────────


def sse_to_chunks(event: dict) -> list[dict]:
    """将单个 SSE event 转为 chunk 列表。

    一个 delta 可能同时携带 reasoning + text + tool_calls + finish_reason，
    我们逐字段提取，返回多个 chunk（对齐 Elixir sse_to_chunks/1）。

    chunk 类型:
    - {type:"text", content:str}
    - {type:"reasoning", content:str}
    - {type:"tool_call_delta", tool_call:{index, id, name, arguments}}
    - {type:"finish", reason:str}
    - {type:"error", content:str}
    """
    if event.get("__done__"):
        return []

    # 错误响应
    if "error" in event and isinstance(event["error"], dict):
        msg = event["error"].get("message") or str(event["error"])
        return [{"type": "error", "content": msg}]

    choices = event.get("choices")
    if not choices or not isinstance(choices, list):
        return []

    choice = choices[0]
    if not isinstance(choice, dict):
        return []

    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    chunks: list[dict] = []

    # 1. Reasoning content — 检查所有已知字段名变体
    reasoning_text = _extract_reasoning(delta)
    if reasoning_text:
        chunks.append({"type": "reasoning", "content": reasoning_text})

    # 2. Text content — 支持 string 和 array-of-content-blocks 两种格式
    text_content = _extract_text_content(delta.get("content"))
    if text_content:
        chunks.append({"type": "text", "content": text_content})

    # 3. Tool calls — 支持 function 包装和 flat 两种格式
    tool_calls_raw = delta.get("tool_calls")
    if isinstance(tool_calls_raw, list) and tool_calls_raw:
        for tc in tool_calls_raw:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            arguments = fn.get("arguments") or tc.get("arguments") or ""
            chunks.append({
                "type": "tool_call_delta",
                "tool_call": {
                    "index": tc.get("index", 0),
                    "id": tc.get("id"),
                    "name": name,
                    "arguments": arguments,
                },
            })

    # 4. Finish reason（最后处理，不阻塞其他字段）
    if finish_reason is not None and finish_reason != "null":
        chunks.append({"type": "finish", "reason": finish_reason})

    return chunks


def _extract_reasoning(delta: dict) -> str | None:
    """提取 reasoning/thinking 内容（多字段名兼容）。"""
    for key in ("reasoning_content", "reasoning", "thinking", "thinking_content"):
        val = delta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_text_content(content: Any) -> str | None:
    """提取 text content，支持 string 和 array-of-blocks 格式。"""
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        # array of content blocks: [{"type":"text","text":"..."}]
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    texts.append(t)
        if texts:
            return "".join(texts)
    return None


# ── Tool calls 合并 ─────────────────────────────────────────


def merge_tool_calls(
    existing: list[dict],
    new_deltas: list[dict],
) -> list[dict]:
    """将流式 tool_call deltas 合并为完整的 tool_calls。

    流式返回的 tool_calls 是分片的: name 和 arguments 分多次到达。
    按 index 分组，拼接 name 和 arguments fragments。

    对齐 Elixir merge_tool_calls/2。

    Args:
        existing: 已合并的 tool_calls 列表
        new_deltas: 新的 delta 列表 [{index, id, name, arguments}]

    Returns:
        合并后的完整 tool_calls 列表
    """
    all_deltas = existing + new_deltas
    if not all_deltas:
        return []

    # 按 index 分组
    groups: dict[int, list[dict]] = {}
    for d in all_deltas:
        idx = d.get("index", 0)
        groups.setdefault(idx, []).append(d)

    result: list[dict] = []
    for idx in sorted(groups.keys()):
        deltas = groups[idx]
        # name fragments 按顺序拼接
        name = "".join(
            d["name"] for d in deltas if d.get("name")
        )
        # arguments fragments 按顺序拼接
        arguments = "".join(
            d["arguments"] for d in deltas if d.get("arguments")
        )
        # id 取第一个非空值
        call_id = next(
            (d["id"] for d in deltas if d.get("id")),
            str(uuid.uuid4()),
        )
        result.append({
            "id": call_id,
            "name": name,
            "arguments": arguments,
        })

    return result


# ── Streamer ────────────────────────────────────────────────


class Streamer:
    """LLM 流式调用 + tool loop。

    用法::

        streamer = Streamer()
        result = await streamer.stream(
            agent_id="agent-123",
            messages=[{"role":"user","content":"你好"}],
            model_config={"base_url":"...","api_key":"...","model_id":"..."},
            tools=[...],
            on_delta=lambda evt: websocket.send(evt),
            on_tool_call=lambda name, args, tid: tool_executor.execute(name, args),
        )

    返回::

        {
            "status": "ok" | "empty" | "error",
            "content": "最终文本",
            "thinking": "推理内容",
            "tool_calls": [...],  # 工具调用历史
            "tool_turn_messages": [...],  # 所有 assistant+tool 消息
            "rounds": N,
            "usage": {"input":..,"output":..,"total":..},
            "error": "错误信息" | None,
        }
    """

    def __init__(
        self,
        provider_factory_inst: ProviderFactory | None = None,
        circuit_breaker_inst: CircuitBreaker | None = None,
        retry_handler: RetryHandler | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self._provider_factory = provider_factory_inst or provider_factory
        self._circuit_breaker = circuit_breaker_inst or circuit_breaker
        self._retry_handler = retry_handler or RetryHandler()
        self.max_tool_rounds = max_tool_rounds

    # ── 主入口 ──────────────────────────────────────────────

    async def stream(
        self,
        agent_id: str,
        messages: list[dict],
        model_config: dict,
        tools: list[dict] | None = None,
        on_delta: DeltaCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        max_tool_rounds: int | None = None,
    ) -> dict:
        """流式调用 LLM，执行 tool loop，返回最终结果。

        Args:
            agent_id: Agent ID（用于日志/遥测）
            messages: 初始消息列表（含 system + history + user）
            model_config: 模型配置 dict（base_url, api_key, model_id, ...）
            tools: 可用工具列表 [{type:"function", function:{name, description, parameters}}]
            on_delta: SSE delta 回调（text_delta/thinking_delta 等事件）
            on_tool_call: 工具执行回调，返回 {role:"tool", content, tool_call_id}
            max_tool_rounds: 本轮调用的 tool loop 上限。若提供则覆盖构造器
                默认值（来自 agent 的 DEFAULT_MAX_TOOL_ROUNDS = 600）。
                未提供时回退到 self.max_tool_rounds。

        Returns:
            结果 dict（见类文档字符串）
        """
        start_time = time.monotonic()
        provider = self._provider_factory.create(model_config)
        provider_name = model_config.get("name") or "primary"

        # 优先用调用方传入的 max_tool_rounds，未提供时回退到实例默认值
        effective_max_rounds = max_tool_rounds if max_tool_rounds else self.max_tool_rounds

        log.info(
            "stream_start",
            agent_id=agent_id,
            provider=provider.provider_type,
            model=provider.model_name,
            msg_count=len(messages),
            tool_count=len(tools or []),
        )

        # 注册熔断器（如果未注册）
        await self._circuit_breaker.register(
            provider_name, fallback=provider.fallback
        )

        # 熔断器检查（C9: fallback 不再是无操作死代码 — 直接抛出明确异常）
        cb_result = await self._circuit_breaker.check(provider_name)
        if not cb_result.allowed:
            # Bug J fix: 如果有 fallback provider，自动切换重试
            if cb_result.fallback:
                log.info("circuit_fallback_switch",
                         from_provider=provider_name,
                         to_provider=cb_result.fallback)
                try:
                    from hiveweave.services.model import ModelService
                    model_svc = ModelService()
                    fallback_config = await model_svc.get(
                        cb_result.fallback)
                    if fallback_config and fallback_config.get("is_active"):
                        # 用 fallback model config 递归调用 stream
                        return await self.stream(
                            agent_id=agent_id,
                            messages=messages,
                            model_config=fallback_config,
                            tools=tools,
                            on_delta=on_delta,
                            on_tool_call=on_tool_call,
                            max_tool_rounds=max_tool_rounds,
                        )
                except Exception as fb_err:
                    log.warning("circuit_fallback_failed",
                                fallback=cb_result.fallback,
                                error=str(fb_err))
            raise CircuitBreakerOpenError(provider_name, cb_result.fallback)

        # 广播 start 事件
        await self._fire_delta(on_delta, {"type": "start"})

        try:
            result = await asyncio.wait_for(
                self._run_tool_loop(
                    agent_id=agent_id,
                    provider=provider,
                    provider_name=provider_name,
                    messages=list(messages),
                    tools=tools,
                    on_delta=on_delta,
                    on_tool_call=on_tool_call,
                    max_tool_rounds=effective_max_rounds,
                ),
                timeout=TOTAL_TIMEOUT_S,
            )
            # 熔断器成功/失败上报已移至 _stream_single_round 按轮次精确上报（C10）
            result["duration_ms"] = int((time.monotonic() - start_time) * 1000)
            return result
        except TimeoutError:
            # BUG-041: total timeout 通常是多轮工具调用累计超时，
            # 不是 provider 不稳定 — 不报熔断失败
            log.error("stream_total_timeout", agent_id=agent_id,
                      timeout_s=TOTAL_TIMEOUT_S)
            await self._fire_delta(on_delta, {
                "type": "error", "content": f"请求总超时（{TOTAL_TIMEOUT_S}s）"
            })
            return self._error_result("请求总超时", start_time)
        except Exception as e:
            await self._circuit_breaker.report_failure(provider_name)
            log.exception("stream_error", agent_id=agent_id, error=str(e))
            await self._fire_delta(on_delta, {
                "type": "error", "content": str(e)
            })
            return self._error_result(str(e), start_time)
        finally:
            await self._fire_delta(on_delta, {"type": "done"})

    # ── Tool loop 主循环 ────────────────────────────────────

    async def _run_tool_loop(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        on_tool_call: ToolCallCallback | None,
        max_tool_rounds: int | None = None,
    ) -> dict:
        """Tool loop: 流式请求 → 检查 tool_calls → 执行工具 → 重复。"""
        # 使用调用方传入的上限，回退到实例默认值
        rounds_cap = max_tool_rounds if max_tool_rounds else self.max_tool_rounds
        text_acc = ""
        thinking_acc = ""
        tool_history: list[dict] = []
        tool_turn_acc: list[dict] = []
        last_usage: dict | None = None
        no_text_rounds = 0
        no_text_hint_count = 0  # 无文字提示注入次数，超过 NO_TEXT_HINT_MAX 时 break
        # R2: 跟踪连续相同的 (tool_name, tool_args) 调用。
        # 累加式计数会误判合法的跨轮重复操作；改为「连续相同」计数，
        # 遇到不同调用时重置。只在连续 DOOM_LOOP_THRESHOLD 次相同调用时才判定。
        doom_tracker: dict[str, Any] = {"last_key": None, "count": 0}

        # Doom loop 警告标志: 第一次触发时注入反馈给 LLM 纠正机会，
        # 只有第二次再次触发才真正中断。
        doom_warning_given: bool = False

        # Bug-5 修复: 跟踪本对话是否已注入过占位符，避免 LLM 把占位符当
        # 自己的输出后陷入 "调工具不说话 → 占位注入 → LLM 看到 '好的开始处理'
        # → 不结束 → 再调工具 → 再注入" 的死循环。
        placeholder_injected: bool = False

        for round_num in range(rounds_cap):
            # 通知回调：新一轮开始（用于重置流式文本累积器）
            if round_num > 0 and on_delta:
                await self._fire_delta(on_delta, {
                    "type": "round_start",
                    "round": round_num,
                })

            # 上下文溢出检查
            messages = self._trim_context_if_needed(messages, provider)

            # 中轮提醒: 80% 轮次时注入
            messages = self._maybe_inject_mid_round_reminder(
                messages, round_num, rounds_cap
            )

            log.info("tool_loop_round",
                     agent_id=agent_id, round=round_num,
                     msg_count=len(messages))

            # 单轮流式请求（带空响应重试）
            round_result = await self._stream_with_empty_retry(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=messages,
                tools=tools,
                on_delta=on_delta,
                round_num=round_num,
            )

            if round_result["status"] == "error":
                return {
                    "status": "error",
                    "content": text_acc or "",
                    "thinking": thinking_acc,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                    "error": round_result.get("error"),
                }

            new_text = round_result["text"] or ""
            new_thinking = round_result["thinking"] or ""
            tool_calls = round_result["tool_calls"]
            finish_reason = round_result["finish_reason"]
            last_usage = round_result.get("usage")

            combined_text = text_acc + new_text
            combined_thinking = thinking_acc + new_thinking

            log.info("round_result",
                     agent_id=agent_id, round=round_num,
                     text_len=len(new_text), tool_count=len(tool_calls),
                     finish=finish_reason)

            # 处理截断的响应
            if finish_reason in ("length", "content_filter") and tool_calls:
                # 截断的 tool_calls 可能不完整，丢弃
                log.warning("discard_incomplete_tool_calls",
                            agent_id=agent_id, round=round_num,
                            finish=finish_reason)
                real_text = self._strip_placeholder(combined_text)
                warning = f"\n\n⚠️ 响应被截断（{finish_reason}），部分工具调用可能不完整。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            if finish_reason == "length":
                log.warning("response_truncated_length", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被截断（达到最大输出长度），请继续以完成。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            if finish_reason == "content_filter":
                log.warning("content_filtered", round=round_num)
                real_text = self._strip_placeholder(combined_text)
                warning = "\n\n⚠️ 回复被内容过滤器截断。"
                tool_turn_acc.append({"role": "assistant", "content": real_text + warning})
                return {
                    "status": "ok",
                    "content": real_text + warning,
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            # 有 tool_calls → 执行工具，继续循环
            if tool_calls:
                # 截断到每轮最多 MAX_TOOLS_PER_ROUND 个
                if len(tool_calls) > MAX_TOOLS_PER_ROUND:
                    log.warning("truncate_tool_calls",
                                round=round_num,
                                total=len(tool_calls),
                                capped=MAX_TOOLS_PER_ROUND)
                    tool_calls = tool_calls[:MAX_TOOLS_PER_ROUND]

                # 占位文本: 如果累积文本为空且本轮还没注入过占位符，广播占位（UI 提示）
                # Bug-5 修复: 1) 同一 round 只注入一次 2) 占位不进入 text_acc
                # 避免 UI 上看到 5 个 "好的，开始处理" 的循环。
                if not combined_text and not placeholder_injected:
                    await self._fire_delta(on_delta, {
                        "type": "text_delta",
                        "content": DEFAULT_PLACEHOLDER,
                        "delta_id": f"default_{round_num}",
                        "is_placeholder": True,
                    })
                    # 不要把占位符塞进 combined_text / text_acc，避免下一轮再次注入
                    placeholder_injected = True

                # Doom loop 检测
                doom = self._detect_doom_loop(tool_calls, doom_tracker)
                if doom:
                    limit = DOOM_LOOP_TOOL_LIMITS.get(doom, DOOM_LOOP_DEFAULT_LIMIT)
                    if not doom_warning_given:
                        # 第一次触发: 拦截重复调用，注入反馈给 LLM 纠正机会
                        log.warning("doom_loop_warned",
                                    agent_id=agent_id, tool=doom, count=limit)
                        doom_warning_given = True
                        # 构造 assistant 消息（含 tool_calls）让 LLM 看到自己的请求
                        doom_assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": new_text if new_text else None,
                            "tool_calls": [
                                {
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": tc["arguments"],
                                    },
                                }
                                for tc in tool_calls
                            ],
                        }
                        if provider.supports_thinking and new_thinking:
                            doom_assistant_msg["reasoning_content"] = new_thinking
                        tool_turn_acc.append(doom_assistant_msg)
                        for tc in tool_calls:
                            tool_history.append({
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            })
                        # 返回拦截结果（不执行真正的工具）
                        tool_results = [
                            {
                                "role": "tool",
                                "content": (
                                    f"[DOOM LOOP 拦截] 你已连续 {limit} 次用完全相同的参数调用 '{doom}'。"
                                    f"这可能是死循环。请换用其他工具、调整参数，或先用文字说明"
                                    f"你为何需要重复执行相同命令。"
                                ),
                                "tool_call_id": tc["id"],
                            }
                            for tc in tool_calls
                        ]
                        messages = messages + [doom_assistant_msg] + tool_results
                        tool_turn_acc.extend(tool_results)
                        # 重置 tracker，给 LLM 一轮纠正机会
                        doom_tracker = {"last_key": None, "count": 0}
                        # 累积文本和 thinking
                        text_acc = self._strip_placeholder(combined_text)
                        thinking_acc = combined_thinking
                        continue
                    else:
                        # 第二次触发: 真正中断
                        log.warning("doom_loop_detected",
                                    agent_id=agent_id, tool=doom)
                        return {
                            "status": "error",
                            "content": text_acc or "",
                            "thinking": thinking_acc,
                            "tool_calls": tool_history,
                            "tool_turn_messages": tool_turn_acc,
                            "rounds": round_num + 1,
                            "usage": last_usage,
                            "error": f"Doom loop detected: tool '{doom}' called "
                                     f"{limit}+ times with same args (after warning)",
                        }

                # 构建 assistant 消息（含 tool_calls）
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": new_text if new_text else None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                # reasoning 模型: 附加 reasoning_content 保持思维链
                if provider.supports_thinking and new_thinking:
                    assistant_msg["reasoning_content"] = new_thinking

                tool_turn_acc.append(assistant_msg)

                # 累积 tool_history
                for tc in tool_calls:
                    tool_history.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    })

                # 执行工具
                if on_tool_call is None:
                    log.error("no_tool_executor", agent_id=agent_id)
                    tool_results = [
                        {"role": "tool", "content": "[No tool executor]",
                         "tool_call_id": tc["id"]}
                        for tc in tool_calls
                    ]
                    doom_tracker["last_errored"] = True
                else:
                    tool_results, error_ids = await self._execute_tools(
                        agent_id=agent_id,
                        tool_calls=tool_calls,
                        on_tool_call=on_tool_call,
                        on_delta=on_delta,
                    )
                    # doom 检测的失败重试豁免：本轮有工具出错时，下一轮同参数
                    # 重试不计 doom（合法的重试，如 commit_turn 被出口闸门拒收后
                    # 履行义务再提交）
                    doom_tracker["last_errored"] = bool(error_ids)

                # 追加 assistant + tool_results 到 messages
                messages = messages + [assistant_msg] + tool_results
                tool_turn_acc.extend(tool_results)

                # 连续无文字轮次检测
                if not new_text:
                    no_text_rounds += 1
                    if no_text_rounds >= NO_TEXT_ROUNDS_THRESHOLD:
                        no_text_hint_count += 1
                        if no_text_hint_count > NO_TEXT_HINT_MAX:
                            # 第 2 次注入后仍然只调工具不说话 → 强制结束 tool loop
                            log.warning("no_text_hint_exhausted",
                                        agent_id=agent_id,
                                        round=round_num,
                                        hint_count=no_text_hint_count)
                            summary = await self._make_max_rounds_summary(
                                agent_id, provider, messages, on_delta
                            )
                            # FIX(text-acc): 同 max_rounds 路径，只用 summary
                            final_text = self._strip_placeholder(summary)
                            tool_turn_acc.append(
                                {"role": "assistant", "content": final_text}
                            )
                            return {
                                "status": "ok",
                                "content": final_text,
                                "thinking": thinking_acc,
                                "tool_calls": tool_history,
                                "tool_turn_messages": tool_turn_acc,
                                "rounds": round_num + 1,
                                "usage": last_usage,
                            }
                        log.info("inject_no_text_hint", round=round_num,
                                 no_text_rounds=no_text_rounds,
                                 hint_count=no_text_hint_count)
                        messages.append({
                            "role": "system",
                            "content": (
                                f"你已经连续{no_text_rounds}轮只调用工具没有输出文字。"
                                "从现在开始，你必须在调用工具之前先用一段文字说明"
                                "你正在做什么、分析到了什么。不要只调用工具不说话。"
                            ),
                        })
                        no_text_rounds = 0
                else:
                    no_text_rounds = 0

                # Bug-5 修复: 累积前剥除可能混入的占位符（防御性，多余但安全）
                text_acc = self._strip_placeholder(combined_text)
                thinking_acc = combined_thinking
                continue

            # 无 tool_calls — 检查是否有真实文本
            has_real_text = bool(combined_text) and combined_text != DEFAULT_PLACEHOLDER
            if not has_real_text:
                log.warning("empty_response_final", round=round_num)
                return {
                    "status": "empty",
                    "content": "",
                    "thinking": combined_thinking,
                    "tool_calls": tool_history,
                    "tool_turn_messages": tool_turn_acc,
                    "rounds": round_num + 1,
                    "usage": last_usage,
                }

            # 有真实文本 — 剥离占位符，结束
            # FIX(text-acc): 只用最终轮的文本 (new_text)，不拼接中间轮。
            # 中间轮文本已通过 line 777 的 per-round assistant 消息保存在
            # tool_turn_acc 中，无需在最终消息中重复。之前的 combined_text
            # 会拼接所有轮的文本，导致同一分析语句在最终消息中重复 3-5 次，
            # 进而污染 conversation_turns 和下一轮的 LLM 上下文。
            final_text = self._strip_placeholder(new_text)
            final_msg = {"role": "assistant", "content": final_text}
            tool_turn_acc.append(final_msg)

            log.info("stream_complete",
                     agent_id=agent_id,
                     text_len=len(final_text),
                     rounds=round_num + 1)

            return {
                "status": "ok",
                "content": final_text,
                "thinking": combined_thinking,
                "tool_calls": tool_history,
                "tool_turn_messages": tool_turn_acc,
                "rounds": round_num + 1,
                "usage": last_usage,
            }

        # 达到最大轮次 — 做一次无工具的总结调用
        log.warning("max_rounds_reached",
                    agent_id=agent_id,
                    max_rounds=rounds_cap)
        summary = await self._make_max_rounds_summary(
            agent_id, provider, messages, on_delta
        )
        # FIX(text-acc): 只用 summary，不拼接 text_acc。
        # summary 是专门的 LLM 调用，已概括全部进展。拼接 text_acc 会引入
        # 所有中间轮的重复文本（同正常退出路径的修复逻辑）。
        final_text = self._strip_placeholder(summary)
        final_msg = {"role": "assistant", "content": final_text}
        tool_turn_acc.append(final_msg)

        return {
            "status": "ok",
            "content": final_text,
            "thinking": thinking_acc,
            "tool_calls": tool_history,
            "tool_turn_messages": tool_turn_acc,
            "rounds": rounds_cap,
            "usage": last_usage,
        }

    # ── 单轮流式请求（带空响应重试）──────────────────────────

    async def _stream_with_empty_retry(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        round_num: int,
    ) -> dict:
        """单轮流式请求，空响应时退避重试（最多 3 次）。"""
        last_result: dict | None = None

        for attempt in range(EMPTY_RESPONSE_MAX_RETRIES + 1):
            result = await self._stream_single_round(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=messages,
                tools=tools,
                on_delta=on_delta,
                round_num=round_num,
                delta_id=f"r{round_num}_{attempt}_{uuid.uuid4().hex[:6]}",
            )

            if result["status"] == "error":
                return result

            # 检查空响应: 无文本 + 无 tool_calls
            is_empty = (
                not result.get("text")
                and not result.get("tool_calls")
            )
            if not is_empty:
                return result

            last_result = result
            if attempt < EMPTY_RESPONSE_MAX_RETRIES:
                backoff_ms = EMPTY_RESPONSE_BACKOFF_MS[attempt]
                log.info("empty_response_retry",
                         agent_id=agent_id, round=round_num,
                         attempt=attempt + 1, backoff_ms=backoff_ms)
                await self._fire_delta(on_delta, {
                    "type": "text_delta",
                    "content": "（收到空响应，正在重试…）\n",
                    "delta_id": f"empty_retry_{round_num}_{attempt}",
                })
                await asyncio.sleep(backoff_ms / 1000.0)

        # 空响应重试耗尽
        log.warning("empty_response_exhausted",
                    agent_id=agent_id, round=round_num)
        return last_result or {
            "status": "empty",
            "text": "",
            "thinking": "",
            "tool_calls": [],
            "finish_reason": None,
        }

    # ── 单轮流式请求（带 HTTP 重试）──────────────────────────

    async def _stream_single_round(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        round_num: int,
        delta_id: str,
    ) -> dict:
        """发起单轮流式 HTTP 请求，解析 SSE，返回本轮结果。

        带 HTTP 重试（429/503/504/529 + 网络错误），首 chunk 超时检测。
        """
        url = provider.build_url()
        headers = provider.build_headers()

        # FIX(gateway-tool-id-400): opencode zen go 网关（Console Go）在请求
        # 尾部为 tool/system 消息时，会校验尾部 tool_call id 链的签名；
        # 跨连接/跨节点回声历史 id 会被判为未知 id，整包拒绝并返回
        # HTTP 400 invalid_request_error（agent 多轮工具循环被 doom 的根因，
        # 实测：末尾为 tool 消息 + 非本网关签发 id → 必 400）。
        # 在请求末尾追加一条静态 user 哨兵消息即可跳过该校验（实测 200），
        # 模型行为不受影响。注意只追加到请求副本，不回写 messages，
        # 避免污染 tool_turn_messages 持久化历史。
        req_messages = messages
        if req_messages and req_messages[-1].get("role") != "user":
            req_messages = [
                *req_messages,
                {"role": "user", "content": CONTINUE_SENTINEL},
            ]
        body = provider.build_body(
            messages=req_messages,
            stream=True,
            tools=tools,
        )

        body_json = json.dumps(body, ensure_ascii=False)
        log.info("http_request",
                 agent_id=agent_id, round=round_num,
                 url=url, body_size=len(body_json))

        async def do_request() -> dict:
            # Bug B fix: 全局并发控制 — 在 HTTP 请求级别限流
            sem = _get_llm_semaphore()
            async with sem:
                return await self._do_streaming_request(
                    agent_id=agent_id,
                    provider=provider,
                    url=url,
                    headers=headers,
                    body=body,
                    on_delta=on_delta,
                    delta_id=delta_id,
                    round_num=round_num,
                )

        try:
            result = await self._retry_handler.with_retry(do_request)
            # 成功完成 → 报告熔断器成功（C10: 按轮次精确上报）
            await self._circuit_breaker.report_success(provider_name)
            return result
        except RetryableError as e:
            # 可重试错误耗尽 → 报告熔断器失败（C10: 让熔断器感知 HTTP 429/503/504/529 + 网络错误）
            await self._circuit_breaker.report_failure(provider_name)
            return {
                "status": "error",
                "text": "",
                "thinking": "",
                "tool_calls": [],
                "finish_reason": None,
                "error": str(e),
            }
        except PermanentError as e:
            # 不可重试错误（401/400 等）→ 不报告熔断器
            # （客户端配置问题，非 provider 故障，不应触发熔断）
            return {
                "status": "error",
                "text": "",
                "thinking": "",
                "tool_calls": [],
                "finish_reason": None,
                "error": str(e),
            }

    # ── 实际流式 HTTP 请求（线程池 + 同步 httpx）────────────────

    async def _do_streaming_request(
        self,
        agent_id: str,
        provider: ProviderConfig,
        url: str,
        headers: dict[str, str],
        body: dict,
        on_delta: DeltaCallback | None,
        delta_id: str,
        round_num: int,
    ) -> dict:
        """执行 HTTP 流式请求（同步 httpx 跑在线程池里，事件边收边推）。

        Windows 上 asyncio CancelledError 无法中断 httpx 的 socket read，
        因此改用同步 httpx.Client + run_in_executor。同步版的超时走
        socket.settimeout()（OS 级）。

        真流式：线程内解析 SSE 后通过 queue 推到事件循环，立刻 _fire_delta，
        避免整包收完才刷新（否则 UI 长时间冻住，误判为 streaming 僵尸）。
        """
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        read_to = httpx.Timeout(read=READ_TIMEOUT_S, connect=10, write=10, pool=10)

        loop = asyncio.get_running_loop()
        event_q: asyncio.Queue = asyncio.Queue()
        _DONE = object()
        _ERR = object()

        def _run_sync() -> None:
            """在线程中执行：HTTP 请求 + SSE 解析，事件即时入队。"""
            http_client = httpx.Client(timeout=read_to)
            try:
                with http_client.stream(
                    "POST", url, headers=headers, content=body_bytes,
                ) as response:
                    if response.status_code != 200:
                        body_text = response.read().decode(
                            "utf-8", errors="replace"
                        )[:500]
                        loop.call_soon_threadsafe(
                            event_q.put_nowait,
                            (
                                _ERR,
                                {
                                    "ok": False,
                                    "http_status": response.status_code,
                                    "body": body_text,
                                    "headers": dict(response.headers),
                                },
                            ),
                        )
                        return
                    decoder = codecs.getincrementaldecoder("utf-8")(
                        errors="replace"
                    )
                    buffer = ""
                    for raw in response.iter_bytes():
                        text = decoder.decode(raw)
                        if text:
                            buffer += text
                            parsed, buffer = parse_sse(buffer)
                            for ev in parsed:
                                loop.call_soon_threadsafe(
                                    event_q.put_nowait, ("event", ev)
                                )
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        buffer += tail
                        parsed, _ = parse_sse(buffer)
                        for ev in parsed:
                            loop.call_soon_threadsafe(
                                event_q.put_nowait, ("event", ev)
                            )
                loop.call_soon_threadsafe(event_q.put_nowait, (_DONE, None))
            except httpx.ReadTimeout:
                loop.call_soon_threadsafe(
                    event_q.put_nowait,
                    (_ERR, {"ok": False, "timeout": True}),
                )
            except httpx.ConnectError as e:
                loop.call_soon_threadsafe(
                    event_q.put_nowait,
                    (_ERR, {"ok": False, "connect_error": str(e)}),
                )
            except Exception as e:
                loop.call_soon_threadsafe(
                    event_q.put_nowait,
                    (_ERR, {"ok": False, "error": str(e)}),
                )
            finally:
                http_client.close()

        deadline = FIRST_CHUNK_TIMEOUT_S + READ_TIMEOUT_S + 10
        executor_task = loop.run_in_executor(None, _run_sync)

        text_acc = ""
        thinking_acc = ""
        tool_call_deltas: list[dict] = []
        finish_reason: str | None = None
        usage: dict | None = None

        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        event_q.get(), timeout=deadline
                    )
                except asyncio.TimeoutError:
                    raise RetryableError(
                        f"HTTP request timed out after {deadline}s"
                    )

                if kind is _DONE:
                    break
                if kind is _ERR:
                    raw = payload
                    if raw.get("timeout"):
                        raise RetryableError(
                            f"HTTP read timeout ({READ_TIMEOUT_S}s)"
                        )
                    if raw.get("connect_error"):
                        raise RetryableError(
                            f"Connection error: {raw['connect_error']}"
                        )
                    if raw.get("http_status"):
                        if is_retryable_status(raw["http_status"]):
                            raise RetryableError(
                                f"HTTP {raw['http_status']}: "
                                f"{raw['body'][:500]}",
                                status=raw["http_status"],
                                headers=raw.get("headers", {}),
                            )
                        raise PermanentError(
                            f"HTTP {raw['http_status']}: {raw['body'][:500]}",
                            status=raw["http_status"],
                        )
                    raise RetryableError(
                        raw.get("error", "Unknown HTTP error")
                    )

                event = payload
                if not isinstance(event, dict):
                    continue
                extracted = provider.extract_usage(event)
                if extracted:
                    usage = extracted
                for c in provider.parse_stream_chunk(event):
                    ctype = c.get("type")
                    if ctype == "text":
                        content = c["content"]
                        await self._fire_delta(on_delta, {
                            "type": "text_delta", "content": content,
                            "delta_id": delta_id})
                        text_acc += content
                    elif ctype == "reasoning":
                        content = c["content"]
                        await self._fire_delta(on_delta, {
                            "type": "thinking_delta", "content": content,
                            "delta_id": delta_id})
                        thinking_acc += content
                    elif ctype == "tool_call_delta":
                        tool_call_deltas.append(c["tool_call"])
                    elif ctype == "tool_call_start":
                        tc = c.get("tool_call", {})
                        if tc:
                            tool_call_deltas.append(tc)
                    elif ctype == "tool_call_end":
                        pass
                    elif ctype in ("thinking_start", "thinking_signature",
                                   "message_stop"):
                        pass
                    elif ctype == "usage":
                        u = c.get("usage", {})
                        if u:
                            usage = usage or {}
                            usage.update(u)
                    elif ctype == "finish":
                        finish_reason = (
                            c.get("reason")
                            or c.get("finish_reason")
                            or finish_reason
                        )
                    elif ctype == "error":
                        log.warning(
                            "sse_error_chunk",
                            agent_id=agent_id,
                            error=c.get("content"),
                        )
        finally:
            try:
                await executor_task
            except Exception:
                pass

        tool_calls = merge_tool_calls([], tool_call_deltas)
        cache_read = (usage or {}).get("cache_read", 0)
        cache_creation = (usage or {}).get("cache_creation", 0)
        if cache_read or cache_creation:
            log.info(
                "prompt_cache_hit",
                agent_id=agent_id,
                round=round_num,
                cache_read=cache_read,
                cache_creation=cache_creation,
                input_tokens=(usage or {}).get("input", 0),
            )
        log.info("round_http_done", agent_id=agent_id, round=round_num,
                 text_len=len(text_acc), tool_count=len(tool_calls),
                 finish=finish_reason)
        return {"status": "ok", "text": text_acc, "thinking": thinking_acc,
                "tool_calls": tool_calls, "finish_reason": finish_reason,
                "usage": usage}

    # ── SSE 迭代器（带首 chunk + idle 超时）──────────────────

    async def _iter_sse_with_timeout(
        self,
        response: httpx.Response,
    ) -> AsyncIterator[dict]:
        """带超时的 SSE 事件迭代器。

        双超时机制：
        1. httpx 原生 read timeout (95s) — socket 级，Windows 可靠
        2. time.monotonic() 跟踪 — 应用级，在收到数据后检查 deadline

        不再依赖 asyncio.wait_for 取消 __anext__() —— Windows 上 CancelledError
        可能无法中断 httpx 的底层 socket read。

        BUG-009/012/013 修复：用增量 UTF-8 解码器（codecs.getincrementaldecoder）
        替代逐 chunk `raw.decode("utf-8", errors="replace")`。后者会在多字节字符
        被网络分片切断时产生 U+FFFD，导致中文花名/消息/工具参数损坏（mojibake）。
        增量解码器跨 chunk 缓冲未完成字节，正确重组字符。
        """
        buffer = ""
        first_received = False
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        start_time = time.monotonic()
        last_event_time = start_time

        async for raw in response.aiter_bytes():
            now = time.monotonic()

            # Per-chunk deadline check (belt, httpx read timeout is suspenders)
            if not first_received:
                if now - start_time > FIRST_CHUNK_TIMEOUT_S:
                    raise asyncio.TimeoutError(
                        f"First chunk timeout ({FIRST_CHUNK_TIMEOUT_S}s)"
                    )
            else:
                if now - last_event_time > IDLE_TIMEOUT_S:
                    raise asyncio.TimeoutError(
                        f"Stream idle timeout ({IDLE_TIMEOUT_S}s)"
                    )

            last_event_time = now

            if not raw:
                continue

            if not first_received:
                first_received = True

            text = decoder.decode(raw)
            if not text:
                continue
            buffer += text

            # 解析完整的 SSE 事件
            events, buffer = parse_sse(buffer)
            for event in events:
                yield event

        # flush 增量解码器残余字节 + 处理流结束后剩余的 buffer
        tail = decoder.decode(b"", final=True)
        if tail:
            buffer += tail
        if buffer:
            events, _ = parse_sse(buffer)
            for event in events:
                yield event

    # ── 工具执行 ────────────────────────────────────────────

    async def _execute_tools(
        self,
        agent_id: str,
        tool_calls: list[dict],
        on_tool_call: ToolCallCallback,
        on_delta: DeltaCallback | None,
    ) -> tuple[list[dict], set[str]]:
        """执行一批工具调用，返回 (tool result 消息列表, 出错的 tool_call_id 集合)。

        并行执行独立的工具调用（对齐 Elixir Task.Supervisor.async_nolink）。
        error_ids 供 doom 检测区分"盲目重复"与"失败后重试"（后者是合法行为）。
        """
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
            self._execute_single_tool(agent_id, tc, on_tool_call)
            for tc in tool_calls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_results: list[dict] = []
        error_ids: set[str] = set()
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
                ):
                    error_ids.add(tc["id"])
            tool_results.append({
                "role": "tool",
                "content": content,
                "tool_call_id": tc["id"],
            })
            # 广播 tool_result
            await self._fire_delta(on_delta, {
                "type": "tool_result",
                "tool_call_id": tc["id"],
                "content": content,
            })

        return tool_results, error_ids

    async def _execute_single_tool(
        self,
        agent_id: str,
        tool_call: dict,
        on_tool_call: ToolCallCallback,
    ) -> dict:
        """执行单个工具调用（带 120s 超时）。"""
        tool_name = tool_call["name"]
        arguments = tool_call["arguments"]
        tool_call_id = tool_call["id"]

        log.info("tool_execute",
                 agent_id=agent_id,
                 tool=tool_name,
                 args_len=len(arguments))

        try:
            result = await asyncio.wait_for(
                on_tool_call(tool_name, arguments, tool_call_id),
                timeout=TOOL_EXECUTION_TIMEOUT_S,
            )
            return result
        except TimeoutError:
            log.error("tool_timeout",
                      agent_id=agent_id, tool=tool_name)
            return {"content": f"[Tool Timeout] {tool_name} did not complete within {TOOL_EXECUTION_TIMEOUT_S}s"}

    # ── Doom loop 检测 ──────────────────────────────────────

    @staticmethod
    def _detect_doom_loop(
        tool_calls: list[dict],
        tracker: dict[str, Any],
    ) -> str | None:
        """检测 doom loop: 同一工具+同一参数连续超过工具专属限制。

        不同工具有不同的容忍度（见 DOOM_LOOP_TOOL_LIMITS）：
        - 只读工具 10-15 次 — 探索式重复查询是正常的
        - 审查工具 6 次 — LLM 可能在纠正输出格式
        - 幂等写入 8 次 — 覆盖写入无害但不应无限
        - 副作用工具 3 次 — bash/apply_patch 严格限制

        失败重试豁免（井字棋实测 #1）：上一轮同参数调用**执行失败**时，
        本次同参数调用是合法重试（如 commit_turn 被出口闸门拒收后履行义务
        再提交），不计 doom。真正的死循环仍由 MAX_ROUNDS / 无进展熔断兜底。

        遇到不同调用时重置计数。更新 tracker 并返回触发 doom loop 的工具名，或 None。
        """
        last_key = tracker.get("last_key")
        count = tracker.get("count", 0)
        last_errored = tracker.get("last_errored", False)
        for tc in tool_calls:
            key = (tc["name"], tc["arguments"])
            if key == last_key:
                if last_errored:
                    # 失败后重试：计数保持不变，且豁免标志只消费一次
                    last_errored = False
                else:
                    count += 1
            else:
                last_key = key
                count = 1
                last_errored = False
            limit = DOOM_LOOP_TOOL_LIMITS.get(tc["name"], DOOM_LOOP_DEFAULT_LIMIT)
            if count >= limit:
                tracker["last_key"] = last_key
                tracker["count"] = count
                tracker["last_errored"] = last_errored
                return tc["name"]
        tracker["last_key"] = last_key
        tracker["count"] = count
        tracker["last_errored"] = last_errored
        return None

    # ── 上下文溢出修剪 ──────────────────────────────────────

    #: Prune 保护窗口（token）— 最近工具输出保留原文
    _PRUNE_PROTECT_TOKENS = 40_000
    #: Prune 最低收益（token）— 候选总量不足此值则不执行
    _PRUNE_MINIMUM_TOKENS = 10_000
    #: Prune 占位符
    _PRUNE_PLACEHOLDER = "[Old tool result content cleared]"

    def _prune_old_tool_outputs(self, messages: list[dict]) -> list[dict]:
        """在 tool loop 中裁剪旧工具输出（OpenCode prune 模式，临时版）。

        逆序遍历：跳过最近 2 轮（assistant 消息计轮次），保护窗口(40K)外的
        旧 tool 输出替换为占位符。候选总量 > 10K 时才执行。
        """
        if len(messages) < 6:
            return messages

        to_prune_indices: list[int] = []
        protected = 0
        turns = 0

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "assistant":
                turns += 1
            if turns < 2:
                continue

            if "tool_call_id" not in msg:
                continue

            # 已被裁剪过 → 停止
            if msg.get("content") == self._PRUNE_PLACEHOLDER:
                break

            tokens = estimate_tokens_for_messages([msg])
            new_protected = protected + tokens
            if new_protected <= self._PRUNE_PROTECT_TOKENS:
                protected = new_protected
            else:
                to_prune_indices.append(i)

        if not to_prune_indices:
            return messages

        prune_tokens = sum(
            estimate_tokens_for_messages([messages[i]]) for i in to_prune_indices
        )
        if prune_tokens < self._PRUNE_MINIMUM_TOKENS:
            return messages  # 收益不足

        result = list(messages)
        for i in to_prune_indices:
            result[i] = {**result[i], "content": self._PRUNE_PLACEHOLDER}

        log.info(
            "tool_loop_prune",
            pruned_count=len(to_prune_indices),
            pruned_tokens=prune_tokens,
            protected_tokens=protected,
        )
        return result

    def _trim_context_if_needed(
        self,
        messages: list[dict],
        provider: ProviderConfig,
    ) -> list[dict]:
        """上下文溢出检查: 先 prune 旧工具输出，再估算 token，超 usable 则硬截断。

        对齐 Elixir trim_context_if_needed + OpenCode prune 模式。
        """
        # Step 1: Prune 旧工具输出（替换为占位符，不丢弃消息）
        messages = self._prune_old_tool_outputs(messages)

        max_output = provider.max_output_tokens
        if provider.supports_thinking:
            max_output = max(max_output, OUTPUT_TOKEN_GLOBAL_CAP)
        else:
            max_output = min(max_output, OUTPUT_TOKEN_GLOBAL_CAP)

        # 治本：不再用 max(负数, 8192) 掩盖非法配置。
        # 若 context_window - max_output - buffer <= 0，说明配置非法
        # （输出预算吃掉整个窗口），ProviderConfig 构造时本应已拦住。
        # 此处若仍触发 = DB 有脏数据绕过了构造校验，硬失败暴露问题，
        # 绝不静默 floor 到 8192 后带病发请求（那会导致 400 且原因难定位）。
        input_budget = provider.context_window - max_output - SAFETY_BUFFER_TOKENS
        if input_budget <= 0:
            raise ValueError(
                f"非法模型配置：context_window={provider.context_window:,} - "
                f"max_output={max_output:,} - safety_buffer={SAFETY_BUFFER_TOKENS:,} "
                f"= {input_budget}（输入预算 <= 0）。输出预算吃掉整个窗口，"
                f"请修复模型配置的 max_output_tokens。"
            )
        # 合法小窗口模型的兜底：input_budget > 0 但小于 8192 时，
        # 保证输入至少有 8192 可用（此时 max_output 会被 cap 到不超限）。
        usable = max(input_budget, 8_192)
        total = estimate_tokens_for_messages(messages)

        if total <= usable:
            return messages

        log.info("context_overflow_trim", total=total, usable=usable)

        # 保留首 2 条（system prompt）+ 末 N 条（最近上下文）
        if len(messages) <= 4:
            return messages

        head = messages[:2]
        tail = messages[2:]

        # 从 tail 前端逐步裁剪直到 token 数达标
        while len(tail) > 2 and estimate_tokens_for_messages(head + tail) > usable:
            # R3: 保持 tool_calls + tool_result 对的完整性，避免产生孤儿 tool_result
            # （没有对应 tool_calls 的 tool_result 会导致 API 400 错误）。
            # 原实现只检查相邻 2 条，多 tool_result 批次会留下孤儿。
            first = tail[0]
            drop = 1
            if "tool_calls" in first:
                # assistant(tool_calls) — 连同其后所有同批 tool_result 一起裁剪
                drop = 1
                while drop < len(tail) and "tool_call_id" in tail[drop]:
                    drop += 1
            elif "tool_call_id" in first:
                # 孤儿 tool_result（其 tool_calls 已被裁剪）— 裁剪它及后续同批 tool_result
                drop = 0
                while drop < len(tail) and "tool_call_id" in tail[drop]:
                    drop += 1
            tail = tail[drop:]

        # R3: 最终清理 — 移除裁剪后可能残留在 tail 头部的孤儿 tool_result
        # （循环可能因 len(tail)<=2 提前退出而留下孤儿）
        while tail and "tool_call_id" in tail[0]:
            tail = tail[1:]

        trimmed = head + tail
        log.info("context_trimmed",
                 original=len(messages), trimmed=len(trimmed),
                 tokens=estimate_tokens_for_messages(trimmed))
        return trimmed

    # ── 中轮提醒 ────────────────────────────────────────────

    def _maybe_inject_mid_round_reminder(
        self,
        messages: list[dict],
        round_num: int,
        rounds_cap: int | None = None,
    ) -> list[dict]:
        """80% 轮次时注入「开始收尾」系统提示。"""
        cap = rounds_cap if rounds_cap else self.max_tool_rounds
        reminder_round = max(int(cap * MID_ROUND_REMINDER_RATIO), 1)
        if round_num == reminder_round and round_num < cap:
            rounds_left = cap - round_num
            log.info("inject_mid_round_reminder",
                     round=round_num, rounds_left=rounds_left)
            messages = messages + [{
                "role": "system",
                "content": (
                    f"⚠️ You have {rounds_left} tool calls remaining. "
                    "Start wrapping up: finish critical actions now and prepare a summary."
                ),
            }]
        return messages

    # ── 最大轮次总结 ────────────────────────────────────────

    async def _make_max_rounds_summary(
        self,
        agent_id: str,
        provider: ProviderConfig,
        messages: list[dict],
        on_delta: DeltaCallback | None,
    ) -> str:
        """达到最大轮次后，做一次无工具的总结调用。"""
        summary_prompt = (
            "CRITICAL — MAXIMUM TOOL ROUNDS REACHED\n\n"
            "You have reached the maximum number of tool calls for this turn. "
            "Tools are now disabled.\n\n"
            "You MUST respond with a text summary. Include:\n"
            "1. What you have accomplished so far\n"
            "2. What tasks remain incomplete\n"
            "3. Recommended next steps\n\n"
            "Respond with text ONLY. Do NOT attempt any tool calls."
        )
        summary_messages = messages + [{"role": "user", "content": summary_prompt}]

        url = provider.build_url()
        headers = provider.build_headers()
        body = provider.build_body(
            messages=summary_messages,
            stream=False,
            temperature=0.3,
            tools=None,
        )

        client = provider.build_client()
        try:
            resp = await client.post(
                url, headers=headers,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                if choices:
                    content = choices[0].get("message", {}).get("content")
                    if content:
                        await self._fire_delta(on_delta, {
                            "type": "text_delta",
                            "content": content,
                            "delta_id": "summary",
                        })
                        return content
            log.warning("summary_request_failed", status=resp.status_code)
            return "⚠️ Reached max tool rounds. Some tasks may be incomplete."
        except Exception as e:
            log.warning("summary_request_error", error=str(e))
            return "⚠️ Reached max tool rounds. Some tasks may be incomplete."
        finally:
            await client.aclose()

    # ── 辅助方法 ────────────────────────────────────────────

    @staticmethod
    async def _read_error_body(response: httpx.Response) -> str:
        """读取错误响应体（限制 500 字符）。"""
        try:
            body = await response.aread()
            return body.decode("utf-8", errors="replace")[:500]
        except Exception:
            return "(streaming body)"

    @staticmethod
    def _strip_placeholder(text: str) -> str:
        """剥离开头的占位文本（不计为真实 LLM 输出）。

        Bug-5 修复: 用 while 循环剥除所有重复出现的占位符（防御旧消息历史
        中可能存在的累积占位符）。
        """
        if not text:
            return text
        while text.startswith(DEFAULT_PLACEHOLDER):
            text = text[len(DEFAULT_PLACEHOLDER):]
        return text

    @staticmethod
    async def _fire_delta(on_delta: DeltaCallback | None, event: dict) -> None:
        """触发 delta 回调（支持同步/异步）。"""
        if on_delta is None:
            return
        result = on_delta(event)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _error_result(message: str, start_time: float) -> dict:
        """构建错误结果 dict。"""
        return {
            "status": "error",
            "content": "",
            "thinking": "",
            "tool_calls": [],
            "tool_turn_messages": [],
            "rounds": 0,
            "usage": None,
            "error": message,
            "duration_ms": int((time.monotonic() - start_time) * 1000),
        }
