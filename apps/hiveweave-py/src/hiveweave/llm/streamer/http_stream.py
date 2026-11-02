"""HTTP streaming request + SSE iteration mixins."""
from __future__ import annotations

import asyncio
import codecs
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
import structlog

from hiveweave.llm.circuit_breaker import UPSTREAM_BREAKER_COOLDOWN_S
from hiveweave.llm.provider import (
    ProviderConfig,
    _IMAGES_OMITTED_NOTE,
    _looks_like_image_unsupported_error,
    mark_image_unsupported,
)
from hiveweave.llm.failover import failover_threshold
from hiveweave.llm.retry import (
    STREAM_IDLE_ERROR_CODE,
    PermanentError,
    RetryAborted,
    RetryableError,
    classify_http_error,
    is_region_unavailable_error,
    is_stream_idle_error,
    is_upstream_death,
    ride_wall_cap_s,
)
from hiveweave.llm.unknown_error_samples import (
    flush_unknown_samples,
    note_unknown_sample,
)
from hiveweave.services.crash_loop import crash_loop_guard_enabled

from .constants import (
    ABORT_POLL_S,
    ABORTED_STOP_REASON,
    continue_sentinel_text,
    EMPTY_RESPONSE_BACKOFF_MS,
    EMPTY_RESPONSE_MAX_RETRIES,
    FIRST_BYTE_TOTAL_CAP_S,
    FIRST_BYTE_TOTAL_TIMEOUT_ERROR_CODE,
    FIRST_CHUNK_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    LLM_QUEUE_PING_S,
    STREAM_NO_TERMINATOR_ERROR_CODE,
    STREAM_SOCKET_CONNECT_TIMEOUT_S,
    STREAM_SOCKET_POOL_TIMEOUT_S,
    STREAM_SOCKET_READ_TIMEOUT_S,
    STREAM_SOCKET_WRITE_TIMEOUT_S,
    TURN_TOTAL_TIMEOUT_ERROR_CODE,
    stream_chunk_wait_s,
    _get_llm_semaphore,
)
from .abort_source import (
    abort_stream,
    register_abort_source,
    unregister_abort_source,
)
from .degenerate_detect import DegenerateDetector
from .sse import merge_tool_calls, parse_sse
from .types import DeltaCallback

log = structlog.get_logger(__name__)


def _strip_images_for_retry(messages: list[dict]) -> list[dict]:
    """降级重试用：去掉所有消息里的图片像素，正文追加缺图说明。

    只改请求副本，不落持久化历史。缺图说明只陈述「图未发出去」，
    不指挥用哪个工具。
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m, dict) and m.get("images"):
            cleaned = {k: v for k, v in m.items() if k != "images"}
            content = cleaned.get("content") or ""
            note = _IMAGES_OMITTED_NOTE.format(n=1)
            base = str(content).rstrip()
            cleaned["content"] = f"{base}\n{note}" if base else note
            out.append(cleaned)
        else:
            out.append(m)
    return out


def classify_stream_socket_timeout(
    *, got_event: bool
) -> RetryableError | PermanentError:
    """httpx socket ReadTimeout: retry only before the first token."""
    msg = f"HTTP read timeout ({STREAM_SOCKET_READ_TIMEOUT_S}s)"
    if got_event:
        return PermanentError(f"{msg} after tokens")
    return RetryableError(msg)


def _first_byte_total_error() -> PermanentError:
    """T3'：零输出总帽到点的终态错误（单一构造点，防两处文案漂移）。"""
    return PermanentError(
        f"Upstream produced zero output for "
        f"{FIRST_BYTE_TOTAL_CAP_S:g}s total wall clock "
        "(across retries/backoff/queue) — first-byte cap exceeded",
        error_code=FIRST_BYTE_TOTAL_TIMEOUT_ERROR_CODE,
    )


