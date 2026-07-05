"""Provider 工厂 — 将模型配置转为 OpenAI 兼容的 HTTP 请求参数。

契约 01: LLM 流式调用 — Provider 工厂
- openai → OpenAI provider
- anthropic → Anthropic provider
- google → Google provider
- 其他 → OpenAI-compatible（DeepSeek, Groq, TogetherAI, ...）
- 所有 provider 统一为 OpenAI 兼容格式（简化实现，统一走 /v1/chat/completions）
- 三层超时: connect=10s / read=120s / total=300s
- 参考: Elixir provider_factory.ex + TS provider-factory.ts

简化说明:
- TS 版用 Vercel AI SDK 的 createOpenAI/createAnthropic/createGoogleGenerativeAI，
  各 provider 有不同的请求/响应格式。
- Python 版统一走 OpenAI 兼容格式（/chat/completions + SSE），
  因为 DeepSeek/Groq/TogetherAI 等 OpenAI-compatible provider 都是这个格式，
  OpenAI/Anthropic/Google 也提供 OpenAI 兼容端点。
- 这样 streamer 只需一套 SSE 解析逻辑，大幅简化实现。
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Provider 类型 ───────────────────────────────────────────

# 支持的 provider 标识符（对齐 TS ProviderType）
ProviderType = str  # "openai" | "anthropic" | "google" | "openai-compatible"

KNOWN_PROVIDERS: frozenset[str] = frozenset({
    "openai", "anthropic", "google", "openai-compatible",
})

# ── 三层超时常量（契约 01）──────────────────────────────────
CONNECT_TIMEOUT_S = 10.0
"""连接超时 10 秒。TCP 握手阶段。"""

READ_TIMEOUT_S = 120.0
"""读取超时 120 秒。两个 chunk 之间的最大间隔。"""

TOTAL_TIMEOUT_S = 300.0
"""总超时 300 秒。整个请求的生命周期上限（含流式）。"""

WRITE_TIMEOUT_S = 10.0
"""写入超时 10 秒。发送请求体阶段。"""

POOL_TIMEOUT_S = 10.0
"""连接池获取超时 10 秒。"""


def build_timeout() -> httpx.Timeout:
    """构建三层超时配置。

    connect=10s  — TCP 连接建立
    read=120s    — 两个 chunk 之间的间隔（流式 idle 超时防线）
    write=10s    — 请求体写入
    pool=10s     — 连接池获取
    total=300s   — 整个请求总超时（兜底）

    注意: httpx 的 timeout.read 在 stream 模式下作用于「两次读取之间」，
    正好对应 TS 防线②的 idle 超时语义。total=300s 对应 TS 防线③的 turn 级超时。
    """
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_S,
        read=READ_TIMEOUT_S,
        write=WRITE_TIMEOUT_S,
        pool=POOL_TIMEOUT_S,
    )


# ── ProviderConfig ─────────────────────────────────────────


class ProviderConfig:
    """单个 provider 的请求配置（OpenAI 兼容格式）。

    封装 base_url / api_key / model_name，提供:
    - build_url(): 拼接 /chat/completions 端点
    - build_headers(): Authorization Bearer + Content-Type
    - build_body(): OpenAI 兼容的请求体
    - build_client(): 创建带三层超时的 httpx.AsyncClient

    所有 provider（openai/anthropic/google/openai-compatible）共享此配置，
    因为它们都走 OpenAI 兼容格式。provider_type 仅用于日志和遥测。
    """

    def __init__(
        self,
        provider_type: ProviderType,
        base_url: str,
        api_key: str,
        model_name: str,
        context_window: int = 128_000,
        max_output_tokens: int = 8_192,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        temperature: float = 0.7,
        fallback: str | None = None,
    ) -> None:
        self.provider_type = provider_type
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.supports_thinking = supports_thinking
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.fallback = fallback

    # ── URL ────────────────────────────────────────────────

    def build_url(self) -> str:
        """构建 chat completions 端点 URL。

        OpenAI 兼容格式: {base_url}/chat/completions
        """
        return f"{self.base_url}/chat/completions"

    # ── Headers ────────────────────────────────────────────

    def build_headers(self) -> dict[str, str]:
        """构建请求头。

        - Authorization: Bearer {api_key}（OpenAI 兼容标准）
        - Content-Type: application/json
        - Accept: text/event-stream（声明 SSE）
        """
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    # ── Body ───────────────────────────────────────────────

    def build_body(
        self,
        messages: list[dict],
        *,
        stream: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建 OpenAI 兼容的请求体。

        Args:
            messages: 消息列表 [{role, content, ...}]
            stream: 是否流式（默认 True）
            temperature: 采样温度（缺省用 self.temperature）
            max_tokens: 最大输出 token 数（缺省用 self.max_output_tokens）
            tools: 工具列表 [{type:"function", function:{...}}]
            include_usage: 是否在流式末尾返回 usage（stream_options）
            extra: 额外字段（如 reasoning_effort）

        Returns:
            OpenAI 兼容的请求体 dict。
        """
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
            "temperature": temperature if temperature is not None else self.temperature,
        }

        # max_tokens 计算（对齐 Elixir streamer.ex:296-307）
        # - reasoning 模型: 用完整 max_output_tokens
        # - 非 reasoning 模型: min(max_output, 32000)
        global_cap = 32_000
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        elif self.supports_thinking and self.max_output_tokens > 0:
            body["max_tokens"] = self.max_output_tokens
        else:
            body["max_tokens"] = min(self.max_output_tokens or global_cap, global_cap)

        # 流式 usage（OpenAI stream_options.include_usage）
        if stream and include_usage:
            body["stream_options"] = {"include_usage": True}

        # reasoning_effort（仅 thinking 模型且显式配置时发送）
        if self.supports_thinking and self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        # 工具
        if tools:
            body["tools"] = tools

        # 额外字段（覆盖）
        if extra:
            body.update(extra)

        return body

    # ── HTTP Client ────────────────────────────────────────

    def build_client(self) -> httpx.AsyncClient:
        """创建带三层超时的 httpx.AsyncClient。

        超时配置:
        - connect=10s / read=120s / write=10s / pool=10s
        - 总超时 300s 通过 timeout 参数设置（httpx 的 timeout.total）

        注意: 调用方负责关闭 client（async with / await client.aclose()）。
        """
        timeout = build_timeout()
        # httpx 的 total 超时通过 timeout 参数的 5 元组无法直接设置，
        # 需要在 Timeout 对象上设置。但 httpx.Timeout 不支持 total 字段，
        # 我们用 read=120s 作为 idle 防线，配合 streamer 的 asyncio.wait_for
        # 实现 total=300s 兜底。
        return httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return (
            f"ProviderConfig(type={self.provider_type!r}, "
            f"model={self.model_name!r}, "
            f"base_url={self.base_url!r})"
        )


