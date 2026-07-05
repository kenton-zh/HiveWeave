"""LLM 流式调用层 — Provider 工厂 + 重试 + 熔断 + 流式调用。

契约 01: LLM 流式调用

本包实现 LLM 调用的全链路:
- provider: Provider 工厂（openai/anthropic/google/openai-compatible，统一 OpenAI 兼容格式）
- retry: 重试逻辑（429/503/504/529，指数退避+jitter，Retry-After header）
- circuit_breaker: 熔断器（连续失败 5 次熔断，30s 冷却，半开试探）
- streamer: 核心流式调用 + tool loop（SSE 解析，最多 25 轮，空响应重试）

参考:
- Elixir: apps/hiveweave/lib/hiveweave/llm/{streamer,circuit_breaker,retry,provider_factory}.ex
- TS: packages/agent-runtime/src/{provider-factory,retry-utils,agent-runtime}.ts
"""

from hiveweave.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CheckResult,
    circuit_breaker,
)
from hiveweave.llm.provider import (
    ProviderConfig,
    ProviderFactory,
    ProviderType,
    provider_factory,
)
from hiveweave.llm.retry import (
    MAX_RETRIES,
    PermanentError,
    RetryHandler,
    RetryableError,
    RETRYABLE_STATUSES,
    compute_backoff,
    is_retryable_status,
    parse_retry_after_ms,
    should_retry_exception,
)
from hiveweave.llm.streamer import (
    MAX_TOOL_ROUNDS,
    Streamer,
    merge_tool_calls,
    parse_sse,
    sse_to_chunks,
)

__all__ = [
    # streamer
    "Streamer",
    "parse_sse",
    "sse_to_chunks",
    "merge_tool_calls",
    "MAX_TOOL_ROUNDS",
    # provider
    "ProviderFactory",
    "ProviderConfig",
    "ProviderType",
    "provider_factory",
    # retry
    "RetryHandler",
    "RetryableError",
    "PermanentError",
    "is_retryable_status",
    "should_retry_exception",
    "parse_retry_after_ms",
    "compute_backoff",
    "MAX_RETRIES",
    "RETRYABLE_STATUSES",
    # circuit_breaker
    "CircuitBreaker",
    "CircuitState",
    "CheckResult",
    "circuit_breaker",
]