class HttpStreamMixin:
    """HTTP / SSE streaming methods for Streamer."""

    if TYPE_CHECKING:
        _fire_delta: Any
        _retry_handler: Any
        _circuit_breaker: Any
        _provider_factory: Any

    # ── T34 故障转移接缝 ────────────────────────────────────

    def _failover_state(self):
        """本 run 的故障转移状态机（T34）。

        住在 mixin 上：测试会裸构造 ``HttpStreamMixin()`` 直调
        ``_stream_single_round``（缺 ``_failover`` 属性时惰性补齐）。
        """
        from hiveweave.llm.failover import RunFailover

        fo = getattr(self, "_failover", None)
        if fo is None:
            fo = RunFailover()
            self._failover = fo
        return fo

    def _effective_provider(self, provider, provider_name):
        """当前生效 provider：run 内已故障转移 ⇒ 按切换后模型重建。

        未切换（active_config 空）原样返回；工厂构建失败时 fail-open
        返回原 provider（调用方的 ``is`` 同一性检查据此防死循环）。
        """
        cfg = self._failover_state().active_config
        if not cfg:
            return provider, provider_name
        try:
            new_provider = self._provider_factory.create(cfg)
        except Exception as e:  # noqa: BLE001 — 构建失败退回原 provider
            log.warning(
                "failover_provider_build_failed",
                error=str(e)[:200],
            )
            return provider, provider_name
        new_name = (
            str(cfg.get("name") or "")
            or str(cfg.get("model_id") or "")
            or provider_name
        )
        return new_provider, new_name

    async def _open_breaker_if_upstream_death(
        self,
        provider_name: str,
        agent_id: str,
        round_num: int,
        exc: BaseException,
    ) -> bool:
        """上游死亡类永久错误（403/RegionError）→ 立即熔断（B+ 快熔）。

        判据 = 跨组契约唯一判据 ``retry.is_upstream_death``；命中即
        ``CircuitBreaker.open_for``（不等 FAIL_THRESHOLD 阈值累计），
        ``upstream_breaker_opened`` 日志（provider/cooldown_s 契约字段）
        只在此一处维护。返回是否命中（供测试/调用方分支）。
        AUTH/参数类（401/400/402）返回 False，维持「不喂熔断」旁路。
        """
        if not is_upstream_death(exc):
            return False
        await self._circuit_breaker.open_for(provider_name)
        log.warning(
            "upstream_breaker_opened",
            provider=provider_name,
            cooldown_s=UPSTREAM_BREAKER_COOLDOWN_S,
            agent_id=agent_id,
            round=round_num,
            error=str(exc)[:200],
        )
        return True

    async def _feed_turn_total_timeout_gates(
        self,
        *,
        agent_id: str,
        provider_name: str,
        from_model: str = "",
        error_message: str = "请求总超时",
    ) -> None:
        """P0-2：整轮兜底总超时按「上游级失败」喂三处闸门（best-effort，不抛）。

        病灶（qoder 双项目报告）：总超时在 ``core.stream`` 的外层
        ``asyncio.wait_for`` 处被转成裸 error result —— 下游 ``with_retry``
        只看得到被 wait_for 吞掉的 CancelledError，因此不进重试、不上报
        熔断器、不喂 T34 连败计数（实测 13,125 次调用仅 5 次 failover）。
        本方法把总超时归因为**一次独立的上游级失败**，与 ``with_retry`` 内
        RetryableError 的三处上报同缝。防重复计数边界：**轮级**失败仍在
        ``_run_round_chain`` 上报（C10），这里只补「整轮兜底超时」这一次。

        1. 熔断器 ``report_failure``（C10 同口径，带稳定错误码
           ``TURN_TOTAL_TIMEOUT_ERROR_CODE``）；
        2. T34 连败计数 ``observe_failure``（与 ``_failover_gate`` 同一口径）；
        3. T33 骑乘账（复用 ``_persist_retry`` 落 ``llm_retry_exhausted``
           事件，failover_streak 随行 —— 先落账纪律；exhausted=True
           不打 retry 日志/广播。落账先于切换：账面 streak 须含本次失败，
           ``maybe_switch`` 成功后的归零不得洗掉它）；
        4. T34 达阈值 ⇒ ``maybe_switch`` 换模型接管本 run（切换只改本 run
           生效配置，SKIP_MODELS 纪律在 failover config 层，此处不碰模型
           选择本身）。

        每步独立兜底：闸门/账本故障只记日志，绝不放大本已失败的主流程。
        用户主动停止（CancelledError）不进本路径 —— 外层
        ``except TimeoutError`` 天然不接取消；正常收口 / 优雅预算收口
        （``_budget_exhausted_result``）也不抛 TimeoutError，零误报。
        """
        # 1) 熔断器：总超时 = 上游级失败（进时间窗 + 连败计数）。
        try:
            await self._circuit_breaker.report_failure(
                provider_name, error_code=TURN_TOTAL_TIMEOUT_ERROR_CODE
            )
        except Exception as e:  # noqa: BLE001 — 上报旁支不得炸错误收口
            log.warning(
                "turn_timeout_breaker_report_failed",
                agent_id=agent_id,
                provider=provider_name,
                error=str(e)[:200],
            )
        # 2) T34：连败计数 +1（observe_failure 是纯计数，不会抛）。
        streak = self._failover_state().observe_failure()
        # 3) T33 骑乘账：本条骑乘链被整轮兜底墙终止 ⇒ exhausted 落账。
        try:
            persist = self._persist_retry(agent_id, provider_name, None)
            await persist(1, 0, TimeoutError(error_message), True)
        except Exception as e:  # noqa: BLE001 — 账本旁支不得炸错误收口
            log.warning(
                "turn_timeout_ride_ledger_failed",
                agent_id=agent_id,
                provider=provider_name,
                error=str(e)[:200],
            )
        # 4) T34：达阈值 ⇒ 换模型接管本 run（与闸同判据同节奏）。
        if streak >= failover_threshold():
            try:
                await self._failover_state().maybe_switch(
                    agent_id=agent_id,
                    from_model=from_model or provider_name,
                )
            except Exception as e:  # noqa: BLE001 — 切换旁支不得炸错误收口
                log.warning(
                    "turn_timeout_failover_switch_failed",
                    agent_id=agent_id,
                    provider=provider_name,
                    streak=streak,
                    error=str(e)[:200],
                )

    async def _stream_with_empty_retry(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        round_num: int,
        budget_deadline: float | None = None,
    ) -> dict:
        """单轮流式请求，空响应时退避重试（最多 3 次）。

        ``budget_deadline``（time.monotonic 刻度）是本轮可使用的绝对截止：
        到点即中途切断（budget_cut），由 tool_loop 优雅收口 —— 不允许为了
        重试越过 turn 硬预算（否则 agent SAFETY_TIMEOUT 会强杀整轮）。
        """
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
                budget_deadline=budget_deadline,
            )

            if result["status"] == "error":
                return result

            # 预算切断不是空响应，禁止退避重试（重试必然再次越预算）
            if result.get("budget_cut"):
                return result

            # N3 批28-1 施工2：平台 abort（人为掐流）不是空响应 —— 禁止
            # 退避重试（重试 = 把刚掐掉的流再发一遍）。aborted 是终态分类，
            # 交 tool_loop 按 interrupted 语义收口。
            if result.get("aborted"):
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
                # 退避睡眠不得越过预算截止
                if budget_deadline is not None:
                    remain_s = budget_deadline - time.monotonic()
                    if remain_s <= backoff_ms / 1000.0:
                        log.info("empty_response_retry_skipped_budget",
                                 agent_id=agent_id, round=round_num,
                                 remain_s=round(remain_s, 1))
                        # 打 budget_cut 标记走优雅收口（审计 2026-08-08）：
                        # 裸 empty 会让 agent 层同 turn 整轮重试 —— 重试
                        # 拿全新 570s 预算，但 SAFETY 窗口从 chat 开始计时
                        # 不重置，必然被强杀，正是预算闸口要消灭的模式。
                        last_result["budget_cut"] = True
                        return last_result
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

    def _persist_retry(
        self,
        agent_id: str,
        provider_name: str,
        on_delta: DeltaCallback | None = None,
    ):
        """重试持久化 + 实时广播（参考 DSH 把 retry 写入持久事件流并可见）。

        1) 落 agent_events，供监控/审计（重启后仍可查）。
        2) 广播 ``retry`` 事件到前端 —— 前端 useChatSend 一直在监听该事件
           并渲染「重试中 attempt/max」，但此前后端从不发送，导致 56 次真实
           重试（TEST_DSH_29 实测）对用户完全不可见，表现为"卡住"。
        两者都是 best-effort，失败不影响重试主流程。
        """
        async def _cb(attempt: int, delay_ms: int, exc, exhausted: bool) -> None:
            # T3' #3：重试外显 —— 每次重试被调度时打一条结构化日志
            # （attempt/max_retries/delay_ms/reason），供 UI/观测接。
            # with_retry 的三类调度（通用退避 / idle 专属 / 网络异常）都经
            # 本回调，此处一处打点全覆盖；耗尽走 llm_retry_exhausted 事件。
            if not exhausted:
                log.info(
                    "llm_retry_scheduled",
                    agent_id=agent_id,
                    provider=provider_name,
                    attempt=attempt,
                    max_retries=self._retry_handler.max_retries,
                    delay_ms=delay_ms,
                    reason=str(exc)[:200],
                )
            try:
                from hiveweave.services.event_audit import event_audit

                _fo = getattr(self, "_failover", None)
                await event_audit.log(
                    agent_id=agent_id,
                    project_id="",
                    event_type=(
                        "llm_retry_exhausted" if exhausted else "llm_retry"
                    ),
                    payload={
                        "attempt": attempt,
                        "delay_ms": delay_ms,
                        "provider": provider_name,
                        "error": str(exc)[:300],
                        # 可抄③（DSH 先落账后等待）：重试计数随行落账 ——
                        # failover 连败数（T34）与重试事件同窗可审计，
                        # 进程重启后事件流仍是完整账本。
                        "failover_streak": (
                            int(getattr(_fo, "streak", 0) or 0)
                            if _fo is not None
                            else 0
                        ),
                    },
                )
            except Exception:
                pass
            if not exhausted:
                try:
                    await self._fire_delta(on_delta, {
                        "type": "retry",
                        "attempt": attempt,
                        "maxRetries": self._retry_handler.max_retries,
                        "delayMs": delay_ms,
                        "reason": str(exc)[:200],
                    })
                except Exception:
                    pass

        return _cb

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
        budget_deadline: float | None = None,
    ) -> dict:
        """发起单轮流式 HTTP 请求，解析 SSE，返回本轮结果。

        T34（2026-09-29）：本方法是**故障转移重启循环**的外壳 —— 每条
        重试链（:meth:`_run_round_chain`，内含波次级骑乘）因故障转移闸
        中止（``RetryAborted``）时，切换到 ``RunFailover`` 选出的备用模型
        重建链重跑本轮；备用也连败 ⇒ 链上下一个候选；池走完 ⇒ 按原错误
        收口（不无限换）。转移只改本 run 的生效 provider（实例态），
        不写 DB；下回合由 agent 重新解析模型自然恢复主模型。
        ``budget_deadline`` 为 turn 硬预算截止（monotonic 刻度），透传到
        SSE 消费循环做中途预算切断；无预算（auto 模式）时由
        ``ride_wall_cap_s()`` 兜底墙终止骑乘。
        """
        # 本 run 已切换（跨 stream() 调用续用）⇒ 生效 provider = 切换后模型
        provider, provider_name = self._effective_provider(provider, provider_name)
        wall = time.monotonic() + ride_wall_cap_s()
        chain_deadline = (
            min(budget_deadline, wall) if budget_deadline is not None else wall
        )
        while True:
            try:
                return await self._run_round_chain(
                    agent_id=agent_id,
                    provider=provider,
                    provider_name=provider_name,
                    messages=messages,
                    tools=tools,
                    on_delta=on_delta,
                    round_num=round_num,
                    delta_id=delta_id,
                    budget_deadline=budget_deadline,
                    chain_deadline=chain_deadline,
                )
            except RetryAborted as ab:
                new_provider, new_name = self._effective_provider(
                    provider, provider_name
                )
                if new_provider is provider:
                    # 切换未生效（候选工厂构建失败等）→ 防死循环：按触发
                    # 错误原样收口（交既有 error → durable recovery 路）。
                    last = ab.last_error
                    if last is None:
                        raise
                    raise last from None
                log.warning(
                    "failover_chain_restart",
                    agent_id=agent_id,
                    round=round_num,
                    from_provider=str(getattr(provider, "model_name", "") or provider_name),
                    to_provider=str(getattr(new_provider, "model_name", "") or new_name),
                )
                provider, provider_name = new_provider, new_name
                continue

    async def _run_round_chain(
        self,
        agent_id: str,
        provider: ProviderConfig,
        provider_name: str,
        messages: list[dict],
        tools: list[dict] | None,
        on_delta: DeltaCallback | None,
        round_num: int,
        delta_id: str,
        budget_deadline: float | None = None,
        chain_deadline: float | None = None,
    ) -> dict:
        """一条重试链：构建请求 → 波次级骑乘（with_retry）→ 归一化返回。

        供 :meth:`_stream_single_round` 的故障转移重启循环逐次调用；每次
        调用 = 一个模型上的一条完整重试链（含全部退避重试）。链因
        ``RetryAborted``（故障转移闸）终止时异常向上传播，由外壳换模型
        重启。
        """
        url = provider.build_url()
        # opencode Go 网关按 agent 会话稳定键发 x-opencode-session（缺失
        # 即 400 MissingSessionID）；其他网关在 build_headers 内 no-op。
        headers = provider.build_headers(session_id=agent_id)

        # FIX(gateway-tool-id-400): opencode zen go 网关（Console Go）在请求
        # 尾部为 tool/system 消息时，会校验尾部 tool_call id 链的签名；
        # 跨连接/跨节点回声历史 id 会被判为未知 id，整包拒绝并返回
        # HTTP 400 invalid_request_error（agent 多轮工具循环被 doom 的根因，
        # 实测：末尾为 tool 消息 + 非本网关签发 id → 必 400）。
        # 在请求末尾追加一条静态 user 哨兵即可跳过该校验（实测 200）。
        # 哨兵文案同时说明「为何再次唤醒」（回合未收口 / 非人类新指令），
        # 避免模型把旧的 "(continue)" 误读成用户 continue。
        # 只追加到请求副本，不回写 messages，避免污染持久化历史。
        req_messages = messages
        if req_messages and req_messages[-1].get("role") != "user":
            req_messages = [
                *req_messages,
                {"role": "user", "content": continue_sentinel_text()},
            ]
        # 图像能力自判定（让模型自己决定）：请求是否真的带了图 + 是否已剥图重试过。
        had_images = any(
            isinstance(m, dict) and bool(m.get("images")) for m in req_messages
        )
        stripped_retry = False
        body = provider.build_body(
            messages=req_messages,
            stream=True,
            tools=tools,
        )

        body_json = json.dumps(body, ensure_ascii=False)
        log.info("http_request",
                 agent_id=agent_id, round=round_num,
                 url=url, body_size=len(body_json))

        # ── T3'（TEST_DSH_78）：零输出总墙钟帽 ──────────────────────
        # 在进入 with_retry **之前**武装一次绝对 deadline（monotonic 刻度），
        # 覆盖本请求的全部尝试：信号量排队、退避 sleep、每次 attempt 的
        # 等响应头/等首 chunk。只认上游真实字节续命（见 _do_streaming_request
        # 的 seen 置位点），内部控制事件不续命。到点 PermanentError →
        # with_retry 不再重试，走既有 run 级收口。dict 载体跨 attempt 共享
        # （闭包内不可变 deadline + 可变 seen 标志），同 client_holder 惯例。
        first_byte_budget: dict = {
            "deadline": time.monotonic() + FIRST_BYTE_TOTAL_CAP_S,
            "seen": False,
        }

        async def do_request() -> dict:
            # T3'：attempt 边界强制 —— 退避 sleep 与此前 attempt 烧掉的墙钟
            # 不产字节；帽已耗尽就不许再发起下一次请求（否则 90s×重试的
            # 总墙钟又无人负责）。此前 attempt 已见首字节则帽已溶解，放行。
            if (
                not first_byte_budget["seen"]
                and time.monotonic() >= first_byte_budget["deadline"]
            ):
                raise _first_byte_total_error()
            # Bug B fix: 全局并发控制 — 在 HTTP 请求级别限流
            sem = _get_llm_semaphore()
            wait_started = time.monotonic()
            while True:
                queue_wait = LLM_QUEUE_PING_S
                if not first_byte_budget["seen"]:
                    # T3'：排队等待同属零输出墙钟，被帽钳住（到点即判死，
                    # 而非无限排队）
                    queue_wait = min(
                        queue_wait,
                        max(
                            0.05,
                            first_byte_budget["deadline"] - time.monotonic(),
                        ),
                    )
                try:
                    await asyncio.wait_for(
                        sem.acquire(), timeout=queue_wait
                    )
                    break
                except asyncio.TimeoutError:
                    if not first_byte_budget["seen"] and (
                        time.monotonic()
                        >= first_byte_budget["deadline"] - 0.05
                    ):
                        raise _first_byte_total_error() from None
                    # Keep zombie sweep alive; do not use round_start
                    # (that resets the streaming text accumulator).
                    await self._fire_delta(on_delta, {
                        "type": "llm_queue",
                        "round": round_num,
                        "wait_s": int(time.monotonic() - wait_started),
                    })
            try:
                return await self._do_streaming_request(
                    agent_id=agent_id,
                    provider=provider,
                    url=url,
                    headers=headers,
                    body=body,
                    on_delta=on_delta,
                    delta_id=delta_id,
                    round_num=round_num,
                    budget_deadline=budget_deadline,
                    provider_name=provider_name,
                    first_byte_budget=first_byte_budget,
                )
            finally:
                sem.release()

        # ── T34：故障转移闸 —— 每次上游级失败（RetryableError 分类）咨询
        # 一次：连败 ≥ 阈值且池内有候选 ⇒ 中止本链（RetryAborted），由
        # _stream_single_round 换模型重启；无候选 ⇒ False，继续骑乘。
        # 闸挂在 with_retry 调度重试之前（retry.py），骑乘与转移互补：
        # 无界骑乘骑过波次，连败 ≥3 换掉持续故障的上游。
        fo = self._failover_state()

        async def _failover_gate() -> bool:
            streak = fo.observe_failure()
            if streak < failover_threshold():
                return False
            switched = await fo.maybe_switch(
                agent_id=agent_id,
                from_model=str(
                    getattr(provider, "model_name", "") or provider_name
                ),
            )
            return switched is not None

        # ── crash-loop 守卫（施工单 2026-11-03 §4）────────────────────
        # 主 run 的 LLM 请求链（含 with_retry 内全部重试/转移）进入前打
        # 「在飞」标记，finally 清除 —— 只有「进程死在请求中间」才留下
        # 标记，启动 sweep 据此计入 crash_streak（services/crash_loop.py）。
        # 工具执行中 / 请求间隙的正常重启不计数。oneshot/compaction 无
        # _run_id 跳过；best-effort（失败 debug，不阻断请求）。
        _inflight_marked = False
        if crash_loop_guard_enabled() and getattr(self, "_run_id", None):
            try:
                from hiveweave.services.run_ledger import RunLedger

                await RunLedger().mark_inflight(agent_id, self._run_id)
                _inflight_marked = True
            except Exception as mark_err:  # noqa: BLE001 — best-effort
                log.debug(
                    "crash_loop_mark_inflight_failed",
                    agent_id=agent_id, error=str(mark_err),
                )

        try:
            result = await self._retry_handler.with_retry(
                do_request,
                persist=self._persist_retry(agent_id, provider_name, on_delta),
                deadline=chain_deadline,
                failover_gate=_failover_gate,
            )
            if result.get("aborted"):
                # N3 批28-1 施工2：aborted（人为掐流）不是链的 HTTP 成败 ——
                # 既不算 provider 失败（不喂熔断/连败计数），也不冒充成功
                # （掐流时上游可能一个字节都没给，成功无证据）。原样返回。
                return result
            # 链成功（含 empty / budget_cut —— HTTP 交换本身完成）⇒ 瞬态
            # 成功重置连败计数（T33.2：第二次波次按新梯算，而非累计）。
            fo.observe_success()
            # 成功完成 → 报告熔断器成功（C10: 按轮次精确上报）
            await self._circuit_breaker.report_success(provider_name)
            return result
        except RetryableError as e:
            # 可重试错误耗尽 → 报告熔断器失败（C10: 让熔断器感知 HTTP 429/503/504/529 + 网络错误）
            # ⚠ TEST_DSH_64 暗坑③：流空转类 RetryableError **跳过**
            # report_failure —— idle 原是 PermanentError（从不喂熔断），改判
            # Retryable 后若照喂，会经 FAIL_THRESHOLD=5「5 次累计」后门把
            # provider 全项目熔断（慢流关闸面扩大）。空转的恢复钥匙是
            # with_retry 专属重试 + run 级 durable 重醒，不是熔断。
            if not is_stream_idle_error(e):
                await self._circuit_breaker.report_failure(
                    provider_name, error_code=getattr(e, "error_code", None)
                )
            return {
                "status": "error",
                "text": "",
                "thinking": "",
                "tool_calls": [],
                "finish_reason": None,
                "error": str(e),
                # Preserve for agent-level quota park (TEST20 P0-B)
                "error_status": e.status,
                "error_headers": dict(e.headers or {}),
                # 45 轮 P1：断流 raise 前已收的部分 usage（保账）
                "partial_usage": getattr(e, "partial_usage", None),
                # #13 批 A：稳定错误码（typed error 随行，下游按码消费）
                "error_code": getattr(e, "error_code", None),
            }
        except PermanentError as e:
            # B+（TEST_DSH_63）：403 RegionError 等确定性上游死亡**不再走**
            # 「不喂 breaker」旁路 —— REGION/5xx 类失败都要推动熔断（原则：
            # AUTH/参数类 401/400 仍不喂）。首次命中即 open_for，开启期并行
            # 请求在 stream() 入口 check() 秒败（带 UPSTREAM_BREAKER_MARKER），
            # 冷却过后 half_open 探针自动恢复。
            await self._open_breaker_if_upstream_death(
                provider_name, agent_id, round_num, e
            )
            # fixplan #13：本方法作用域内的样本（典型是下面的图像能力短语表失配）
            # **不在** `_do_streaming_request` 的 flush 覆盖内 —— 那是**另一个方法**，
            # 且只在抛异常时才 flush。不在这里补一次的话，该样本要等本 agent
            # **下一次**流异常才被顺带带走；若此后不再报错就永不落库（只剩日志）。
            # 重复 flush 是安全的：缓冲区里该 agent 的样本为空时直接返回 0。
            try:
                await flush_unknown_samples(agent_id)
            except Exception:  # noqa: BLE001 — 样本 flush 是 best-effort 观测旁支（e14bd1a）：
                pass            # 失败只丢这条留痕，绝不影响外层的错误收口主路径
            # 不可重试错误（401/400 等）→ 不报告熔断器
            # （客户端配置问题，非 provider 故障，不应触发熔断）
            # error_status 必须保留: agent 层靠它区分 402 余额耗尽
            # （触发全局停唤醒）与普通客户端错误（TEST19 教训）。
            # 图像能力自判定：请求带了图、400 且错误文本确为「图像不支持」→
            # 这是首轮探测，标记负缓存 + 剥图重试一次（让模型自己「判定」，
            # 不再依赖人工勾选 supports_images）。仅此一次，不再递归。
            #
            # fixplan-16items §三 #13：这张短语表**未命中**时同样要 fail-loud。
            # 「带了图 + 400 但短语表不认识」= 表可能漏了新厂商的措辞
            # （`provider.py:265-289` 自陈「无结构化字段，这仍是高精短语匹配」）。
            # 记样本**不改行为**（剥图判定仍走下面原 `if`）；放在 `if` 之前是为了
            # 只在「失配」时记 —— 命中时不记（那是正常路径，不是未知）。
            if (
                not stripped_retry
                and had_images
                and e.status == 400
                and not _looks_like_image_unsupported_error(str(e))
            ):
                note_unknown_sample(
                    source="image_capability_phrase_table",
                    status=e.status,
                    body=str(e),
                    provider=provider_name,
                    model=provider.model_name,
                    agent_id=agent_id,
                    extra={"had_images": True, "stripped_retry": False},
                )
            if (
                not stripped_retry
                and had_images
                and e.status == 400
                and _looks_like_image_unsupported_error(str(e))
            ):
                log.info(
                    "image_unsupported_mark_degrade",
                    agent_id=agent_id,
                    round=round_num,
                    model=provider.model_name,
                    url=provider.base_url,
                )
                mark_image_unsupported(provider.base_url, provider.model_name)
                stripped_retry = True
                req_messages = _strip_images_for_retry(req_messages)
                body = provider.build_body(
                    messages=req_messages,
                    stream=True,
                    tools=tools,
                )
                try:
                    return await do_request()
                except RetryableError as se:
                    # 剥图重试本身遇到瞬态错误 → 归一化返回（保 error_status/headers），
                    # 不泄漏裸异常（主路径同款契约，TEST19 教训：402 需区分）。
                    # ⚠ TEST_DSH_64 暗坑③（第二处，OCR 复查补）：stream_idle 系
                    # 同样跳过 report_failure —— 与主出口同一条「5 次累计」
                    # 后门熔断红线。
                    if not is_stream_idle_error(se):
                        await self._circuit_breaker.report_failure(
                            provider_name, error_code=getattr(se, "error_code", None)
                        )
                    return {
                        "status": "error",
                        "text": "",
                        "thinking": "",
                        "tool_calls": [],
                        "finish_reason": None,
                        "error": str(se),
                        "error_status": se.status,
                        "error_headers": dict(se.headers or {}),
                        "partial_usage": getattr(se, "partial_usage", None),
                        "error_code": getattr(se, "error_code", None),
                    }
                except PermanentError as se:
                    # 剥图后仍失败（非图像类 400/401 等）→ 归一化返回，不再剥图递归。
                    # 同主路径 B+ 原则：上游死亡类（403/RegionError）也要喂熔断。
                    await self._open_breaker_if_upstream_death(
                        provider_name, agent_id, round_num, se
                    )
                    return {
                        "status": "error",
                        "text": "",
                        "thinking": "",
                        "tool_calls": [],
                        "finish_reason": None,
                        "error": str(se),
                        "error_status": se.status,
                        "partial_usage": getattr(se, "partial_usage", None),
                        # TEST_DSH_64：stream_idle_exhausted 码随行（暗坑②：
                        # agent 层重建异常才有码可保，重醒门窄谓词靠它）
                        "error_code": getattr(se, "error_code", None),
                    }
            return {
                "status": "error",
                "text": "",
                "thinking": "",
                "tool_calls": [],
                "finish_reason": None,
                "error": str(e),
                "error_status": e.status,
                "partial_usage": getattr(e, "partial_usage", None),
                # TEST_DSH_64：PermanentError 出口此前不透 error_code ——
                # stream_idle_exhausted 耗尽码会在这里凭空消失。
                "error_code": getattr(e, "error_code", None),
            }
        finally:
            if _inflight_marked:
                try:
                    from hiveweave.services.run_ledger import RunLedger

                    await RunLedger().clear_inflight(agent_id, self._run_id)
                except Exception as clear_err:  # noqa: BLE001 — best-effort
                    log.debug(
                        "crash_loop_clear_inflight_failed",
                        agent_id=agent_id, error=str(clear_err),
                    )

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
        budget_deadline: float | None = None,
        provider_name: str | None = None,
        first_byte_budget: dict | None = None,
    ) -> dict:
        """执行 HTTP 流式请求（同步 httpx 跑在线程池里，事件边收边推）。

        Windows 上 asyncio CancelledError 无法中断 httpx 的 socket read，
        因此改用同步 httpx.Client + run_in_executor。同步版的超时走
        socket.settimeout()（OS 级）。

        真流式：线程内解析 SSE 后通过 queue 推到事件循环，立刻 _fire_delta，
        避免整包收完才刷新（否则 UI 长时间冻住，误判为 streaming 僵尸）。

        ``first_byte_budget``（T3'，``_stream_single_round`` 武装的共享 dict：
        ``{"deadline": monotonic 刻度, "seen": bool}``）为零输出总墙钟帽 ——
        未收到任何真实 SSE 字节期间，SSE wait 被其钳住，到点抛
        PermanentError(first_byte_total_timeout)；首个真实字节到达即置
        ``seen=True`` 溶解该帽（跨重试永久生效），此后由既有 IDLE 层接管。
        None（直调/测试）= 不启用本帽。
        """
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        read_to = httpx.Timeout(
            read=STREAM_SOCKET_READ_TIMEOUT_S,
            connect=STREAM_SOCKET_CONNECT_TIMEOUT_S,
            write=STREAM_SOCKET_WRITE_TIMEOUT_S,
            pool=STREAM_SOCKET_POOL_TIMEOUT_S,
        )

        loop = asyncio.get_running_loop()
        event_q: asyncio.Queue = asyncio.Queue()
        _DONE = object()
        _ERR = object()
        # 客户端引用透出到事件循环侧：budget_cut / 首包 / 空闲超时都要主动
        # 断流（关闭客户端让线程内 iter_bytes 立即出错退出）。只 raise 不关
        # 的话 finally 会 await 线程，等到 socket 读超时（idle+30）才返回，
        # 看门狗形同虚设，且占着 LLM semaphore。
        client_holder: dict[str, httpx.Client] = {}

        def _close_http_client() -> None:
            """主动断流：关客户端让线程内 iter_bytes 立即出错退出。

            best-effort —— 线程 finally 里还会再关一次（幂等），失败也无妨
            （线程终会自然结束，最长 httpx read = stream idle + 30s）。
            """
            orphan_client = client_holder.get("client")
            if orphan_client is not None:
                try:
                    orphan_client.close()
                except Exception:
                    pass

        # ── N3 批28-1 施工2：per-request abort 源 ──────────────────────
        # pi 形态适配：signal 无法传进同步 httpx 线程 ⇒ 注册表 + 周期检查
        # （每 chunk 直读 + ABORT_POLL_S 心动 poller，静默流也有掐流时钟）。
        # 置位 ⇒ 关 client 断流 + 归类 aborted（非 error；run 走 interrupted
        # 语义）。run_id 来自 stream() 入口（agent 侧传 _current_run_id），
        # 供 abort_stream 做陈旧判废；旁路调用缺省 None = 按 agent 掐。
        _ABORT = object()
        abort_src = register_abort_source(
            agent_id,
            run_id=getattr(self, "_run_id", None),
            round_num=round_num,
        )
        abort_hit = False
        abort_reason: str | None = None

        # ── N3 批28-2 施工1：流内退化检测器 ────────────────────────────
        # 病灶（第六轮）：failover 免费档输出坍塌（Robert 的「Hmm. OK. …」
        # 循环 / DSH 星野的 </longcat_tool_call> 正文连吐）—— idle/首块看门狗
        # 只看「有没有 chunk」，失控流稳定吐 chunk 直到烧满 128k。检测器
        # 对正文/thinking 增量累积判退化（暖机 4k、判据见 degenerate_detect
        # 模块注释），命中 ⇒ 经批28-1 abort 管道掐流（aborted ⇒ run 按
        # interrupted 语义收口）。优先于 length 帽：失控流在烧满**之前**被掐。
        # per-request 一次：命中即闩锁 + 直接投递 _ABORT 哨兵（本 chunk 即
        # 收，不等下一 chunk / 2s poller —— 失控流每多跑一个 chunk 都在烧钱）。
        degenerate_detector = DegenerateDetector()

        # ── N3 批28-2 P2-1 修复：流活动续约 ────────────────────────────
        # renew_lease 原只在轮/步边界续约 —— 慢 provider 上一轮 >15min 的
        # 健康流（批28-1 length 恢复重问也会拉长）会在下一 tick 被误收割。
        # 语义改为「chunk 在流 = 活」：收到新 chunk 且距上次续约 ≥60s ⇒
        # _abort_poll 续约一次；流挂死（无 chunk）不续 ⇒ 15min floor 后由
        # 收割接管（退化流在烧满前已被检测器掐掉，不占收割名额）。
        _stream_activity = {"last_chunk": 0.0, "last_renew": 0.0}
        _RENEW_EVERY_S = 60.0

        def _check_degenerate(delta: str) -> None:
            nonlocal abort_hit
            if abort_hit:
                return
            kind = degenerate_detector.feed(delta)
            if kind is None:
                return
            reason = f"degenerate_output:{kind}"
            log.warning(
                "degenerate_output_detected",
                agent_id=agent_id,
                round=round_num,
                kind=kind,
                text_len=len(text_acc),
                thinking_len=len(thinking_acc),
                note="degenerate output loop/tag-leak detected mid-stream; "
                     "aborting via abort pipeline",
            )
            # 经注册表入口掐流（run_id 陈旧判废 + 统一日志）；再直接投递
            # _ABORT 哨兵保证确定性收口（汇合到上方既有 _ABORT 处理分支，
            # 单一收口路径，不另写第二套）。
            abort_stream(
                agent_id,
                run_id=getattr(self, "_run_id", None),
                reason=reason,
            )
            event_q.put_nowait((_ABORT, reason))

        async def _abort_poll() -> None:
            """2s 心动检查 abort 源：置位 ⇒ 投递 _ABORT 控制消息。

            静默流（思考中/无 chunk，最长 IDLE 150s 无事件）唯一的掐流
            时钟 —— 不改 SSE wait 的 idle 看门狗结构（wait_for 超时即判死
            的语义不动），由本任务在旁路周期唤醒。
            """
            while True:
                await asyncio.sleep(ABORT_POLL_S)
                if abort_src.event.is_set():
                    event_q.put_nowait((_ABORT, abort_src.reason or "aborted"))
                    return
                # 批28-2 P2-1：流活动续约（节流 60s）—— chunk 在流 = 活，
                # 续 lease 防「慢而健康」的长轮被 lease 收割误杀；挂死流
                # （无 chunk）不续 ⇒ floor 后由收割接管。
                _run_id_now = getattr(self, "_run_id", None)
                now_m = time.monotonic()
                if (
                    _run_id_now
                    and _stream_activity["last_chunk"] > _stream_activity["last_renew"]
                    and now_m - _stream_activity["last_renew"] >= _RENEW_EVERY_S
                ):
                    _stream_activity["last_renew"] = now_m
                    try:
                        from hiveweave.services.run_ledger import RunLedger

                        await RunLedger().renew_lease(agent_id, _run_id_now)
                    except Exception as renew_err:  # noqa: BLE001 — best-effort
                        log.debug(
                            "stream_lease_renew_failed",
                            agent_id=agent_id, error=str(renew_err),
                        )

        def _run_sync() -> None:
            """在线程中执行：HTTP 请求 + SSE 解析，事件即时入队。"""
            http_client = httpx.Client(timeout=read_to)
            client_holder["client"] = http_client
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

        executor_task = loop.run_in_executor(None, _run_sync)
        abort_task = asyncio.ensure_future(_abort_poll())

        text_acc = ""
        thinking_acc = ""
        tool_call_deltas: list[dict] = []
        finish_reason: str | None = None
        usage: dict | None = None
        budget_cut = False
        abandon_executor = False
        got_event = False
        # N3 批28-1 施工2：aborted 状态（与 budget_cut 平行的提前收口位）
        # T3' #3：终止事件证据位 —— finish / message_stop / [DONE] 任一见过
        # 即置位；传输正常关闭但从未置位 = 截断流，不许当成功返回。
        saw_terminator = False
        try:
            while True:
                # Idle watchdog: wait only for the next SSE event. First
                # token uses FIRST_CHUNK; afterwards IDLE (default 75s).
                # Turn wall clock (always on) can still cut earlier.
                wait_s = stream_chunk_wait_s(got_event=got_event)
                if (
                    first_byte_budget is not None
                    and not first_byte_budget["seen"]
                ):
                    # T3'：零输出期间被总帽钳住（budget_deadline 同款 min()；
                    # 内部控制事件不续命，帽是绝对线）。首字节到达后本 min()
                    # 永不再生效 —— 帽已溶解，IDLE 层接管。
                    wait_s = min(
                        wait_s,
                        max(
                            0.05,
                            first_byte_budget["deadline"] - time.monotonic(),
                        ),
                    )
                if budget_deadline is not None:
                    wait_s = min(
                        wait_s,
                        max(0.1, budget_deadline - time.monotonic()),
                    )
                try:
                    kind, payload = await asyncio.wait_for(
                        event_q.get(), timeout=wait_s
                    )
                except asyncio.TimeoutError:
                    if budget_deadline is not None and (
                        time.monotonic() >= budget_deadline - 0.5
                    ):
                        # 预算切断：保留已累积文本；不完整 tool_calls 丢弃
                        # （截断的 JSON args 不可执行，同 length 截断语义）。
                        budget_cut = True
                        abandon_executor = True
                        log.warning(
                            "stream_budget_cut",
                            agent_id=agent_id,
                            round=round_num,
                            text_len=len(text_acc),
                            discarded_tool_deltas=len(tool_call_deltas),
                        )
                        _close_http_client()
                        break
                    if (
                        first_byte_budget is not None
                        and not first_byte_budget["seen"]
                        and time.monotonic()
                        >= first_byte_budget["deadline"] - 0.05
                    ):
                        # T3'：零输出总帽到点 —— 明确「上游零输出总超时」，
                        # PermanentError 终态（with_retry 不再重试），走
                        # 既有 run 级收口。放既有 first-chunk 判定之前：
                        # 帽是跨尝试的总线，first-chunk 只是单次尝试线。
                        abandon_executor = True
                        _close_http_client()
                        log.warning(
                            "llm_first_byte_total_timeout",
                            agent_id=agent_id,
                            round=round_num,
                            cap_s=FIRST_BYTE_TOTAL_CAP_S,
                            text_len=len(text_acc),
                            note=(
                                "upstream zero output; total wall-clock cap "
                                "across retries exceeded"
                            ),
                        )
                        raise _first_byte_total_error() from None
                    abandon_executor = True
                    _close_http_client()
                    if not got_event:
                        raise RetryableError(
                            f"First chunk timeout ({FIRST_CHUNK_TIMEOUT_S}s)"
                        )
                    # TEST_DSH_64 #3：首 chunk 后空转原抛 PermanentError ⇒
                    # 不进 with_retry 重试、重醒门也不认（is_upstream_death
                    # 判 False），死 run 全靠 30s 冷却运气。改判 RetryableError
                    # （error_code=stream_idle，跨组契约）⇒ with_retry 专属
                    # 重试 1 次，耗尽升级 PermanentError(stream_idle_exhausted)
                    # 交 run 级 durable 重醒。150s 数值不动（64 实测全为真死，
                    # 非误杀）；⚠ 不许把 idle 纳入 is_upstream_death（与 60s
                    # 快熔共判据，扩它 = 慢流对全项目关闸）。
                    raise RetryableError(
                        f"Stream idle timeout ({IDLE_TIMEOUT_S}s)",
                        error_code=STREAM_IDLE_ERROR_CODE,
                    )

                if kind is _DONE:
                    break
                if kind is _ERR:
                    raw = payload
                    if raw.get("timeout"):
                        raise classify_stream_socket_timeout(
                            got_event=got_event
                        )
                    if raw.get("connect_error"):
                        raise RetryableError(
                            f"Connection error: {raw['connect_error']}"
                        )
                    if raw.get("http_status"):
                        raise classify_http_error(
                            raw["http_status"],
                            raw.get("body", ""),
                            headers=raw.get("headers", {}),
                            provider=provider_name,
                            model=provider.model_name,
                            agent_id=agent_id,
                        )
                    # 无 http_status 的异常文本形态：仅 region/不可用类
                    # 确定性永久失败 fast-fail（403 地域类误入退避曾慢死
                    # 476s，TEST_DSH_47 #8）；未知措辞保持可重试，避免
                    # opaque 网关瞬态错误被误判成 Permanent 秒死。
                    #
                    # ⚠ 这里是**最高价值的未知桶**（连 status 都没有，只能看文案）
                    # ⇒ 必须 fail-loud 留样本（fixplan #13 修法 2）。本分支的 note
                    # 由本方法的 `except BaseException` 里的 flush 覆盖（同一个方法）。
                    err_text = raw.get("error", "Unknown HTTP error")
                    if is_region_unavailable_error(err_text):
                        raise PermanentError(err_text)
                    note_unknown_sample(
                        source="transport_raw_default",
                        status=None,
                        body=err_text,
                        provider=provider_name,
                        model=provider.model_name,
                        agent_id=agent_id,
                        extra={"transport_raw": True},
                    )
                    raise RetryableError(err_text)

                if kind is _ABORT:
                    # N3 批28-1 施工2：人为掐流 ⇒ 断流 + aborted 分类（pi：
                    # stopReason="aborted" 而非 error —— 下游可区分「人为掐」
                    # 与「真失败」）。已收文本保留；不完整 tool_calls 丢弃
                    # （同 budget_cut 语义：半截 JSON 不可执行）。
                    abort_hit = True
                    abort_reason = payload
                    abandon_executor = True
                    _close_http_client()
                    log.warning(
                        "llm_stream_aborted",
                        agent_id=agent_id,
                        round=round_num,
                        reason=abort_reason,
                        text_len=len(text_acc),
                        discarded_tool_deltas=len(tool_call_deltas),
                    )
                    break

                got_event = True
                if first_byte_budget is not None:
                    # T3'：首个真实上游字节到达 —— 零输出总帽对本重试链
                    # **永久溶解**（跨后续 attempt 生效；IDLE 层接管）。
                    # 判据与既有 got_event 同源：任何解析出的 SSE 事件都算
                    # 真实字节；queue ping / _DONE / _ERR 控制消息不置位。
                    first_byte_budget["seen"] = True

                # N3 批28-1 施工2：活跃流快速检查 —— chunk 到达即查 abort 源
                # （比 2s poller 更快掐）。置位 ⇒ 投递 _ABORT 哨兵，汇合到
                # 上方同一处理分支（单一收口路径，不另写第二套）。
                if abort_src.event.is_set():
                    event_q.put_nowait((_ABORT, abort_src.reason or "aborted"))

                # 逐事件预算检查（审计 2026-08-08 P1）：上面的切断只在 wait
                # 超时分支判定 —— 连续事件流（间隔 <0.1s）会让 wait 永不超时，
                # 整个闸口被绕过。「过预算」必须是逐事件判定的结构属性，不能
                # 依赖流出现空隙的统计属性。放在 _DONE/_ERR 之后：控制消息
                # （正常结束/错误分类）优先走完原路径。
                if budget_deadline is not None and (
                    time.monotonic() >= budget_deadline
                ):
                    budget_cut = True
                    abandon_executor = True
                    log.warning(
                        "stream_budget_cut",
                        agent_id=agent_id,
                        round=round_num,
                        text_len=len(text_acc),
                        discarded_tool_deltas=len(tool_call_deltas),
                    )
                    _close_http_client()
                    break

                event = payload
                if not isinstance(event, dict):
                    continue
                if event.get("__done__"):
                    # [DONE] 哨兵（parse_sse 产出）：终止证据
                    saw_terminator = True
                try:
                    extracted = provider.extract_usage(event)
                except (TypeError, ValueError, OverflowError, AttributeError):
                    extracted = None
                if extracted:
                    # Merge: a later sparse usageMetadata must not wipe cache_read.
                    usage = {**(usage or {}), **extracted}
                for c in provider.parse_stream_chunk(event):
                    ctype = c.get("type")
                    if ctype == "text":
                        content = c["content"]
                        await self._fire_delta(on_delta, {
                            "type": "text_delta", "content": content,
                            "delta_id": delta_id})
                        text_acc += content
                        _stream_activity["last_chunk"] = time.monotonic()
                        _check_degenerate(content)
                    elif ctype == "reasoning":
                        content = c["content"]
                        await self._fire_delta(on_delta, {
                            "type": "thinking_delta", "content": content,
                            "delta_id": delta_id})
                        thinking_acc += content
                        _stream_activity["last_chunk"] = time.monotonic()
                        _check_degenerate(content)
                    elif ctype == "tool_call_delta":
                        tool_call_deltas.append(c["tool_call"])
                    elif ctype == "tool_call_start":
                        tc = c.get("tool_call", {})
                        if tc:
                            tool_call_deltas.append(tc)
                    elif ctype == "tool_call_end":
                        pass
                    elif ctype == "message_stop":
                        # 终止证据（Anthropic 流收尾）
                        saw_terminator = True
                    elif ctype in ("thinking_start", "thinking_signature"):
                        pass
                    elif ctype == "usage":
                        u = c.get("usage", {})
                        if u:
                            usage = usage or {}
                            usage.update(u)
                    elif ctype == "finish":
                        # 终止证据（OpenAI 系 finish_reason chunk）
                        saw_terminator = True
                        finish_reason = (
                            c.get("reason")
                            or c.get("finish_reason")
                            or finish_reason
                        )
                    elif ctype == "error":
                        # 流中错误 chunk（HTTP 200 但 body 内包 error）不能吞掉：
                        # 旧实现只打日志继续跑，最终返回 status:"ok" 空文本，
                        # 被拖进空响应重试循环甚至误判为成功空回合
                        # （对齐 opencode preserve compatible stream errors）。
                        # 交给 classify_http_error 按内容判定重试/永久，
                        # 与 HTTP 非 200 分支走同一套多厂商瞬态错误识别。
                        error_content = str(c.get("content", ""))
                        log.warning(
                            "sse_error_chunk",
                            agent_id=agent_id,
                            error=error_content,
                        )
                        raise classify_http_error(
                            None,
                            error_content,
                            provider=provider_name,
                            model=provider.model_name,
                            agent_id=agent_id,
                        )

            # N3 批28-1 施工2：流自然结束（_DONE 先于哨兵被读）但 abort 已
            # 置位 ⇒ 按 aborted 收口（对齐 pi 的 post-stream signal.aborted
            # 检查：掐晚了也是掐）。
            if not budget_cut and not abort_hit and abort_src.event.is_set():
                abort_hit = True
                abort_reason = abort_src.reason or "aborted"
                log.warning(
                    "llm_stream_aborted_post_done",
                    agent_id=agent_id,
                    round=round_num,
                    reason=abort_reason,
                    text_len=len(text_acc),
                )

            # T3' #3：空流终态校验 —— 传输正常关闭（字节收完）但从未见过
            # 终止事件（message_stop / finish / [DONE]）= 截断流。按 pi
            # 「ended without message_stop」先例抛 RetryableError（error_code
            # 区分），交 with_retry 重发，不许当成功返回（否则半截
            # tool_call arguments 会当 ok 进历史回传 → 网关 400 杀死整个
            # turn 的已知病灶）。budget_cut 有自己的收口语义（保留已收
            # 文本），不经本校验。放在 try 块内：partial_usage 随行与
            # unknown-sample flush 由本方法的 except 覆盖。
            if (
                not budget_cut
                and not abort_hit
                and not saw_terminator
            ):
                raise RetryableError(
                    "Stream ended without terminator event "
                    "(message_stop/finish/[DONE])",
                    error_code=STREAM_NO_TERMINATOR_ERROR_CODE,
                )
        except BaseException as e:
            if not abandon_executor:
                abandon_executor = True
                _close_http_client()
            # fixplan-16items §三 #13 修法 2：把本轮「分类层认不出来」的样本
            # 落 agent_events（本方法拿得到 agent_id，分类函数拿不到）。
            # ⚠ 必须在 raise 之前 await；且 flush 自身绝不抛（否则会**覆盖掉
            # 正在抛出的原始错误**，让排查失去线索）。best-effort，失败不影响。
            try:
                await flush_unknown_samples(agent_id)
            except Exception:  # noqa: BLE001 — 样本 flush 是 best-effort 观测旁支（e14bd1a）：
                pass            # 失败只丢这条留痕，绝不影响外层的错误收口主路径
            # 45 轮 P1：断流/超时 raise 路径保住已收 usage（budget_cut
            # 返回路径此前已保，raise 路径随异常丢弃）——挂异常对象，由
            # _stream_single_round 的错误收口带出、tool_loop 并入账本。
            if usage:
                try:
                    # setattr 形式保 mypy 基线（BaseException 无该属性声明）
                    setattr(e, "partial_usage", dict(usage))
                except Exception:
                    pass
            raise
        finally:
            # N3 批28-1 施工2：请求结束（任何形态）—— 停 poller + 注销 abort
            # 源（正常完成注销 ⇒ 注册表不残留；异常/取消路径同样经过这里）。
            abort_task.cancel()
            unregister_abort_source(agent_id, abort_src)
            if abandon_executor:
                # 预算切断 / 首包 / 空闲超时都【不能】await executor 线程 ——
                # 它正阻塞在 socket read 上；await 会把看门狗省下的时间耗光
                # （httpx read = idle + 30s），并占着 LLM semaphore。
                # 已 close 客户端。cancel() 拦住尚未开工的默认线程池任务，
                # 避免幽灵 HTTP 稍后占满 streamer 共用的 default executor。
                if not executor_task.done():
                    executor_task.cancel()
            else:
                try:
                    await executor_task
                except Exception:
                    pass

        if abort_hit:
            # N3 批28-1 施工2：aborted 结果形态 —— status ok（非 error），
            # finish_reason="aborted"，事实位 aborted=True + abort_reason。
            # tool_calls 已丢弃（半截不可执行）；usage 随行保账。
            return {
                "status": "ok",
                "text": text_acc,
                "thinking": thinking_acc,
                "tool_calls": [],
                "finish_reason": ABORTED_STOP_REASON,
                "usage": usage,
                "aborted": True,
                "abort_reason": abort_reason,
            }

        if budget_cut:
            return {
                "status": "ok",
                "text": text_acc,
                "thinking": thinking_acc,
                "tool_calls": [],
                "finish_reason": "budget_cut",
                "usage": usage,
                "budget_cut": True,
            }

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
        1. httpx 原生 read timeout（stream idle + 30s）— socket 级
        2. time.monotonic() 跟踪 — 应用级 first-chunk / idle

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

