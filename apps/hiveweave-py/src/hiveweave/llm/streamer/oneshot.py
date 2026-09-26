"""Oneshot streaming LLM completion（审计 / 评审 / run_tests 一次性补全）。

背景（审计 P0·L6，四项目最大确定性失血）：request_code_audit 69-86% 调用
失败且整齐卡死在 111-112s —— 旧链路走**非流式单发 HTTP**（服务端要等整段
生成完才发字节），``httpx.Timeout(read=…)`` 等效「总时长墙」：慢 = 死，
哪怕一直在动。主链路（agent 对话）是流式 SSE，超时单位是**事件间隔**，
慢而活着永不被杀。

本模块把 oneshot 链路搬到与主链路同一套流式机件上（复用
``http_stream._do_streaming_request`` 的线程 + queue + 增量 UTF-8 解码 +
``parse_sse`` 传输形状、``constants`` 的 idle 看门狗语义、``retry`` 的
错误分类、``provider`` 的 build_body/headers/parse_stream_chunk/
extract_usage），并重排三道帽：

1. **idle 帽**（事件间隔判活）：首事件前 FIRST_CHUNK_TIMEOUT_S，之后
   ``oneshot_idle_timeout_s()``（env ``HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S``，
   默认对齐主链路 ``IDLE_TIMEOUT_S``=150）。到点 = 判死，**不重试**。
2. **总时长帽**（独立于 idle 的墙钟）：``oneshot_total_timeout_s()``
   （env ``HIVEWEAVE_CODE_AUDIT_TIMEOUT_S``，默认 540 —— 必须 < turn 硬
   预算 ``HARD_TOTAL_TIMEOUT_S``=570，否则审计若跑满帽，agent turn 会先
   被收口、审计完成也送不到 agent 手里；540 留 30s 收尾余量。真实审计
   常态 30-90s，旧 120s 帽余量不足；长 diff 需要足量。env 可显式调高，
   调过 570 的语义见 ``oneshot_total_timeout_s``）。逐事件判定（结构
   属性，不依赖流出现空隙）。到点 = 判死，**不重试**。
   外层（code_audit 的 wait_for）与内层共享同一 env 来源 —— 单一真相。
3. **重试窗**：语义重定义为「只对连接失败 / 立即拒绝生效」——
   connect 类异常（未收到任何事件）+ HTTP 429/5xx 立即拒绝可重试
   ≤1 次；idle / 总时长 / 首 chunk 判死与收到事件后的传输死亡一律不重试
   （慢而活着不再触发「读超时→整段重发」的 110s 恒死循环）。

失败归因：**等待超时（窗口内无事件到达）恒归 idle 族**——真沉默就是
"慢而死了"，即使沉默窗口被剩余预算提前截断（剩余 < idle 帽时到点，
归 idle 而非 total：归 total 会误导成「多给预算就能完成」；P2-2 独立
审计 2026-09-27）。**total 只留给「事件仍在流动时预算耗尽」（逐事件
判定）或信号量等待超时**。判死异常携带 ``timeout_layer``
（"first_chunk" / "idle" / "total"），错误消息分别注明；结构化字段为
批 D 的 timeout_layer 事实位预留（消费方按
``getattr(exc, "timeout_layer", None)`` 读取）。
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os as _os
import random
import time
from typing import Any

import httpx
import structlog

from hiveweave.llm.retry import (
    MAX_DELAY_MS,
    PermanentError,
    RetryableError,
    classify_http_error,
    is_region_unavailable_error,
    parse_retry_after_ms,
    should_retry_exception,
)

from .constants import (
    FIRST_CHUNK_TIMEOUT_S,
    HARD_TOTAL_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    _get_llm_semaphore,
)
from .sse import parse_sse

log = structlog.get_logger(__name__)

# RetryableError 的产生面（classify_http_error）本就只有 429/5xx 状态码、
# 瞬态文案命中与未知信号三类 —— 流未开口时全部符合「连接失败/立即拒绝」，
# 见 _retry_allowed；4xx 客户端错误走 PermanentError，天然不可重试。

# 默认总时长帽：必须 < turn 硬预算 HARD_TOTAL_TIMEOUT_S（570，见
# constants.py）——审计若可跑满帽，agent turn 会先被预算收口，审计完成
# 也送不到 agent 手里（P2-3，独立审计 2026-09-27）。540 留 30s 收尾余量；
# env 仍可显式调高（语义见 oneshot_total_timeout_s）。
ONESHOT_TOTAL_TIMEOUT_DEFAULT_S = 540.0
assert ONESHOT_TOTAL_TIMEOUT_DEFAULT_S < HARD_TOTAL_TIMEOUT_S, (
    "oneshot 默认总帽必须 < turn 硬预算 HARD_TOTAL_TIMEOUT_S，"
    "否则审计完成也送不到 agent 手里（turn 先收口）"
)

_RETRY_BACKOFF_RANGE_S = (0.5, 1.0)

# 总时长帽的重试地板：剩余预算低于此值不再发起新尝试（进了也是被切断）。
_RETRY_MIN_REMAINING_S = 10.0


def _env_float(name: str, default: float) -> float:
    """env 读取浮点秒数——非法值回退默认，绝不在导入期/调用期抛异常。

    与 services/code_audit._timeout_from_env 同纪律：`HIVEWEAVE_X=60s`
    / 空串 / 负数都不能炸链路。
    """
    raw = _os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("oneshot.bad_timeout_env", key=name, value=raw, default=default)
        return default
    return value if value > 0 else default


def oneshot_total_timeout_s() -> float:
    """oneshot 单次调用总时长帽（秒）——单一真相。

    env ``HIVEWEAVE_CODE_AUDIT_TIMEOUT_S``，默认 540（< turn 硬预算
    ``HARD_TOTAL_TIMEOUT_S``=570，留 30s 收尾余量）。**运行时读取**：
    code_audit 外层 wait_for（``effective_audit_timeout_s``）与内层流式
    看门狗共用本函数，改 env 两端同步生效（旧实现两端各持一份快照，
    内层 110s 恒小于外层 120s ⇒ 外层 env 永不触发的假闸）。

    env 可显式调高；**调过 570 意味着审计可能活得比 turn 久** —— agent
    turn 会先被硬预算收口，本次审计结果送不到 agent 手里，只能经
    audit_retry 队列兜底送达（不丢，但迟到）。默认 540 无此问题。
    """
    return _env_float(
        "HIVEWEAVE_CODE_AUDIT_TIMEOUT_S", ONESHOT_TOTAL_TIMEOUT_DEFAULT_S
    )


def oneshot_idle_timeout_s() -> float:
    """oneshot 流 idle 看门狗（秒）——事件间隔判活帽。

    env ``HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S``，默认对齐主链路
    ``IDLE_TIMEOUT_S``（150，含其 env 覆盖）。审计场景需要独立收紧/
    放宽时用本 env，不必动主链路。
    """
    return _env_float("HIVEWEAVE_ONESHOT_IDLE_TIMEOUT_S", IDLE_TIMEOUT_S)


class OneshotTimeoutError(Exception):
    """oneshot 判死基类：携带 timeout_layer 结构化字段（批 D 事实位接缝）。

    ``timeout_layer``: "first_chunk"（首事件前判死）/ "idle"（事件间隔
    判死）/ "total"（总时长墙钟判死）。消费方用
    ``getattr(exc, "timeout_layer", None)`` 读取，普通异常该属性不存在。
    """

    def __init__(self, message: str, timeout_layer: str) -> None:
        super().__init__(message)
        self.timeout_layer = timeout_layer


class OneshotIdleTimeout(OneshotTimeoutError):
    """首 chunk / 事件间隔超时——流沉默判死（慢而沉默 ≠ 慢而活着）。"""

    def __init__(self, message: str, timeout_layer: str = "idle") -> None:
        super().__init__(message, timeout_layer)


class OneshotTotalTimeout(OneshotTimeoutError):
    """总时长墙钟判死 —— 仅限「事件仍在流动时预算耗尽」或信号量等待超时。

    流沉默（等待窗口内无事件）**不归本类**：沉默 = 慢而死了，归 total 会
    误导成「多给预算就能完成」（P2-2，独立审计 2026-09-27）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, "total")


