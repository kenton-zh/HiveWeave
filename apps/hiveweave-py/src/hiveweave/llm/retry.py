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
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

import structlog

log = structlog.get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
MAX_RETRIES = 2
"""最大重试次数（不含首次请求）。契约 01。"""

BASE_DELAY_MS = 1_000
"""指数退避基础延迟（1 秒）。用户指定 base=1s。"""

MAX_DELAY_MS = 30_000
"""单次退避上限（30 秒），防止 Retry-After 返回过大值。"""

RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503, 504, 529})
"""可重试的 HTTP 状态码。对齐 Elixir/TS。"""

T = TypeVar("T")


# ── 状态码判定 ──────────────────────────────────────────────

def is_retryable_status(status: int) -> bool:
    """判断 HTTP 状态码是否可重试。"""
    return status in RETRYABLE_STATUSES


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
