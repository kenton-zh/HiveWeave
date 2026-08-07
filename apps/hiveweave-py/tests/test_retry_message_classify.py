"""多厂商错误文本重试判定 — matches_retryable_message + classify_http_error 单测。

背景: 移植 opencode retry.ts 的 RETRYABLE_MESSAGE_PATTERNS —— 厂商无关的
内容级错误识别。HiveWeave 可能接入任意厂商（ARK / DeepSeek / OpenAI / Groq /
OpenRouter / 自建兼容网关…），很多网关把瞬态错误包在 HTTP 200 body 或非标准
4xx 里，仅靠状态码 {429,503,504,529} 无法识别。本测试锁定:
  - matches_retryable_message: 识别的正样本 + 不误判的负样本
  - classify_http_error: 状态码 + 内容双通道判定
  - is_retryable_status: 429 + 全量 5xx

被测模块: hiveweave.llm.retry
"""

from __future__ import annotations

import pytest

from hiveweave.llm.retry import (
    PermanentError,
    RetryableError,
    classify_http_error,
    is_retryable_status,
    matches_retryable_message,
)


# ── is_retryable_status ─────────────────────────────────────


class TestIsRetryableStatus:
    """状态码判定 — 429 + 全量 5xx 可重试; 4xx/其他 不可重试."""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 524, 529])
    def test_retryable_statuses(self, status):
        """429 及全量 5xx 都应判定为可重试."""
        assert is_retryable_status(status)

    @pytest.mark.parametrize(
        "status",
        [400, 401, 402, 403, 404, 408],  # 4xx 客户端错误不可重试
    )
    def test_non_retryable_statuses(self, status):
        """4xx 客户端错误不应判定为可重试."""
        assert not is_retryable_status(status)


# ── matches_retryable_message ───────────────────────────────────────

class TestMatchesRetryableMessage:
    """内容级识别 — 厂商无关的瞬态错误文本应命中; 正常语义不误判."""

    @pytest.mark.parametrize(
        "body",
        [
            # 限流（DeepSeek / 各兼容网关常见文案）
            '{"error":{"message":"Rate limit reached for org-xxx"}}',
            'error: 429 Too Many Requests',
            "upstream rate_limit exceeded, retry later",
            # 过载 / 上游错误（ARK 400 body 包 upstream 错误）
            '{"error":{"message":"upstream server error, please retry"}}',
            "Service Unavailable (503)",
            "Internal Server Error",
            "server_error: 502 Bad Gateway",
            # 网络层 / 连接
            "failed to fetch https://api.xxx.com/chat/completions",
            "fetch failed: socket hang up",
            "connection reset by peer (ECONNRESET)",
            "getaddrinfo ENOTFOUND api.xxx.com",
            # 超时语义
            "request timed out after 30s",
            "stream read timeout",
            # 明确要求重试 / 资源耗尽
            "Please retry your request later",
            "resource exhausted: quota for current provider",
            # 中文厂商文案（ARK / 火山引擎 / 通义等 on-line 网关）
            "上游服务繁忙，请稍后重试",
            "请求过于频繁，请稍后再试",
            "网关服务器过载，暂时无法处理",
            "读超时，请重试",
            "服务器内部错误",
            "触发限流，请稍后重试",
            # 独立状态码（HTTP 503 / 424 独立 token）
            "status: HTTP 503 Service Unavailable",
            '{"code":"502 Bad Gateway"}',
        ],
    )
    def test_positive_retryable(self, body):
        """瞬态错误文本（限流/过载/网络/超时/要求重试/中文文案）都应命中."""
        assert matches_retryable_message(body), f"should match: {body}"

    @pytest.mark.parametrize(
        "body",
        [
            # 永久性非瞬态错误 —— 不应误判为重试
            "invalid_api_key: Invalid API key provided",
            "401 Unauthorized: authentication failed",
            "400 invalid_request_error: message must end with user role",
            "Model only support text input",
            "max_tokens exceeded model limit",
            "The model 'xxx' is not found or not activated",
            "prompt too long: 142857 > 128000 tokens",
            # 正常业务成功文本不该命中数字模式
            "Success, 200 OK",
            # 数字误判回归：真实错误文本里的 token 数 / 时间戳 / request-id
            # 若不带锚点，会被状态码单条(?<![\d])(?:429|5\d{2})(?![\d])
            # 之前是裸 429|500|... 子串，100500/150000/日期 全部误命中。
            "This model's maximum context length is 100000 tokens. "
            "However, you requested 100500 tokens",
            "max context length is 150000 tokens",
            "max_tokens 3500 exceeds model limit",
            "request req_20250429500abc was aborted",
            "output buffer 524288 exceeds hard cap",
            "max_output_tokens 312500 invalid",
            "context_length_exceeded: 300000 > 262144",
            # 中文永久错误不应命中
            "模型不存在或未激活，请检查模型配置",
            "无效的 API Key，请检查配置",
        ],
    )
    def test_non_retryable(self, body):
        """客户端配置错 / 语义性错误 / 含数字业务文本 / 正常文本不应命中."""
        assert not matches_retryable_message(body)


# ── classify_http_error ─────────────────────────────────────────────

class TestClassifyHttpError:
    """HTTP/流错误分类 — 状态码通道 + 内容通道双识别."""

    def test_retryable_status_returns_retryable_error(self):
        """状态码 5xx → RetryableError, 携带 status 便于 Retry-After 解析."""
        err = classify_http_error(503, "Service Unavailable")
        assert isinstance(err, RetryableError)
        assert err.status == 503

    def test_permanent_status_with_retryable_body_is_retried(self):
        """400 但 body 含 upstream 错误 → 内容通道捞回, 判定可重试.

        这是本次改造的核心场景: 网关把瞬态错误打包在 4xx 里
        （如 ARK 400 "upstream server error"）, 仅凭状态码会误判为永久错误.
        """
        err = classify_http_error(400, "invalid_request: upstream server error, retry")
        assert isinstance(err, RetryableError)
        assert err.status == 400

    def test_permanent_status_with_permanent_body(self):
        """400 + 明确客户端错误文案 → PermanentError（不可重试）."""
        err = classify_http_error(400, "invalid_api_key: Bad credentials")
        assert isinstance(err, PermanentError)
        assert err.status == 400

    def test_retryable_message_without_status_is_retried(self):
        """HTTP 200 body 包瞬态错误（无状态码）→ 内容通道重试."""
        err = classify_http_error(None, "rate_limit: 429 too many requests")
        assert isinstance(err, RetryableError)
        assert err.status is None

    def test_unknown_error_without_status_is_permanent(self):
        """无状态码且无命中内容 → PermanentError（不盲目重试）."""
        err = classify_http_error(None, "unrecognized provider error")
        assert isinstance(err, PermanentError)
        assert err.status is None

    def test_error_status_does_not_leak_into_agent_kill(self):
        """永久错误保留 status, 供上层区分 402 余额耗尽等语义（TEST19）. """
        err = classify_http_error(402, "insufficient_quota")
        assert isinstance(err, PermanentError)
        assert err.status == 402