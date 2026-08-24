"""Streamer orchestrator — thin class composing mixins."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from hiveweave.llm.circuit_breaker import CircuitBreaker, circuit_breaker
from hiveweave.llm.provider import ProviderFactory, provider_factory
from hiveweave.llm.retry import RetryHandler

from .constants import (
    DEFAULT_PLACEHOLDER,
    HARD_TOTAL_TIMEOUT_S,
    MAX_TOOL_ROUNDS,
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
        # 初值；每次 stream() 入口重置（同一实例可能因 empty 重试/failover
        # 被连续调用，跨 attempt 不得携带脏标志）
        self._context_rewrote = False

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
        steer_queue: asyncio.Queue | None = None,
        skip_providers: set[str] | None = None,
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
        # 每次 stream() 调用重置：empty 重试 / 同层 failover 会复用同一实例
        # 连续调用 stream()，上一 attempt 的改写标志不得泄漏到下一 attempt。
        self._context_rewrote = False
        provider = self._provider_factory.create(model_config)
        provider_name = model_config.get("name") or "primary"
        # E6: fallback 递归防环 —— 已尝试过的 provider 不再回跳（A→B→A 停）。
        tried = set(skip_providers or ())
        tried.add(provider_name)

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

        # 注册熔断器（如果未注册）。E6: fallback 覆盖从「模型行手填字段」扩展到
        # 「同 tier 备份」——模型行 fallback 为空时由 tier 主备配置推导注入。
        effective_fallback = await self._resolve_fallback_name(
            provider, model_config, tried
        )
        await self._circuit_breaker.register(
            provider_name, fallback=effective_fallback
        )

        # 熔断器检查（C9: fallback 不再是无操作死代码 — 直接抛出明确异常）
        cb_result = await self._circuit_breaker.check(provider_name)
        if not cb_result.allowed:
            # Bug J fix: 如果有 fallback provider，自动切换重试
            if cb_result.fallback and cb_result.fallback not in tried:
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
                                steer_queue=steer_queue,
                                skip_providers=tried,
                            )
                except Exception as fb_err:
                    log.warning("circuit_fallback_failed",
                                fallback=cb_result.fallback,
                                error=str(fb_err))
            # E6: 无有效 fallback 不再裸抛 —— 返回 error result（error_status=503），
            # 让 agent 层 is_retryable=True → 走既有同 tier failover 通道；
            # failover 无解 → 正常 handle_error（配额风暴落到 E7 容量处理）。
            return self._breaker_open_error(provider_name, start_time, tried)

        # 广播 start 事件
        await self._fire_delta(on_delta, {"type": "start"})

        try:
            # Turn budget（写死启用，见 constants.py 顶部说明）：外层
            # wait_for 是最终兜底 — 循环内闸口应先优雅收口。
            loop_coro = self._run_tool_loop(
                agent_id=agent_id,
                provider=provider,
                provider_name=provider_name,
                messages=list(messages),
                tools=tools,
                on_delta=on_delta,
                on_tool_call=on_tool_call,
                max_tool_rounds=effective_max_rounds,
                steer_queue=steer_queue,
            )
            result = await asyncio.wait_for(
                loop_coro,
                timeout=HARD_TOTAL_TIMEOUT_S + 30.0,
            )
            # 熔断器成功/失败上报已移至 _stream_single_round 按轮次精确上报（C10）
            result["duration_ms"] = int((time.monotonic() - start_time) * 1000)
            # 前缀改写信号：completion 据此决定是否把等价裁剪回写 DB。
            result["context_rewritten"] = self._context_rewrote
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
    async def _resolve_fallback_name(
        provider: Any, model_config: dict, tried: set[str]
    ) -> str | None:
        """E6: 有效熔断 fallback —— 模型行 fallback 为空时从同 tier 备份推导。

        语义对齐 agent._resolve_failover_backup：skip 当前模型 + same-api-key
        守卫（同 key = 共享配额，切换无意义，事故主备同 key 即此情形）。
        已在 tried（递归防环）内的 provider 不再返回。推导失败 fail-open 返回
        None（维持「no fallback available」语义，不改变既有行为）。
        """
        configured = getattr(provider, "fallback", None)
        if isinstance(configured, str) and configured and configured not in tried:
            # 审计修正：手填 fallback 与推导路径同守 same-key 闸（同 key =
            # 共享配额池，切换无意义，事故主备同 key 即此情形）。
            try:
                from hiveweave.services.model import ModelService

                fb_cfg = await ModelService().get(configured)
                if not fb_cfg or not fb_cfg.get("is_active"):
                    return None
                fb_key = str(fb_cfg.get("api_key") or "")[:16]
                failed_key = str(model_config.get("api_key") or "")[:16]
                if failed_key and fb_key and fb_key == failed_key:
                    return None
                return configured
            except Exception:
                return None
        tier = model_config.get("tier")
        if not tier:
            return None
        try:
            from hiveweave.services.model import ModelService

            current_id = model_config.get("id")
            failed_key = str(model_config.get("api_key") or "")[:16]
            svc = ModelService()
            skip = {current_id} if current_id else None
            backup = await svc.resolve_model(tier=tier, skip_model_ids=skip)
            if not backup:
                return None
            name = backup.get("name")
            if not name or name in tried:
                return None
            backup_key = str(backup.get("api_key") or "")[:16]
            if failed_key and backup_key == failed_key:
                log.info(
                    "fallback_skip_same_key",
                    model=model_config.get("model_id"),
                    tier=tier,
                )
                return None
            return str(name)
        except Exception as e:
            log.debug("fallback_derive_failed", error=str(e))
            return None

    @staticmethod
    def _breaker_open_error(
        provider_name: str, start_time: float, tried: set[str]
    ) -> dict:
        """E6: 熔断打开且无有效 fallback → 503 error result（不进重试裸抛）。

        让 agent 层的 is_retryable 判定（429+5xx）接住，走既有同 tier
        failover；failover 无解 → 正常 handle_error → 配额风暴交 E7。
        """
        msg = (
            f"Circuit breaker open for provider '{provider_name}' "
            f"and no fallback available (tried={sorted(tried)})"
        )
        return Streamer._error_result(
            msg, start_time, error_status=503, error_headers={}
        )

    @staticmethod
    def _error_result(
        message: str,
        start_time: float,
        error_status: int | None = None,
        error_headers: dict[str, str] | None = None,
    ) -> dict:
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
            **(
                {"error_status": error_status}
                if error_status is not None
                else {}
            ),
            **({"error_headers": error_headers} if error_headers else {}),
        }

