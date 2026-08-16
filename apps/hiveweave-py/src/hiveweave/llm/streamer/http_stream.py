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

from hiveweave.llm.provider import ProviderConfig
from hiveweave.llm.retry import (
    PermanentError,
    RetryableError,
    classify_http_error,
)

from .constants import (
    CONTINUE_SENTINEL,
    EMPTY_RESPONSE_BACKOFF_MS,
    EMPTY_RESPONSE_MAX_RETRIES,
    FIRST_CHUNK_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    LLM_QUEUE_PING_S,
    STREAM_SOCKET_READ_TIMEOUT_S,
    stream_chunk_wait_s,
    _get_llm_semaphore,
)
from .sse import merge_tool_calls, parse_sse
from .types import DeltaCallback

log = structlog.get_logger(__name__)


def classify_stream_socket_timeout(
    *, got_event: bool
) -> RetryableError | PermanentError:
    """httpx socket ReadTimeout: retry only before the first token."""
    msg = f"HTTP read timeout ({STREAM_SOCKET_READ_TIMEOUT_S}s)"
    if got_event:
        return PermanentError(f"{msg} after tokens")
    return RetryableError(msg)


class HttpStreamMixin:
    """HTTP / SSE streaming methods for Streamer."""

    if TYPE_CHECKING:
        _fire_delta: Any
        _retry_handler: Any
        _circuit_breaker: Any

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

        带 HTTP 重试（429/503/504/529 + 网络错误），首 chunk 超时检测。
        ``budget_deadline`` 为 turn 硬预算截止（monotonic 刻度），透传到
        SSE 消费循环做中途预算切断。
        """
        url = provider.build_url()
        headers = provider.build_headers()

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
            wait_started = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(
                        sem.acquire(), timeout=LLM_QUEUE_PING_S
                    )
                    break
                except asyncio.TimeoutError:
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
                )
            finally:
                sem.release()

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
                # Preserve for agent-level quota park (TEST20 P0-B)
                "error_status": e.status,
                "error_headers": dict(e.headers or {}),
            }
        except PermanentError as e:
            # 不可重试错误（401/400 等）→ 不报告熔断器
            # （客户端配置问题，非 provider 故障，不应触发熔断）
            # error_status 必须保留: agent 层靠它区分 402 余额耗尽
            # （触发全局停唤醒）与普通客户端错误（TEST19 教训）。
            return {
                "status": "error",
                "text": "",
                "thinking": "",
                "tool_calls": [],
                "finish_reason": None,
                "error": str(e),
                "error_status": e.status,
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
        budget_deadline: float | None = None,
    ) -> dict:
        """执行 HTTP 流式请求（同步 httpx 跑在线程池里，事件边收边推）。

        Windows 上 asyncio CancelledError 无法中断 httpx 的 socket read，
        因此改用同步 httpx.Client + run_in_executor。同步版的超时走
        socket.settimeout()（OS 级）。

        真流式：线程内解析 SSE 后通过 queue 推到事件循环，立刻 _fire_delta，
        避免整包收完才刷新（否则 UI 长时间冻住，误判为 streaming 僵尸）。
        """
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        read_to = httpx.Timeout(
            read=STREAM_SOCKET_READ_TIMEOUT_S, connect=10, write=10, pool=10
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

        text_acc = ""
        thinking_acc = ""
        tool_call_deltas: list[dict] = []
        finish_reason: str | None = None
        usage: dict | None = None
        budget_cut = False
        abandon_executor = False
        got_event = False
        try:
            while True:
                # Idle watchdog: wait only for the next SSE event. First
                # token uses FIRST_CHUNK; afterwards IDLE (default 5 min).
                # Opt-in session wall clock can still cut earlier.
                wait_s = stream_chunk_wait_s(got_event=got_event)
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
                    abandon_executor = True
                    _close_http_client()
                    if not got_event:
                        raise RetryableError(
                            f"First chunk timeout ({FIRST_CHUNK_TIMEOUT_S}s)"
                        )
                    raise PermanentError(
                        f"Stream idle timeout ({IDLE_TIMEOUT_S}s)"
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
                        )
                    raise RetryableError(
                        raw.get("error", "Unknown HTTP error")
                    )

                got_event = True

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
                        raise classify_http_error(None, error_content)
        except BaseException:
            if not abandon_executor:
                abandon_executor = True
                _close_http_client()
            raise
        finally:
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