async def stream_oneshot(
    provider: Any,
    messages: list[dict],
    *,
    agent_id: str = "",
    total_timeout_s: float | None = None,
    idle_timeout_s: float | None = None,
    first_chunk_timeout_s: float | None = None,
    temperature: float = 0.3,
) -> dict:
    """单次流式补全：SSE 逐事件消费，idle 看门狗 + 总时长帽双闸。

    传输形状复用主链路 ``_do_streaming_request``（同步 httpx 跑线程池 +
    queue 推回事件循环 —— Windows 上 asyncio CancelledError 无法中断
    httpx 同步 socket read，线程 + 主动 close 是既定解法）。

    Returns:
        {"text", "thinking", "finish_reason", "usage", "duration_ms"}；
        usage 为 provider.extract_usage 的合并结果（OpenAI/anthropic 两
        形状，记账侧再过 normalize_usage）。

    Raises:
        OneshotIdleTimeout: 首 chunk / 事件间隔判死（timeout_layer 随行）。
        OneshotTotalTimeout: 总时长帽判死。
        RetryableError / PermanentError: HTTP 状态 / 流中错误 chunk /
            传输异常（经 classify_http_error / should_retry 判定）。
    """
    started = time.monotonic()
    total_s = total_timeout_s if total_timeout_s is not None else oneshot_total_timeout_s()
    deadline = started + total_s
    idle_s = idle_timeout_s if idle_timeout_s is not None else oneshot_idle_timeout_s()
    first_s = (
        first_chunk_timeout_s
        if first_chunk_timeout_s is not None
        else FIRST_CHUNK_TIMEOUT_S
    )

    body = provider.build_body(
        messages=messages,
        stream=True,
        temperature=temperature,
    )
    # opencode Go 网关按会话稳定键发 x-opencode-session（缺失即 400）；
    # 其他网关 build_headers 内 no-op。与主链路同源，不另立头。
    headers = provider.build_headers(session_id=agent_id or "oneshot")
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    # Socket read 必须活得比 idle 看门狗久（否则 httpx 先杀，归因失真）；
    # idle 被 env 调大时 socket 跟涨。
    read_to = httpx.Timeout(
        read=max(IDLE_TIMEOUT_S, idle_s) + 30.0, connect=10, write=10, pool=10
    )

    url = provider.build_url()
    loop = asyncio.get_running_loop()
    event_q: asyncio.Queue = asyncio.Queue()
    _DONE = object()
    _ERR = object()
    client_holder: dict[str, httpx.Client] = {}

    def _close_http_client() -> None:
        orphan_client = client_holder.get("client")
        if orphan_client is not None:
            try:
                orphan_client.close()
            except Exception:  # noqa: BLE001 — best-effort 断流
                pass

    def _run_sync() -> None:
        """线程内：HTTP 请求 + SSE 解析，事件即时入队（主链路同款）。"""
        http_client = httpx.Client(timeout=read_to)
        client_holder["client"] = http_client
        try:
            with http_client.stream(
                "POST", url, headers=headers, content=body_bytes,
            ) as response:
                if response.status_code != 200:
                    body_text = response.read().decode("utf-8", errors="replace")[:500]
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
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
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
                        loop.call_soon_threadsafe(event_q.put_nowait, ("event", ev))
            loop.call_soon_threadsafe(event_q.put_nowait, (_DONE, None))
        except httpx.ReadTimeout:
            loop.call_soon_threadsafe(
                event_q.put_nowait, (_ERR, {"ok": False, "timeout": True})
            )
        except httpx.ConnectError as e:
            loop.call_soon_threadsafe(
                event_q.put_nowait, (_ERR, {"ok": False, "connect_error": str(e)})
            )
        except Exception as e:  # noqa: BLE001 — 传输层兜底，交给分类层
            loop.call_soon_threadsafe(
                event_q.put_nowait, (_ERR, {"ok": False, "error": str(e)})
            )
        finally:
            http_client.close()

    # 信号量在 HTTP 请求级占槽（与主链路同帽）；等待期受总时长帽约束，
    # 不单独 ping —— oneshot 无 UI 流可喂，超时归因走 total。
    sem = _get_llm_semaphore()
    try:
        await asyncio.wait_for(
            sem.acquire(), timeout=max(0.1, deadline - time.monotonic())
        )
    except asyncio.TimeoutError:
        raise OneshotTotalTimeout(
            f"Oneshot total timeout ({total_s:.0f}s wall clock; "
            "stuck waiting for LLM semaphore)"
        ) from None
    executor_task: asyncio.Future | None = None
    try:
        executor_task = loop.run_in_executor(None, _run_sync)
    except BaseException:
        sem.release()
        raise

    text_acc = ""
    thinking_acc = ""
    finish_reason: str | None = None
    usage: dict | None = None
    got_event = False
    last_event_ts = started
    abandon_executor = False
    try:
        while True:
            # idle 看门狗：只等下一个 SSE 事件（首事件前 first_s，之后
            # idle_s）；总时长帽钳住单次等待上限。
            wait_s = first_s if not got_event else idle_s
            remain_s = deadline - time.monotonic()
            wait_s = min(wait_s, max(0.05, remain_s))
            try:
                kind, payload = await asyncio.wait_for(event_q.get(), timeout=wait_s)
            except asyncio.TimeoutError:
                # P2-2（独立审计 2026-09-27）：等待窗口内无事件 = 真沉默，
                # **恒归 idle 族** —— 即使沉默窗口被剩余预算提前截断
                # （剩余 < idle 帽时到点）。total 只留给「事件仍在流动时
                # 预算耗尽」（下方逐事件判定）与信号量等待超时；沉默归
                # total 会误导成「多给预算就能完成」。
                abandon_executor = True
                _close_http_client()
                silent_s = time.monotonic() - last_event_ts
                cap_s = first_s if not got_event else idle_s
                if not got_event:
                    log.warning(
                        "oneshot_first_chunk_timeout",
                        agent_id=agent_id,
                        silent_s=round(silent_s, 2),
                        cap_s=cap_s,
                    )
                    raise OneshotIdleTimeout(
                        f"Oneshot first chunk timeout "
                        f"({silent_s:.1f}s silent, cap {cap_s:g}s) — "
                        "upstream never started streaming",
                        timeout_layer="first_chunk",
                    ) from None
                log.warning(
                    "oneshot_idle_timeout",
                    agent_id=agent_id,
                    silent_s=round(silent_s, 2),
                    cap_s=cap_s,
                    text_len=len(text_acc),
                )
                raise OneshotIdleTimeout(
                    f"Oneshot stream idle timeout "
                    f"({silent_s:.1f}s silent, idle cap {cap_s:g}s)"
                ) from None

            if kind is _DONE:
                break
            if kind is _ERR:
                raw = payload
                if raw.get("timeout"):
                    # socket read 超时（正常应晚于应用层看门狗触发）。归因
                    # 与主链路同判据：首 token 前可重试、开口后判死 —— 但
                    # 消息用 oneshot 实际 socket 帽（idle+30），不用主链路
                    # 常量（idle 可被 env 调大，主链路文案会失真）。
                    if got_event:
                        raise PermanentError(
                            f"Oneshot socket read timeout after tokens "
                            f"({max(IDLE_TIMEOUT_S, idle_s) + 30.0:.0f}s)"
                        )
                    raise RetryableError(
                        f"Oneshot socket read timeout before first token "
                        f"({max(IDLE_TIMEOUT_S, idle_s) + 30.0:.0f}s)"
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
                        provider=getattr(provider, "model_name", None),
                        model=getattr(provider, "model_name", None),
                        agent_id=agent_id,
                    )
                err_text = raw.get("error", "Unknown HTTP error")
                if is_region_unavailable_error(err_text):
                    raise PermanentError(err_text)
                raise RetryableError(err_text)

            got_event = True
            last_event_ts = time.monotonic()
            # 逐事件总时长判定：连续快事件流会让 wait 永不超时，「过预算」
            # 必须是逐事件判定的结构属性（主链路 2026-08-08 审计同款）。
            # 这是 total 归因的唯一流内出口 —— 事件仍在流动 = 慢而活着，
            # 预算耗尽才判 total（沉默死亡走上方 wait 分支归 idle 族）。
            if time.monotonic() >= deadline:
                abandon_executor = True
                _close_http_client()
                log.warning(
                    "oneshot_total_timeout",
                    agent_id=agent_id,
                    total_s=total_s,
                    elapsed_s=round(time.monotonic() - started, 1),
                    text_len=len(text_acc),
                )
                raise OneshotTotalTimeout(
                    f"Oneshot total timeout ({total_s:.0f}s wall clock)"
                ) from None

            event = payload
            if not isinstance(event, dict):
                continue
            try:
                extracted = provider.extract_usage(event)
            except (TypeError, ValueError, OverflowError, AttributeError):
                extracted = None
            if extracted:
                usage = {**(usage or {}), **extracted}
            for c in provider.parse_stream_chunk(event):
                ctype = c.get("type")
                if ctype == "text":
                    text_acc += c["content"]
                elif ctype == "reasoning":
                    thinking_acc += c["content"]
                elif ctype == "finish":
                    finish_reason = (
                        c.get("reason") or c.get("finish_reason") or finish_reason
                    )
                elif ctype == "usage":
                    u = c.get("usage", {})
                    if u:
                        usage = usage or {}
                        usage.update(u)
                elif ctype == "error":
                    error_content = str(c.get("content", ""))
                    log.warning(
                        "oneshot_sse_error_chunk",
                        agent_id=agent_id,
                        error=error_content,
                    )
                    raise classify_http_error(
                        None,
                        error_content,
                        model=getattr(provider, "model_name", None),
                        agent_id=agent_id,
                    )
                # tool_call_* / thinking_* / message_stop：oneshot 不带工具，
                # 静默忽略（不仿主链路拼 tool_calls）。
    except BaseException as e:
        if not abandon_executor:
            abandon_executor = True
            _close_http_client()
        # 重试判定依据：流是否开过口（挂异常随行，setattr 保 mypy 基线）
        try:
            setattr(e, "oneshot_got_event", got_event)
        except Exception:  # noqa: BLE001 — setattr 随行是 best-effort
            pass
        # 断流/超时路径保住已收 usage（记账侧可选消费；主链路 45 轮 P1 同款）
        if usage:
            try:
                setattr(e, "partial_usage", dict(usage))
            except Exception:  # noqa: BLE001 — setattr 保账是 best-effort
                pass
        raise
    finally:
        sem.release()
        if executor_task is not None:
            if abandon_executor:
                # 判死路径不能 await 线程（阻塞在 socket read 上）；已 close
                # 客户端，cancel 防幽灵 HTTP 占 executor（主链路同款）。
                if not executor_task.done():
                    executor_task.cancel()
            else:
                try:
                    await executor_task
                except Exception:  # noqa: BLE001 — 线程内异常已走 _ERR 通道
                    pass

    log.info(
        "oneshot_stream_done",
        agent_id=agent_id,
        text_len=len(text_acc),
        thinking_len=len(thinking_acc),
        finish=finish_reason,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {
        "text": text_acc,
        "thinking": thinking_acc,
        "finish_reason": finish_reason,
        "usage": usage,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _retry_backoff_s(exc: BaseException) -> float:
    """429/503 尊重 Retry-After（帽 MAX_DELAY_MS）；其余小退避 0.5-1s。"""
    if isinstance(exc, RetryableError) and getattr(exc, "status", None) in (429, 503):
        retry_after_ms = parse_retry_after_ms(getattr(exc, "headers", None))
        if retry_after_ms is not None:
            return min(retry_after_ms, MAX_DELAY_MS) / 1000.0
    return random.uniform(*_RETRY_BACKOFF_RANGE_S)


def _retry_allowed(exc: BaseException) -> bool:
    """新重试语义：只对「连接失败 / 立即拒绝」重试。

    - 流已开口（``oneshot_got_event=True``）后的任何死亡 → 一律不重试：
      重发 = 整段生成重来，慢而活着/中途断流都不是重试 trigger；
    - HTTP 429/5xx **立即拒绝**（非 200 分支，流未开口）→ 可重试；
    - connect 类传输异常且流未开口 → 可重试；
    - idle / 首 chunk / 总时长判死、其他 4xx、内容层错误 → 不重试。

    「是否收到事件」由 stream_oneshot 挂在异常上的 ``oneshot_got_event``
    读取（setattr 形式保 mypy 基线，同 partial_usage 惯例）。
    """
    if isinstance(exc, (OneshotIdleTimeout, OneshotTotalTimeout, PermanentError)):
        return False
    if getattr(exc, "oneshot_got_event", False):
        return False
    if isinstance(exc, RetryableError):
        # 429/5xx 立即拒绝 + 无状态码的传输层/文本命中重试模式，都算
        # 「流未开口的连接失败/立即拒绝」（开口后已被上面的 got_event 挡住）。
        return True
    # 裸 httpx 网络异常（理论上已包成 RetryableError，兜底防线）
    return should_retry_exception(exc)


async def stream_oneshot_with_retry(
    provider: Any,
    messages: list[dict],
    *,
    agent_id: str = "",
    total_timeout_s: float | None = None,
    idle_timeout_s: float | None = None,
    first_chunk_timeout_s: float | None = None,
    temperature: float = 0.3,
    max_retries: int = 1,
) -> dict:
    """stream_oneshot + 受限重试（≤max_retries，仅连接失败/立即拒绝）。

    总时长帽是**跨尝试**的：deadline 在本函数入口一次算定，每次尝试只拿
    剩余预算 —— 重试不可能把总墙钟顶破 ``HIVEWEAVE_CODE_AUDIT_TIMEOUT_S``。
    """
    started = time.monotonic()
    total_s = total_timeout_s if total_timeout_s is not None else oneshot_total_timeout_s()
    deadline = started + total_s
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            assert last_exc is not None
            raise last_exc
        try:
            return await stream_oneshot(
                provider,
                messages,
                agent_id=agent_id,
                total_timeout_s=remaining,
                idle_timeout_s=idle_timeout_s,
                first_chunk_timeout_s=first_chunk_timeout_s,
                temperature=temperature,
            )
        except (OneshotTimeoutError, PermanentError):
            raise
        except Exception as exc:  # noqa: BLE001 — 分类后决定重试或上抛
            last_exc = exc
            if attempt >= max_retries or not _retry_allowed(exc):
                log.warning(
                    "oneshot_retry_not_allowed",
                    agent_id=agent_id,
                    attempt=attempt,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                raise
            delay_s = _retry_backoff_s(exc)
            if time.monotonic() + delay_s > deadline - _RETRY_MIN_REMAINING_S:
                log.warning(
                    "oneshot_retry_abandon_over_budget",
                    agent_id=agent_id,
                    remaining_s=round(deadline - time.monotonic(), 1),
                )
                raise
            log.info(
                "oneshot_retry_scheduled",
                agent_id=agent_id,
                attempt=attempt + 1,
                delay_s=round(delay_s, 3),
                reason=type(exc).__name__,
            )
            await asyncio.sleep(delay_s)
    assert last_exc is not None
    raise last_exc


async def record_oneshot_usage(
    *,
    agent_id: str,
    project_id: str | None,
    provider: Any,
    model_config: dict,
    result: dict,
    request_type: str = "oneshot",
) -> None:
    """oneshot 流式调用进 llm_usage 记账（批 G cache_warm 先例同纪律）。

    绕过 Streamer 的调用必须单独记账，否则 Token 页/命中率对审计/评审
    流量全盲。usage 从流式末事件取（OpenAI/anthropic 两形状都过
    normalize_usage），duration 用流式实测值。best-effort：记账失败只打
    debug，绝不影响调用本体。
    """
    usage = result.get("usage")
    if not usage:
        return
    try:
        from hiveweave.llm.util import normalize_usage
        from hiveweave.services.token_meter import token_meter

        provider_value: str | None = None
        try:
            provider_value = provider.api_format.value
        except Exception:  # noqa: BLE001 — provider 值缺失不挡记账
            pass
        norm = normalize_usage(usage, provider_value)
        if not norm:
            return
        await token_meter.record_rounds(
            agent_id,
            project_id,
            [
                {
                    "input": norm.get("input", 0),
                    "output": norm.get("output", 0),
                    "cache_read": norm.get("cache_read", 0),
                    "cache_creation": norm.get("cache_creation", 0),
                    "total": norm.get("total", 0),
                    "duration_ms": int(result.get("duration_ms") or 0),
                    "cache_creation_reported": bool(
                        norm.get("cache_creation_reported")
                    ),
                }
            ],
            model_id=str(model_config.get("model_id") or ""),
            provider=provider_value,
            request_type=request_type,
        )
    except Exception as e:  # noqa: BLE001 — 记账失败不影响 oneshot 本体
        log.debug("oneshot_meter_failed", agent_id=agent_id, error=str(e))
