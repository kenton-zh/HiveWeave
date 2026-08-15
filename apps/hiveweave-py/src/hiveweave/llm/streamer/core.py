"""Streamer orchestrator — thin class composing mixins."""
from __future__ import annotations

import asyncio
import time

import httpx
import structlog

from hiveweave.llm.circuit_breaker import CircuitBreaker, circuit_breaker
from hiveweave.llm.provider import ProviderFactory, provider_factory
from hiveweave.llm.retry import RetryHandler

from .constants import (
    DEFAULT_PLACEHOLDER,
    HARD_TOTAL_TIMEOUT_S,
    MAX_TOOL_ROUNDS,
    session_wall_clock_enabled,
)
from .context import ContextMixin
from .errors import CircuitBreakerOpenError
from .http_stream import HttpStreamMixin
from .tool_exec import ToolExecMixin
from .tool_loop import ToolLoopMixin
from .types import DeltaCallback, ToolCallCallback

log = structlog.get_logger(__name__)


class Streamer(
    ToolLoopMixin,
    HttpStreamMixin,
    ToolExecMixin,
    ContextMixin,
):
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
                        # Tier guard: refuse cross-tier fallback
                        orig_tier = model_config.get("tier")
                        fb_tier = fallback_config.get("tier")
                        if orig_tier and fb_tier and orig_tier != fb_tier:
                            log.warning(
                                "circuit_fallback_tier_mismatch",
                                from_provider=provider_name,
                                orig_tier=orig_tier,
                                fallback_tier=fb_tier,
                            )
                        else:
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
            # Session wall clock is opt-in. Default: no outer wait_for —
            # stream idle + declared tool timeouts stop hung work.
            loop_coro = self._run_tool_loop(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=list(messages),
                tools=tools,
                on_delta=on_delta,
                on_tool_call=on_tool_call,
                max_tool_rounds=effective_max_rounds,
            )
            if session_wall_clock_enabled():
                result = await asyncio.wait_for(
                    loop_coro,
                    timeout=HARD_TOTAL_TIMEOUT_S + 30.0,
                )
            else:
                result = await loop_coro
            # 熔断器成功/失败上报已移至 _stream_single_round 按轮次精确上报（C10）
            result["duration_ms"] = int((time.monotonic() - start_time) * 1000)
            return result
        except TimeoutError:
            # Ultimate safety net — loop should have exited gracefully first.
            log.error(
                "stream_hard_timeout",
                agent_id=agent_id,
                timeout_s=HARD_TOTAL_TIMEOUT_S,
            )
            try:
                from hiveweave.services.telemetry import telemetry
                telemetry.stream_total_timeout(agent_id)
            except Exception:
                pass
            await self._fire_delta(on_delta, {
                "type": "error",
                "content": f"请求总超时（{HARD_TOTAL_TIMEOUT_S}s）",
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
            "usage_rounds": [],  # token metering: 错误路径无成功轮次数据
            "error": message,
            "duration_ms": int((time.monotonic() - start_time) * 1000),
        }