# ── ProviderFactory ─────────────────────────────────────────


class ProviderFactory:
    """Provider 工厂 — 从模型配置创建 ProviderConfig。

    用法::

        factory = ProviderFactory()
        config = factory.create(model_config_dict)
        # config 是 ProviderConfig 实例

    模型配置 dict 格式（来自 ModelService.get()）::
        {
            "id": "...",
            "name": "DeepSeek V4 Flash Free",
            "model_id": "deepseek-v4-flash-free",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key": "sk-...",
            "context_window": 200000,
            "max_output_tokens": 8192,
            "supports_thinking": False,
            "default_reasoning_effort": None,
            "temperature": 0.7,
        }
    """

    def create(self, model_config: dict) -> ProviderConfig:
        """从模型配置 dict 创建 ProviderConfig。

        Args:
            model_config: 模型配置（来自 ModelService.get()）

        Returns:
            ProviderConfig 实例

        Raises:
            ValueError: base_url 或 api_key 为空
        """
        base_url = (model_config.get("base_url") or "").strip()
        api_key = model_config.get("api_key") or ""
        model_name = model_config.get("model_id") or model_config.get("model") or ""

        if not base_url or not api_key:
            raise ValueError(
                f"Invalid model config: base_url={base_url!r}, "
                f"api_key={'***' if api_key else 'empty!r'}, "
                f"model_name={model_name!r}"
            )

        # 推断 provider type: 从 name/model_id/base_url 猜测
        provider_type = self._infer_provider_type(model_config)

        return ProviderConfig(
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            context_window=int(model_config.get("context_window") or 128_000),
            max_output_tokens=int(model_config.get("max_output_tokens") or 8_192),
            supports_thinking=bool(model_config.get("supports_thinking", False)),
            reasoning_effort=model_config.get("default_reasoning_effort"),
            temperature=float(model_config.get("temperature") or 0.7),
            fallback=model_config.get("fallback"),
        )

    # ── Provider type 推断 ─────────────────────────────────

    @staticmethod
    def _infer_provider_type(model_config: dict) -> ProviderType:
        """从模型配置推断 provider 类型。

        优先级:
        1. 显式 provider 字段
        2. base_url 域名匹配
        3. model_id 前缀匹配
        4. 默认 openai-compatible
        """
        # 1. 显式字段
        explicit = (model_config.get("provider") or "").lower().strip()
        if explicit in KNOWN_PROVIDERS:
            return explicit

        base_url = (model_config.get("base_url") or "").lower()
        model_id = (model_config.get("model_id") or "").lower()

        # 2. base_url 域名匹配
        if "api.openai.com" in base_url or "openai.com" in base_url:
            return "openai"
        if "api.anthropic.com" in base_url or "anthropic.com" in base_url:
            return "anthropic"
        if "generativelanguage.googleapis.com" in base_url or "googleapis.com" in base_url:
            return "google"

        # 3. model_id 前缀匹配
        if model_id.startswith(("gpt-", "o1", "o3", "o4", "text-davinci")):
            return "openai"
        if model_id.startswith(("claude-", "claude")):
            return "anthropic"
        if model_id.startswith(("gemini-", "gemini")):
            return "google"

        # 4. 默认 openai-compatible（DeepSeek, Groq, TogetherAI, ...）
        return "openai-compatible"

    # ── 便捷方法 ───────────────────────────────────────────

    def create_from_name(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model_name: str,
        **kwargs: Any,
    ) -> ProviderConfig:
        """直接从参数创建 ProviderConfig（不走 ModelService）。

        用于测试或硬编码 provider 场景。
        """
        provider_lower = provider.lower().strip()
        if provider_lower not in KNOWN_PROVIDERS:
            provider_lower = "openai-compatible"

        return ProviderConfig(
            provider_type=provider_lower,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            **kwargs,
        )


# ── 模块级单例 ──────────────────────────────────────────────

provider_factory = ProviderFactory()
"""全局 Provider 工厂单例。"""
