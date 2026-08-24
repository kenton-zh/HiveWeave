"""重试逻辑 — 指数退避 + jitter + Retry-After header 解析。

契约 01: LLM 流式调用 — 重试与熔断
- 可重试状态码: 429, 503, 504, 529
- 最多 2 次重试（MAX_RETRIES）
- 指数退避: base=1s, factor=2, jitter=±25%（即 [0.75, 1.25]）
- 解析 Retry-After header（OpenAI retry-after-ms + 标准 retry-after 秒/HTTP-date）
- 参考: Elixir retry.ex + TS retry-utils.ts

注意:
- 本模块仅处理「单次 HTTP 请求」级别的重试，由 streamer 在 tool loop 每轮调用。
- 空响应重试（无 content 无 tool_calls）由 streamer 自行处理，不走本模块。
- 超时重试同样由 streamer 的 request_with_retry 驱动，本模块提供 should_retry 判定。
"""

from __future__ import annotations

import asyncio
import random
import re
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

import structlog

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
MAX_RETRIES = 2
"""最大重试次数（不含首次请求）。契约 01。"""

BASE_DELAY_MS = 1_000
"""基础退避延迟（1 秒）。用户指定 base=1s。"""

MAX_DELAY_MS = 30_000
"""单次退避上限（30 秒），防止 Retry-After 返回过大值。"""

RETRYABLE_STATUSES: frozenset[int] = frozenset(range(500, 600)) | frozenset({429})
"""可重试的 HTTP 状态码：429 + 全量 5xx。

历史实现只认 {429, 503, 504, 529}，但多厂商网关（ARK / DeepSeek / OpenAI /
任何 OpenAI-compatible）抽风时常用 500 / 502 / 524。漏掉这些会把瞬态故障
误判为 PermanentError，直接炸掉 agent。对齐 opencode retry.ts 的
`status >= 500` 一律可重试策略。
"""

# 消息内容级可重试模式（厂商无关，移植自 opencode retry.ts RETRYABLE_MESSAGE_PATTERNS）。
# 适用场景：网关在 HTTP 200 / 非标准 4xx body 里包瞬态错误文本（如
# "upstream server error"、"rate limit reached"），仅靠状态码无法识别。
# 注意：这里匹配的是错误 body / 错误消息文本，不是正常响应流。
#
# 状态码单条用 (?<![\d])…(?![\d]) 锚定为独立数字，避免误命中真实业务数字
# （"requested 100500 tokens"→500 / 日期→429 / "524288"→524）。real 事故见
# tests/test_retry_message_classify.py::test_non_retryable。
_RETRYABLE_MESSAGE_PATTERNS: tuple[str, ...] = (
    r"(?<![\d])(?:429|5\d{2})(?![\d])",
    r"rate increased too quickly|rate limit|rate-limit|rate_limit|too many requests|too_many_requests",
    r"overloaded|service unavailable|service_unavailable|service-unavailable|"
    r"internal error|internal_error|internal server error|server error|server_error|server-error|"
    r"provider returned error|provider_returned_error|provider-returned-error",
    r"terminated|fetch failed|failed to fetch|network error|upstream connect|"
    r"connection error|connection refused|connection lost|"
    r"socket connection was closed|socket hang up|reset by peer|getaddrinfo|gai_error|"
    r"enotfound|eai_again|econnrefused|econnreset|etimedout",
    r"^timeout$|\b(?:request|response|connection|network|stream|read) (?:timeout|timed out|time out)\b",
    r"try your request|retry your request|resource exhausted|resource_exhausted",
    # 中文厂商文案（ARK / 火山引擎 / 字节豆包 / 通义等常见 on-line 网关）：
    # 英文模式对中文错误文本 miss，导致瞬态错误被误判为永久错误。
    r"限流|请求过于频繁|访问频率过高|触发(?:了)?限流",
    r"过载|服务器(?:内部|异常|不可用|繁忙)|服务(?:繁忙|不可用)|上游|暂时无法|暂不可用|"
    r"连接(?:失败|中断|重置|超时)|网络连接|读超时|(?:请求|响应|读)超时",
    r"请(?:稍后|重新)重试|请(?:您)?重试|稍后重试|临时(?:故障|问题)",
)
_COMPILED_RETRYABLE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE) for p in _RETRYABLE_MESSAGE_PATTERNS
)

T = TypeVar("T")


# ── 状态码判定 ──────────────────────────────────────────────

