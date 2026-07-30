"""Streamer exceptions."""
from __future__ import annotations

class CircuitBreakerOpenError(Exception):
    """熔断器已打开，请求被拒绝（C9）。

    当 provider 连续失败达到阈值后抛出。携带 provider 名称和（可选的）
    fallback 名称，供调用方决策是否切换到备用 provider。

    简化方案：当前不实现自动 provider 切换（需要解析 fallback model config），
    直接抛出此异常让调用方知道熔断器已打开，避免原代码「只打日志不 return」
    继续用被熔断的 provider 发请求的死代码行为。
    """

    def __init__(self, provider: str, fallback: str | None = None) -> None:
        self.provider = provider
        self.fallback = fallback
        if fallback:
            msg = (
                f"Circuit breaker open for provider '{provider}' "
                f"(fallback '{fallback}' available but auto-switch "
                f"not implemented)"
            )
        else:
            msg = (
                f"Circuit breaker open for provider '{provider}' "
                f"and no fallback available"
            )
        super().__init__(msg)

