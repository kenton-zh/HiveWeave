"""Streamer orchestrator — thin class composing mixins."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

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
        usage_sink: Callable[[dict], None] | None = None,
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
        # #22（2026-09-16）：**错误 result 必须带「这次用的哪个 provider/model」**。
        # 现场：58/59 因 `RegionError`（HTTP 403「This model is not available in
        # your country.」）停摆，而 agent_events 的 payload 只有
        # `{error, error_type}` ⇒ 事后无法回答"是**哪个模型**被 region 拦"
        # （对比 `llm_unknown_error_sample` 是带 provider/model 的）。
        # 在**这里**盖一次即可覆盖全部 7 个错误出口（`_error_result` /
        # `http_stream` 的 4 处 / `tool_loop` 的 2 处）—— 逐个去改会立刻长出
        # "某条出口忘了带"的静默缺口（本仓在事实位白名单上栽过两次）。
        # ⚠ 只盖 `status == "error"` 的结果 ⇒ 正常流**逐字节不变**（缓存前缀纪律）。
        _llm_identity = {
            "provider": provider_name,
            "model": (
                getattr(provider, "model_name", "")
                or model_config.get("model_id")
                or ""
            ),
        }

        def _stamp_error_identity(res: Any) -> Any:
            if isinstance(res, dict) and res.get("status") == "error":
                for _k, _v in _llm_identity.items():
                    if _v:
                        res.setdefault(_k, _v)
            return res
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

        # 熔断器注册（不再注册 fallback —— 自动模型切换已整体移除）。
        await self._circuit_breaker.register(provider_name)

        # 熔断器检查。按 DSH 纪律移除全部自动 provider/模型切换，理由：
        # 1) 换 model = 换缓存域，前缀缓存整条作废（TEST_DSH_29 实测长闲置
        #    请求 100% 零命中，占全价 input token 的 90%）；
        # 2) 切换会在无核算的情况下静默改变模型身份，掩盖真实故障。
        # 熔断打开直接返回 error result（error_status=503），由重试 /
        # park / 容量治理接手。
        cb_result = await self._circuit_breaker.check(provider_name)
        if not cb_result.allowed:
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
                usage_sink=usage_sink,
            )
            result = await asyncio.wait_for(
                loop_coro,
                timeout=HARD_TOTAL_TIMEOUT_S + 30.0,
            )
            # 熔断器成功/失败上报已移至 _stream_single_round 按轮次精确上报（C10）
            result["duration_ms"] = int((time.monotonic() - start_time) * 1000)
            # 前缀改写信号：completion 据此决定是否把等价裁剪回写 DB。
            result["context_rewritten"] = self._context_rewrote
            return _stamp_error_identity(result)
        except TimeoutError:
            # Ultimate safety net — loop should have exited gracefully first.
            log.error(
                "stream_hard_timeout",
                agent_id=agent_id,
                timeout_s=HARD_TOTAL_TIMEOUT_S,
                timeout_kind="turn",  # F7：与工具自身超时（'command'）区分
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
            result = self._error_result("请求总超时", start_time)
            # F7 补出口（TEST_DSH_50/51：真超时+悬挂上 timeout_kind 实测
            # 50% / 0%）。这条是**整轮兜底**超时（外层 wait_for），与工具
            # 自身声明的超时（tool_exec.py 的 `Command timed out after Ns`
            # → timeout_kind='command'）是两类，必须能机检区分。
            # 之前该分支只返回裸 error result，run_steps.timeout_kind 恒 NULL
            # → 「超时不可分类」这件事在 4 个 600s 硬杀 run 上原样残留。
            # 取值 `turn` 为本次新增，schema.py 的 F7 注释已同步。
            #
            # **落点边界（交付后审计指出）**：这两键在 turn 级 result 上，
            # 目前没有消费方会把它写进 run_steps —— `agents/streaming.py:284`
            # 读的是**工具执行结果**，不是本 result。`run_steps.timeout_kind
            # = 'turn'` 的真正落点是 `run_ledger.py` 的孤儿步骤清扫。
            # 这里置位是为了日志可分类 + 给上层留判断入口，不代表步骤级已覆盖。
            result["timeout_kind"] = "turn"
            result["timeout_ms"] = int((HARD_TOTAL_TIMEOUT_S + 30.0) * 1000)
            return _stamp_error_identity(result)
        except Exception as e:
            await self._circuit_breaker.report_failure(provider_name)
            log.exception("stream_error", agent_id=agent_id, error=str(e))
            await self._fire_delta(on_delta, {
                "type": "error", "content": str(e)
            })
            return _stamp_error_identity(self._error_result(str(e), start_time))
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
        """构建错误结果 dict。

        L4（2026-09-11）：**不再携带 ``usage_rounds``**。原键恒为 ``[]`` 且注释
        自称「错误路径无成功轮次数据」—— 那是**越界断言**：整轮硬超时时轮次
        早已跑完，数据就在调用方的 ``usage_sink`` 里。该键的存在正是「两个
        权威源」分叉的根（R11 实测 7/7 终止 run 零账）。
        现在 usage **只经 sink** 推送，本函数不参与记账，也不许声明「没有」。
        """
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
            **(
                {"error_status": error_status}
                if error_status is not None
                else {}
            ),
            **({"error_headers": error_headers} if error_headers else {}),
        }