def is_retryable_status(status: int) -> bool:
    """判断 HTTP 状态码是否可重试（429 + 全量 5xx）。"""
    return status in RETRYABLE_STATUSES


# ── E7: 容量错误分类（vs 瞬时限流）─────────────────────────────
# 复盘致命链二：daily_quota / GoUsageLimitError 是**容量耗尽**（5 小时滚动
# 配额 / 日配额），秒级退避重试无济于事，只会白撞 75 个 error runs。容量
# 错误的恢复钥匙是「配额窗口重置」，应进项目级暂停/排队，而不是逐次重试。
_CAPACITY_NEEDLES: tuple[str, ...] = (
    "gousagelimiterror",        # muse / ox 5 小时滚动配额
    "usage_limit_reached",
    "usage limit exceeded",
    "daily_quota",
    "daily quota",
    "quota exhausted",
    "quota_exhausted",
    "quota exceeded",
    "quota_exceeded",
    "insufficient quota",
)


def is_capacity_error(message: str) -> bool:
    """是否容量类错误（配额耗尽）。

    与瞬时限流（typical 429 rate limit）区分：容量错误的重置以「窗口」计，
    进重试只会白撞；is_daily_quota（header 解析 > 10min）在此之上给出
    确定的重置时刻。
    """
    if not message:
        return False
    m = str(message).lower()
    return any(n in m for n in _CAPACITY_NEEDLES)


# 窗口词：容量错误触发「项目级暂停」需要重置窗口信号（daily/hourly/window/
# reset 或中文 日/小时/窗口/滚动），避免把普通每分钟限流误判成 1 小时暂停。
_WINDOW_NEEDLES: tuple[str, ...] = (
    "window", "daily", "hourly", "reset",
    "滚动", "窗口", "日配额", "每日", "小时",
)


def is_window_quota_error(message: str, *, is_daily: bool = False) -> bool:
    """容量错误且带明确窗口重置信号（可安全做项目级暂停）。

    比 ``is_capacity_error`` 更窄：后者只用于 RetryHandler「不逐次重试」
    （宽判防白撞），前者用于「组织级降速到重置窗口」（窄判防过度冷却）。
    """
    if is_daily:
        return True
    if not message:
        return False
    m = str(message).lower()
    if not is_capacity_error(m):
        return False
    return any(w in m for w in _WINDOW_NEEDLES)


def matches_retryable_message(value: str) -> bool:
    """判断错误文本是否命中可重试内容模式（厂商无关）。

    移植 opencode retry.ts：错误消息即使状态码正常（如 HTTP 200 / 非标准
    4xx），只要文本命中速率限制 / 过载 / 网络问题 / 明确要求重试等模式，
    就应视为瞬态故障重试。厂商无关，写死的只是通用英文错误文本。
    """
    return any(pattern.search(value) for pattern in _COMPILED_RETRYABLE_PATTERNS)


def classify_http_error(
    status: int | None,
    body: str,
    headers: dict[str, str] | None = None,
) -> RetryableError | PermanentError:
    """把一次 HTTP/流错误分类为可重试或永久错误。

    优先级：状态码（429 + 5xx）或 body 内容命中可重试模式 → RetryableError；
    否则 PermanentError。用于 ``streamer/http_stream.py`` 的非 200 分支
    和流中 error chunk —— 兜住多厂商「状态码正常但 body 包瞬态错误」的情况。
    """
    snippet = body[:500]
    if status is not None:
        message = f"HTTP {status}: {snippet}"
    else:
        message = snippet
    if (status is not None and is_retryable_status(status)) or matches_retryable_message(body):
        return RetryableError(message, status=status, headers=headers or {})
    return PermanentError(message, status=status)


def should_retry_exception(exc: BaseException) -> bool:
    """判断异常是否值得重试（网络错误/超时）。

    httpx.ConnectError / ReadTimeout / PoolTimeout / RemoteProtocolError
    等瞬时网络故障都应重试。参考 Elixir should_retry?/1。
    """
    # 延迟导入避免循环依赖
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                        httpx.WriteTimeout, httpx.PoolTimeout,
                        httpx.RemoteProtocolError, httpx.ReadError)):
        return True
    # asyncio.TimeoutError 是 streamer 的 idle watchdog 抛出的
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return False


# ── Retry-After header 解析 ─────────────────────────────────

def parse_retry_after_ms(headers: dict[str, str] | None) -> int | None:
    """解析 Retry-After header，返回毫秒数。

    支持三种格式（对齐 TS retry-utils.ts）:
      1. retry-after-ms: 5000  — 毫秒（OpenAI 非标准但常用）
      2. retry-after: 5  — 秒
      3. retry-after: Wed, 21 Oct 2025 07:28:00 GMT  — HTTP-date

    Returns:
        延迟毫秒数，无 header 或解析失败返回 None。
    """
    if not headers:
        return None

    # 1. retry-after-ms（毫秒）
    ms_val = headers.get("retry-after-ms")
    if ms_val is not None:
        try:
            return max(0, int(ms_val))
        except (ValueError, TypeError):
            pass

    # 2/3. retry-after（秒或 HTTP-date）
    val = headers.get("retry-after")
    if val is None:
        # header 名大小写不敏感兜底
        val = _get_header_ci(headers, "retry-after")
    if val is None:
        ms_val = _get_header_ci(headers, "retry-after-ms")
        if ms_val is not None:
            try:
                return max(0, int(ms_val))
            except (ValueError, TypeError):
                pass
        return None

    # 尝试作为秒数解析
    try:
        seconds = float(val)
        return max(0, int(seconds * 1000))
    except (ValueError, TypeError):
        pass

    # 尝试作为 HTTP-date 解析
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            delta_ms = int((dt - now).total_seconds() * 1000)
            return max(0, delta_ms)
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def _get_header_ci(headers: dict[str, str], name: str) -> str | None:
    """大小写不敏感地获取 header 值。"""
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def parse_quota_reset(
    headers: dict[str, str] | None,
) -> dict[str, float | bool | None]:
    """Parse 429 quota reset timing from response headers.

    Prefers ``X-RateLimit-Reset`` (unix epoch or seconds-until), then
    ``Retry-After``. Used by agent-level park (not in-request retry cap).

    Returns::
        {
          retry_after_s: float | None,
          reset_at_epoch: float | None,
          is_daily_quota: bool,  # reset > QUOTA_EXHAUST_THRESHOLD_S
        }
    """
    import time as _time

    QUOTA_EXHAUST_THRESHOLD_S = 600.0  # >10 min → treat as quota exhaustion
    out: dict[str, float | bool | None] = {
        "retry_after_s": None,
        "reset_at_epoch": None,
        "is_daily_quota": False,
    }
    if not headers:
        return out

    now = _time.time()
    reset_raw = _get_header_ci(headers, "x-ratelimit-reset") or _get_header_ci(
        headers, "x-ratelimit-reset-requests"
    )
    if reset_raw:
        try:
            val = float(str(reset_raw).strip())
            # Heuristic: values > 1e9 look like unix epoch; else seconds-until
            if val > 1_000_000_000:
                out["reset_at_epoch"] = val
                out["retry_after_s"] = max(0.0, val - now)
            else:
                out["retry_after_s"] = max(0.0, val)
                out["reset_at_epoch"] = now + max(0.0, val)
        except (ValueError, TypeError):
            pass

    if out["retry_after_s"] is None:
        ra_ms = parse_retry_after_ms(headers)
        if ra_ms is not None:
            # parse_retry_after_ms is capped at MAX_DELAY_MS for in-request
            # retry — for quota park, re-parse without the 30s cap.
            ra_uncapped = _parse_retry_after_uncapped(headers)
            secs = (ra_uncapped if ra_uncapped is not None else ra_ms) / 1000.0
            out["retry_after_s"] = secs
            out["reset_at_epoch"] = now + secs

    ra = out["retry_after_s"]
    if isinstance(ra, (int, float)) and ra > QUOTA_EXHAUST_THRESHOLD_S:
        out["is_daily_quota"] = True
    return out


def _parse_retry_after_uncapped(headers: dict[str, str]) -> int | None:
    """Like parse_retry_after_ms but without MAX_DELAY_MS cap (for quota park)."""
    ms_val = _get_header_ci(headers, "retry-after-ms")
    if ms_val is not None:
        try:
            return max(0, int(float(ms_val)))
        except (ValueError, TypeError):
            pass
    val = _get_header_ci(headers, "retry-after")
    if val is None:
        return None
    try:
        return max(0, int(float(val) * 1000))
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            return max(0, int((dt - now).total_seconds() * 1000))
    except (ValueError, TypeError, OverflowError):
        pass
    return None


# ── 退避计算 ────────────────────────────────────────────────

def compute_backoff(attempt: int, retry_after_ms: int | None = None) -> int:
    """计算第 attempt 次重试的退避延迟（毫秒）。

    公式: BASE * 2^attempt * jitter, jitter ∈ [0.75, 1.25]（±25%）。
    若提供 retry_after_ms，则优先使用（capped at MAX_DELAY_MS）。

    Args:
        attempt: 当前重试序号（0-based，0 = 第一次重试）。
        retry_after_ms: 来自 Retry-After header 的延迟（毫秒），优先级最高。

    Returns:
        延迟毫秒数。
    """
    if retry_after_ms is not None:
        return min(retry_after_ms, MAX_DELAY_MS)

    # 指数退避: 1s, 2s, 4s, 8s, ...
    base = BASE_DELAY_MS * (2 ** attempt)
    # jitter ±25% → [0.75, 1.25]
    jitter = 0.75 + random.random() * 0.5
    return min(int(base * jitter), MAX_DELAY_MS)


# ── 错误分类 ────────────────────────────────────────────────

class RetryableError(Exception):
    """可重试的错误（HTTP 429/503/504/529 或网络错误）。

    携带 HTTP 状态码和响应头，供 RetryHandler 解析 Retry-After。
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


class PermanentError(Exception):
    """不可重试的错误（401 认证失败、400 请求错误等）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ── RetryHandler ────────────────────────────────────────────

class RetryHandler:
    """异步重试处理器。

    用法::

        handler = RetryHandler()
        result = await handler.with_retry(lambda: do_request())

    回调应返回结果或抛出 RetryableError / PermanentError。
    网络异常（httpx.ConnectError 等）自动判定为可重试。

    每次「实际重试」会触发 on_retry 回调（用于日志/遥测）。
    全部重试耗尽后抛出最后一次的 RetryableError。
    """

    def __init__(
        self,
        max_retries: int = MAX_RETRIES,
        on_retry: Callable[[int, int, BaseException], Awaitable[None] | None] | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.on_retry = on_retry

    async def with_retry(
        self,
        fn: Callable[[], Awaitable[T]],
    ) -> T:
        """执行带重试的异步函数。

        - 首次调用 + 最多 max_retries 次重试。
        - 指数退避 + jitter，Retry-After header 优先。
        - PermanentError 立即抛出，不重试。
        - 非可重试异常也不重试（直接抛出）。
        """
        last_exc: BaseException | None = None
        for attempt in range(self.max_retries + 1):  # 0..max_retries
            try:
                return await fn()
            except PermanentError:
                # 不可重试 — 直接抛出
                raise
            except RetryableError as e:
                # E7: 容量错误（配额耗尽）不进逐次重试——退避解决不了窗口级
                # 问题，白撞只会更快烧完上下文/预算。立即上抛，交给 agent 层
                # 项目级暂停/排队（配额窗口恢复后再批量唤醒）。
                if is_capacity_error(str(e)):
                    log.warning(
                        "capacity_error_no_retry",
                        status=e.status,
                        error=str(e)[:200],
                    )
                    raise
                last_exc = e
                if attempt >= self.max_retries:
                    log.warning(
                        "retry_exhausted",
                        attempt=attempt,
                        status=e.status,
                        error=str(e),
                    )
                    raise
                delay = compute_backoff(attempt, parse_retry_after_ms(e.headers))
                log.info(
                    "retry_scheduled",
                    attempt=attempt + 1,
                    delay_ms=delay,
                    status=e.status,
                    reason=str(e),
                )
                await self._fire_retry(attempt + 1, delay, e)
                await asyncio.sleep(delay / 1000.0)
            except Exception as e:
                # 其他异常 — 检查是否为可重试的网络错误
                if should_retry_exception(e):
                    last_exc = e
                    if attempt >= self.max_retries:
                        log.warning("retry_exhausted_network", attempt=attempt, error=str(e))
                        raise
                    delay = compute_backoff(attempt)
                    log.info(
                        "retry_scheduled_network",
                        attempt=attempt + 1,
                        delay_ms=delay,
                        reason=type(e).__name__,
                    )
                    await self._fire_retry(attempt + 1, delay, e)
                    await asyncio.sleep(delay / 1000.0)
                else:
                    raise

        # 理论上不可达（循环内必返回或抛出），保险起见
        assert last_exc is not None
        raise last_exc

    async def _fire_retry(
        self, attempt: int, delay_ms: int, exc: BaseException
    ) -> None:
        """触发 on_retry 回调（支持同步/异步）。"""
        if self.on_retry is None:
            return
        result = self.on_retry(attempt, delay_ms, exc)
        if asyncio.iscoroutine(result):
            await result
