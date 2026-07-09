"""Provider factory — multi-format LLM provider support.

契约 01: LLM 流式调用 — Provider 工厂
- openai → /chat/completions + Bearer auth + SSE (data: lines)
- anthropic → /v1/messages + x-api-key + SSE (event: + data: lines)
- google → /v1beta/models/{model}:streamGenerateContent + x-goog-api-key + SSE
- openai-compatible → same as openai (DeepSeek, Groq, TogetherAI, ...)

Architecture:
- FormatHandler (ABC) — one per API format; owns URL/headers/body/SSE parsing
- ProviderConfig — wraps a FormatHandler + model-specific settings
- ProviderFactory — creates ProviderConfig from model DB records with auto-detection

Inspired by OpenCode's Protocol/Endpoint/Auth/Framing separation
(apps/opencode/packages/llm/src/route/).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── API Format Enum ───────────────────────────────────────────


class ApiFormat(str, Enum):
    """Supported LLM API formats."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENAI_COMPATIBLE = "openai-compatible"


# ── Provider type (legacy, mapped to ApiFormat) ────────────────
ProviderType = str  # "openai" | "anthropic" | "google" | "openai-compatible"


def _api_format_to_provider_type(fmt: ApiFormat) -> ProviderType:
    """Map ApiFormat to legacy ProviderType string."""
    return fmt.value


# ── Timeout constants (contract 01) ────────────────────────────
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 95.0   # <= FIRST_CHUNK_TIMEOUT_S (90s) + buffer; lets httpx native timeout fire first
TOTAL_TIMEOUT_S = 300.0
WRITE_TIMEOUT_S = 10.0
POOL_TIMEOUT_S = 10.0


def build_timeout() -> httpx.Timeout:
    """Build three-tier timeout config."""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_S,
        read=READ_TIMEOUT_S,
        write=WRITE_TIMEOUT_S,
        pool=POOL_TIMEOUT_S,
    )


# ── Format Handler (Strategy Pattern) ──────────────────────────


class FormatHandler(ABC):
    """Abstract handler for a provider's native API format.

    Each concrete class implements the HTTP-level protocol for one
    API family (OpenAI Chat, Anthropic Messages, Google Gemini).
    Provider-level customizations (headers, fetch wrapping, model
    selection) are separate from format-level protocol differences.

    Inspired by OpenCode's Protocol abstraction.
    """

    @abstractmethod
    def build_url(self, base_url: str, model_id: str) -> str:
        """Build the full endpoint URL for a streaming request."""
        ...

    @abstractmethod
    def build_headers(self, api_key: str, model_config: dict | None = None) -> dict[str, str]:
        """Build HTTP request headers including auth."""
        ...

    @abstractmethod
    def build_body(
        self,
        messages: list[dict],
        model_id: str,
        *,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        supports_prompt_cache: bool = False,
    ) -> dict[str, Any]:
        """Build the provider-native request body.

        supports_prompt_cache: 若 True，handler 可在请求体中添加缓存断点
        （如 Anthropic 的 cache_control: {type: ephemeral}）。
        """
        ...

    @abstractmethod
    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        """Parse a single SSE data JSON object into canonical chunks.

        Returns a list of dicts with keys like:
          {type: "text", content: str}
          {type: "reasoning", content: str}
          {type: "tool_call_delta", tool_call: {index, id, name, arguments}}
          {type: "finish", reason: str}
          {type: "error", content: str}
        """
        ...

    def normalize_tools(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI-format tools to provider-native format.

        Default: pass-through (OpenAI-compatible).
        """
        return tools

    @staticmethod
    def extract_usage(chunk: dict) -> dict | None:
        """Extract usage/token counts from a chunk. Returns None if not present."""
        return None

    @staticmethod
    def map_finish_reason(reason: str) -> str:
        """Map provider-specific finish reason → canonical."""
        return reason

    def get_default_headers(self) -> dict[str, str]:
        """Provider-specific default headers (e.g., anthropic-version)."""
        return {}

    def get_timeout(self) -> httpx.Timeout:
        """Get per-format HTTP timeout config."""
        return build_timeout()


# ── OpenAI Chat Handler ────────────────────────────────────────


class OpenAIHandler(FormatHandler):
    """OpenAI /chat/completions format.

    Used by: OpenAI, and (via OpenAICompatibleHandler) DeepSeek,
    Groq, TogetherAI, Cerebras, Fireworks, DeepInfra, xAI, etc.
    """

    def build_url(self, base_url: str, model_id: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def build_headers(self, api_key: str, model_config: dict | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def build_body(
        self,
        messages: list[dict],
        model_id: str,
        *,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        supports_prompt_cache: bool = False,
    ) -> dict[str, Any]:
        # OpenAI 用隐式 prefix caching，不接受 inline cache_control markers。
        # supports_prompt_cache 参数在此为 no-op，仅为统一接口签名。
        body: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
        }

        # max_tokens: reasoning models use full cap, others capped at 32k
        global_cap = 32_000
        if supports_thinking and max_tokens > 0:
            body["max_tokens"] = max_tokens
        else:
            body["max_tokens"] = min(max_tokens or global_cap, global_cap)

        if stream and include_usage:
            body["stream_options"] = {"include_usage": True}

        if supports_thinking and reasoning_effort:
            body["reasoning_effort"] = reasoning_effort

        if tools:
            body["tools"] = tools

        if extra:
            body.update(extra)

        return body

    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        """Parse OpenAI-format SSE chunk into canonical chunks."""
        # Check for __done__ sentinel (set by parse_sse)
        if raw_json.get("__done__"):
            return []

        # Error response
        if "error" in raw_json and isinstance(raw_json["error"], dict):
            msg = raw_json["error"].get("message") or str(raw_json["error"])
            return [{"type": "error", "content": msg}]

        choices = raw_json.get("choices")
        if not choices or not isinstance(choices, list):
            return []

        choice = choices[0]
        if not isinstance(choice, dict):
            return []

        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        chunks: list[dict] = []

        # Reasoning content
        reasoning_text = _extract_reasoning(delta)
        if reasoning_text:
            chunks.append({"type": "reasoning", "content": reasoning_text})

        # Text content
        text_content = _extract_text_content(delta.get("content"))
        if text_content:
            chunks.append({"type": "text", "content": text_content})

        # Tool calls
        tool_calls_raw = delta.get("tool_calls")
        if isinstance(tool_calls_raw, list) and tool_calls_raw:
            for tc in tool_calls_raw:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                chunks.append({
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": tc.get("index", 0),
                        "id": tc.get("id"),
                        "name": fn.get("name") or tc.get("name"),
                        "arguments": fn.get("arguments") or tc.get("arguments") or "",
                    },
                })

        # Finish reason
        if finish_reason is not None and finish_reason != "null":
            chunks.append({"type": "finish", "reason": finish_reason})

        return chunks

    @staticmethod
    def extract_usage(chunk: dict) -> dict | None:
        u = chunk.get("usage")
        if not u:
            return None
        return {
            "input": u.get("prompt_tokens", 0),
            "output": u.get("completion_tokens", 0),
            "total": u.get(
                "total_tokens",
                u.get("prompt_tokens", 0) + u.get("completion_tokens", 0),
            ),
        }


# ── Anthropic Messages Handler ─────────────────────────────────


class AnthropicHandler(FormatHandler):
    """Anthropic /v1/messages format.

    Key differences from OpenAI:
    - Endpoint: POST /v1/messages (not /chat/completions)
    - Auth: x-api-key header (not Bearer)
    - Body: system as top-level array, messages with content blocks,
      tools with input_schema (not function.parameters)
    - SSE: Named event types (event: message_start, event: content_block_delta, ...)
    - Finish reasons: end_turn/stop_sequence → stop, max_tokens → length,
      tool_use → tool-calls
    """

    def build_url(self, base_url: str, model_id: str) -> str:
        base = base_url.rstrip("/")
        # If base_url already ends with /v1, append /messages
        if base.endswith("/v1"):
            return f"{base}/messages"
        # If base_url is like https://api.anthropic.com, append /v1/messages
        return f"{base}/v1/messages"

    def build_headers(self, api_key: str, model_config: dict | None = None) -> dict[str, str]:
        # LongCat and most Anthropic-compatible proxies use Bearer auth.
        # Official Anthropic API uses x-api-key; we include both for max compatibility.
        return {
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def get_default_headers(self) -> dict[str, str]:
        """Anthropic-specific default headers (applied on top of auth headers)."""
        return {
            "anthropic-version": "2023-06-01",
        }

    def build_body(
        self,
        messages: list[dict],
        model_id: str,
        *,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        supports_prompt_cache: bool = False,
    ) -> dict[str, Any]:
        """Build Anthropic-format request body.

        Converts OpenAI-format messages to Anthropic content blocks:
        - system messages → top-level "system" array
        - user messages → role: "user" with content blocks
        - assistant messages → role: "assistant" with content blocks + tool_use
        - tool messages → role: "user" with tool_result blocks

        Prompt caching (supports_prompt_cache=True):
        参考 opencode cache-policy.ts 的 auto 策略，在 3 个位置注入
        cache_control: {type: "ephemeral"} 断点：
        1. 最后一个 tool 定义（缓存 tools schema）
        2. 最后一个 system block（缓存 system prompt）
        3. 最后一条 user 消息的最后一个 text block（缓存到当前轮次前缀）

        Anthropic 最多 4 个断点，3 个 auto 断点在安全范围内。
        tool loop 中前缀不变，每轮都能命中缓存，显著降低 token 成本和延迟。
        """
        system_blocks: list[dict] = []
        anthropic_messages: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_blocks.append({"type": "text", "text": str(content)})
            elif role == "user":
                blocks = self._user_content_blocks(msg)
                anthropic_messages.append({"role": "user", "content": blocks})
            elif role == "assistant":
                blocks = self._assistant_content_blocks(msg)
                anthropic_messages.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                # Tool results → role: "user" with tool_result block
                tool_call_id = msg.get("tool_call_id", "")
                result_content = str(content)
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": result_content,
                    }],
                })

        # Prompt cache 断点注入（参考 opencode cache-policy.ts auto 策略）
        # 仅标记 system_blocks 和 anthropic_messages；tools 在 normalize_tools 后单独标记
        if supports_prompt_cache:
            self._apply_cache_breakpoints(system_blocks, anthropic_messages)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": anthropic_messages,
            "stream": stream,
            "max_tokens": max_tokens,
        }

        if system_blocks:
            body["system"] = system_blocks

        if temperature is not None:
            body["temperature"] = temperature

        if tools:
            body["tools"] = self.normalize_tools(tools)
            # 标记最后一个 tool 的 cache_control（缓存 tools schema）
            if supports_prompt_cache and body["tools"]:
                body["tools"][-1]["cache_control"] = {"type": "ephemeral"}

        # Thinking support
        if supports_thinking and reasoning_effort:
            thinking_budget = 16_000  # default
            body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

        if extra:
            body.update(extra)

        return body

    def _user_content_blocks(self, msg: dict) -> list[dict]:
        """Build Anthropic content blocks for a user message."""
        content = msg.get("content", "")
        blocks: list[dict] = []

        # Images (if present)
        images = msg.get("images") or []
        for img in images:
            if isinstance(img, dict):
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.get("media_type", "image/png"),
                        "data": img.get("data", ""),
                    },
                })

        # Text content
        if content:
            blocks.append({"type": "text", "text": str(content)})

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks

    def _assistant_content_blocks(self, msg: dict) -> list[dict]:
        """Build Anthropic content blocks for an assistant message."""
        blocks: list[dict] = []
        content = msg.get("content", "")

        # Text content
        if content:
            blocks.append({"type": "text", "text": str(content)})

        # Reasoning/thinking (if present)
        reasoning = msg.get("reasoning_content") or msg.get("thinking")
        if reasoning:
            blocks.append({"type": "thinking", "thinking": str(reasoning)})

        # Tool calls
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", tc.get("name", "")),
                "input": _parse_json_args(fn.get("arguments", tc.get("arguments", "{}"))),
            })

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks

    @staticmethod
    def _apply_cache_breakpoints(
        system_blocks: list[dict],
        anthropic_messages: list[dict],
    ) -> None:
        """注入 prompt cache 断点（参考 opencode cache-policy.ts auto 策略）。

        在 2 个位置标记 cache_control: {type: "ephemeral"}：
        1. 最后一个 system block — 缓存 system prompt（身份/上下文/技能摘要）
        2. 最后一条 user 消息的最后一个 text block — 缓存到当前轮次前缀

        tools 的断点在 build_body 中 normalize_tools 后单独标记（第 3 个断点）。
        共 3 个断点，在 Anthropic 4 断点上限内。

        设计依据：HiveWeave tool loop 中，每轮 LLM 请求的前缀（system +
        history + 最新 user）不变，只有尾部追加 assistant+tool_result。
        缓存这个前缀让每轮都命中 cache_read，大幅降低 token 成本和延迟。
        """
        # 断点 1: 最后一个 system block
        if system_blocks:
            last_system = system_blocks[-1]
            if "cache_control" not in last_system:
                last_system["cache_control"] = {"type": "ephemeral"}

        # 断点 2: 最后一条 user 消息的最后一个 text block
        # 逆序找最后一条 user 消息
        for msg in reversed(anthropic_messages):
            if msg.get("role") != "user":
                continue
            blocks = msg.get("content")
            if not isinstance(blocks, list) or not blocks:
                break
            # 优先标记最后一个 text block，没有则标记最后一个 block
            text_idx = -1
            for i in range(len(blocks) - 1, -1, -1):
                if isinstance(blocks[i], dict) and blocks[i].get("type") == "text":
                    text_idx = i
                    break
            mark_idx = text_idx if text_idx >= 0 else len(blocks) - 1
            target = blocks[mark_idx]
            if isinstance(target, dict) and "cache_control" not in target:
                target["cache_control"] = {"type": "ephemeral"}
            break  # 只标记最后一条 user 消息

    def normalize_tools(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Anthropic tool format.

        OpenAI: {type: "function", function: {name, description, parameters}}
        Anthropic: {name, description, input_schema}
        """
        result = []
        for tool in tools:
            fn = tool.get("function") or tool
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        """Parse Anthropic-format SSE event into canonical chunks.

        Anthropic SSE uses typed events:
        - message_start → extract initial usage
        - content_block_start → detect block type (text/tool_use/thinking)
        - content_block_delta → extract text_delta/thinking_delta/input_json_delta
        - content_block_stop → end of content block
        - message_delta → extract stop_reason + final usage
        - message_stop → stream complete
        - error → error event
        """
        event_type = raw_json.get("type", "")
        chunks: list[dict] = []

        if event_type == "message_start":
            # Initial usage estimate
            msg = raw_json.get("message", {})
            u = msg.get("usage", {})
            if u:
                chunks.append({
                    "type": "usage",
                    "usage": {
                        "input": u.get("input_tokens", 0),
                        "output": u.get("output_tokens", 0),
                        "total": (u.get("input_tokens", 0) + u.get("output_tokens", 0)),
                    },
                })

        elif event_type == "content_block_start":
            block = raw_json.get("content_block", {})
            block_type = block.get("type", "")
            idx = raw_json.get("index", 0)

            if block_type == "tool_use":
                chunks.append({
                    "type": "tool_call_start",
                    "tool_call": {
                        "index": idx,
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": "",
                    },
                })
            elif block_type == "thinking":
                chunks.append({
                    "type": "thinking_start",
                    "index": idx,
                })
            elif block_type == "server_tool_use":
                chunks.append({
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": idx,
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif block_type == "text":
                # text block start — nothing to emit, deltas will follow
                pass

        elif event_type == "content_block_delta":
            delta = raw_json.get("delta", {})
            delta_type = delta.get("type", "")
            idx = raw_json.get("index", 0)

            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    chunks.append({"type": "text", "content": text})
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    chunks.append({"type": "reasoning", "content": thinking})
            elif delta_type == "signature_delta":
                sig = delta.get("signature", "")
                chunks.append({"type": "thinking_signature", "content": sig})
            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json", "")
                if partial:
                    chunks.append({
                        "type": "tool_call_delta",
                        "tool_call": {
                            "index": idx,
                            "id": None,
                            "name": None,
                            "arguments": partial,
                        },
                    })

        elif event_type == "content_block_stop":
            idx = raw_json.get("index", 0)
            chunks.append({
                "type": "tool_call_end",
                "index": idx,
            })

        elif event_type == "message_delta":
            delta = raw_json.get("delta", {})
            stop_reason = delta.get("stop_reason", "")
            chunks.append({
                "type": "finish",
                "reason": self.map_finish_reason(stop_reason),
            })
            u = raw_json.get("usage", {})
            if u:
                chunks.append({
                    "type": "usage",
                    "usage": {
                        "input": u.get("input_tokens", 0),
                        "output": u.get("output_tokens", 0),
                        "total": (u.get("input_tokens", 0) + u.get("output_tokens", 0)),
                    },
                })

        elif event_type == "message_stop":
            chunks.append({"type": "message_stop"})

        elif event_type == "error":
            err = raw_json.get("error", {})
            chunks.append({
                "type": "error",
                "content": f"{err.get('type', 'unknown')}: {err.get('message', str(err))}",
            })

        elif event_type == "ping":
            # Anthropic keepalive — ignore
            pass

        return chunks

    @staticmethod
    def extract_usage(chunk: dict) -> dict | None:
        """提取 usage，含 prompt cache 命中统计。

        Anthropic 在 usage 中返回 cache 字段：
        - cache_creation_input_tokens: 写入缓存的 token 数（计费 1.25x）
        - cache_read_input_tokens: 命中缓存的 token 数（计费 0.1x）
        参考 opencode anthropic-messages.ts mapUsage。
        """
        u = chunk.get("usage")
        if u:
            return {
                "input": u.get("input_tokens", 0),
                "output": u.get("output_tokens", 0),
                "total": (u.get("input_tokens", 0) + u.get("output_tokens", 0)),
                "cache_creation": u.get("cache_creation_input_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
            }
        # Also check message-level usage
        msg = chunk.get("message", {})
        u2 = msg.get("usage")
        if u2:
            return {
                "input": u2.get("input_tokens", 0),
                "output": u2.get("output_tokens", 0),
                "total": (u2.get("input_tokens", 0) + u2.get("output_tokens", 0)),
                "cache_creation": u2.get("cache_creation_input_tokens", 0),
                "cache_read": u2.get("cache_read_input_tokens", 0),
            }
        return None

    @staticmethod
    def map_finish_reason(reason: str) -> str:
        mapping = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "pause_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "refusal": "content_filter",
        }
        return mapping.get(reason, reason)


# ── Google Gemini Handler ──────────────────────────────────────


class GoogleHandler(FormatHandler):
    """Google Gemini generateContent format.

    Key differences from OpenAI:
    - Endpoint: POST /v1beta/models/{model}:streamGenerateContent?alt=sse
    - Auth: x-goog-api-key header (or ?key= query param for AI Studio)
    - Body: contents[] with parts[], systemInstruction separate, generationConfig
    - SSE: Standard data: lines (like OpenAI) but no [DONE] sentinel
    - Roles: "user" and "model" (not "assistant")
    - Tool calls: functionCall/functionResponse in parts
    - Reasoning: thought: true flag on text parts
    """

    def build_url(self, base_url: str, model_id: str) -> str:
        base = base_url.rstrip("/")
        return f"{base}/models/{model_id}:streamGenerateContent?alt=sse"

    def build_headers(self, api_key: str, model_config: dict | None = None) -> dict[str, str]:
        return {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def build_body(
        self,
        messages: list[dict],
        model_id: str,
        *,
        stream: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        supports_prompt_cache: bool = False,
    ) -> dict[str, Any]:
        """Build Google Gemini-format request body.

        Converts OpenAI-format messages to Gemini contents + systemInstruction.
        Gemini 用隐式缓存 + out-of-band CachedContent，不接受 inline cache markers，
        supports_prompt_cache 参数在此为 no-op。
        """
        contents: list[dict] = []
        system_instruction: dict | None = None

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = {
                    "parts": [{"text": str(content)}],
                }
            elif role == "user":
                parts = self._user_parts(msg)
                contents.append({"role": "user", "parts": parts})
            elif role == "assistant":
                parts = self._model_parts(msg)
                contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                # Tool results → functionResponse in user turn
                tool_call_id = msg.get("tool_call_id", "")
                # Find tool name from previous tool calls
                tool_name = "unknown"
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": {"content": str(content)},
                        },
                    }],
                })

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }

        if system_instruction:
            body["systemInstruction"] = system_instruction

        if tools:
            body["tools"] = self.normalize_tools(tools)

        # Thinking config
        if supports_thinking:
            body["generationConfig"]["thinkingConfig"] = {"includeThoughts": True}

        if extra:
            body.update(extra)

        return body

    def _user_parts(self, msg: dict) -> list[dict]:
        """Build Gemini parts for a user message."""
        content = msg.get("content", "")
        parts: list[dict] = []

        # Images
        images = msg.get("images") or []
        for img in images:
            if isinstance(img, dict):
                parts.append({
                    "inlineData": {
                        "mimeType": img.get("media_type", "image/png"),
                        "data": img.get("data", ""),
                    },
                })

        if content:
            parts.append({"text": str(content)})

        if not parts:
            parts.append({"text": ""})
        return parts

    def _model_parts(self, msg: dict) -> list[dict]:
        """Build Gemini parts for a model (assistant) message."""
        parts: list[dict] = []
        content = msg.get("content", "")

        # Text content
        if content:
            parts.append({"text": str(content)})

        # Tool calls → functionCall parts
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args = _parse_json_args(fn.get("arguments", tc.get("arguments", "{}")))
            parts.append({
                "functionCall": {
                    "name": fn.get("name", tc.get("name", "")),
                    "args": args,
                },
            })

        if not parts:
            parts.append({"text": ""})
        return parts

    def normalize_tools(self, tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Google Gemini format.

        OpenAI: {type: "function", function: {name, description, parameters}}
        Google: {functionDeclarations: [{name, description, parameters}]}
        """
        declarations = []
        for tool in tools:
            fn = tool.get("function") or tool
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return [{"functionDeclarations": declarations}]

    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        """Parse Google Gemini-format SSE chunk into canonical chunks.

        Gemini returns SSE data: lines like OpenAI, but with different JSON shape:
        {candidates: [{content: {parts: [{text, thought, functionCall}], role}, finishReason, ...}], usageMetadata: {...}}
        """
        chunks: list[dict] = []

        # Error check
        if "error" in raw_json:
            err = raw_json["error"]
            msg = err.get("message", str(err))
            return [{"type": "error", "content": msg}]

        candidates = raw_json.get("candidates") or []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            role = content.get("role", "model")

            for part in parts:
                if not isinstance(part, dict):
                    continue

                # Text (may have thought flag)
                text = part.get("text", "")
                if text:
                    if part.get("thought"):
                        chunks.append({"type": "reasoning", "content": text})
                    else:
                        chunks.append({"type": "text", "content": text})

                # Function call
                fn_call = part.get("functionCall")
                if fn_call:
                    chunks.append({
                        "type": "tool_call_delta",
                        "tool_call": {
                            "index": 0,
                            "id": fn_call.get("name", ""),  # Gemini doesn't have tool_call_id
                            "name": fn_call.get("name", ""),
                            "arguments": json.dumps(fn_call.get("args", {}), ensure_ascii=False),
                        },
                    })

            finish_reason = candidate.get("finishReason")
            if finish_reason:
                chunks.append({
                    "type": "finish",
                    "reason": self.map_finish_reason(finish_reason),
                })

        # Usage
        usage = raw_json.get("usageMetadata")
        if usage:
            chunks.append({
                "type": "usage",
                "usage": {
                    "input": usage.get("promptTokenCount", 0),
                    "output": usage.get("candidatesTokenCount", 0),
                    "total": usage.get("totalTokenCount", 0),
                    "thoughts": usage.get("thoughtsTokenCount", 0),
                    "cached": usage.get("cachedContentTokenCount", 0),
                },
            })

        return chunks

    @staticmethod
    def extract_usage(chunk: dict) -> dict | None:
        u = chunk.get("usageMetadata")
        if not u:
            return None
        return {
            "input": u.get("promptTokenCount", 0),
            "output": u.get("candidatesTokenCount", 0),
            "total": u.get("totalTokenCount", 0),
        }

    @staticmethod
    def map_finish_reason(reason: str) -> str:
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
            "IMAGE_SAFETY": "content_filter",
            "BLOCKLIST": "content_filter",
            "PROHIBITED_CONTENT": "content_filter",
            "SPII": "content_filter",
            "MALFORMED_FUNCTION_CALL": "error",
        }
        return mapping.get(reason, reason)


# ── OpenAI-Compatible Handler ──────────────────────────────────


class OpenAICompatibleHandler(OpenAIHandler):
    """OpenAI-compatible format (DeepSeek, Groq, TogetherAI, etc.).

    Same as OpenAI Chat format — no changes needed.
    """

    pass


# ── Format Handler Registry ────────────────────────────────────

FORMAT_HANDLERS: dict[ApiFormat, FormatHandler] = {
    ApiFormat.OPENAI: OpenAIHandler(),
    ApiFormat.ANTHROPIC: AnthropicHandler(),
    ApiFormat.GOOGLE: GoogleHandler(),
    ApiFormat.OPENAI_COMPATIBLE: OpenAICompatibleHandler(),
}


# ── ProviderConfig ─────────────────────────────────────────────


class ProviderConfig:
    """Provider configuration wrapping a format handler + model settings.

    Encapsulates base_url, api_key, model_name + format-specific handler.
    """

    def __init__(
        self,
        api_format: ApiFormat,
        base_url: str,
        api_key: str,
        model_name: str,
        context_window: int = 128_000,
        max_output_tokens: int = 8_192,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        temperature: float = 0.7,
        fallback: str | None = None,
        handler: FormatHandler | None = None,
        extra_headers: dict[str, str] | None = None,
        supports_prompt_cache: bool = False,
    ) -> None:
        self.api_format = api_format
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.supports_thinking = supports_thinking
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.fallback = fallback
        self._handler = handler or FORMAT_HANDLERS.get(api_format, OpenAICompatibleHandler())
        self._extra_headers = extra_headers or {}
        # Prompt cache 支持：仅 Anthropic 格式有效（OpenAI/Gemini 用隐式缓存）
        # 参考 opencode RESPECTS_INLINE_HINTS = {"anthropic-messages", "bedrock-converse"}
        self.supports_prompt_cache = supports_prompt_cache and api_format == ApiFormat.ANTHROPIC

    @property
    def provider_type(self) -> str:
        return self.api_format.value

    @property
    def handler(self) -> FormatHandler:
        return self._handler

    # ── Delegated methods ──────────────────────────────────────

    def build_url(self) -> str:
        return self._handler.build_url(self.base_url, self.model_name)

    def build_headers(self) -> dict[str, str]:
        headers = self._handler.build_headers(self.api_key)
        # Merge in default format headers (e.g., anthropic-version)
        defaults = self._handler.get_default_headers()
        for k, v in defaults.items():
            if k not in headers:
                headers[k] = v
        # Merge in extra headers from model config
        headers.update(self._extra_headers)
        return headers

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
        return self._handler.build_body(
            messages=messages,
            model_id=self.model_name,
            stream=stream,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_output_tokens,
            tools=tools,
            include_usage=include_usage,
            extra=extra,
            supports_thinking=self.supports_thinking,
            reasoning_effort=self.reasoning_effort,
            supports_prompt_cache=self.supports_prompt_cache,
        )

    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        """Parse a raw SSE JSON object into canonical chunks using the format handler."""
        return self._handler.parse_stream_chunk(raw_json)

    def extract_usage(self, chunk: dict) -> dict | None:
        """Extract usage (含 cache 命中统计) from a chunk using the format handler."""
        return self._handler.extract_usage(chunk)

    def build_client(self) -> httpx.AsyncClient:
        timeout = self._handler.get_timeout()
        return httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return (
            f"ProviderConfig(format={self.api_format.value!r}, "
            f"model={self.model_name!r}, "
            f"base_url={self.base_url!r})"
        )


# ── ProviderFactory ────────────────────────────────────────────


class ProviderFactory:
    """Factory to create ProviderConfig from model DB records.

    Auto-detects API format from base_url + model_id patterns
    (inspired by OpenCode's provider type inference).
    """

    def create(self, model_config: dict) -> ProviderConfig:
        """Create a ProviderConfig from a model DB record.

        Args:
            model_config: dict from ModelService.get() with keys:
                id, name, model_id, base_url, api_key, context_window,
                max_output_tokens, supports_thinking, default_reasoning_effort,
                temperature, provider_type (optional override)

        Returns:
            ProviderConfig instance

        Raises:
            ValueError: base_url or api_key is empty
        """
        base_url = (model_config.get("base_url") or "").strip()
        api_key = model_config.get("api_key") or ""
        model_name = model_config.get("model_id") or model_config.get("model") or ""

        if not base_url or not api_key:
            raise ValueError(
                f"Invalid model config: base_url={base_url!r}, "
                f"api_key={'***' if api_key else 'empty'}, "
                f"model_name={model_name!r}"
            )

        # Detect format: explicit provider_type takes priority, then auto-detect
        api_format = self._detect_format(model_config)

        # Prompt cache: Anthropic 格式默认开启（5min cache 写 1.25x，读 0.1x，
        # 单次复用即回本）。可被 model_config.supports_prompt_cache 显式关闭。
        # 参考 opencode: undefined → auto（默认开启）
        if "supports_prompt_cache" in model_config:
            supports_cache = bool(model_config["supports_prompt_cache"])
        else:
            supports_cache = api_format == ApiFormat.ANTHROPIC

        return ProviderConfig(
            api_format=api_format,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            context_window=int(model_config.get("context_window") or 128_000),
            max_output_tokens=int(model_config.get("max_output_tokens") or 8_192),
            supports_thinking=bool(model_config.get("supports_thinking", False)),
            reasoning_effort=model_config.get("default_reasoning_effort"),
            temperature=float(model_config.get("temperature") or 0.7),
            fallback=model_config.get("fallback"),
            supports_prompt_cache=supports_cache,
        )

    def _detect_format(self, model_config: dict) -> ApiFormat:
        """Auto-detect API format from model config.

        Priority:
        1. Explicit provider_type field
        2. base_url domain patterns
        3. model_id prefix patterns
        4. Default: openai-compatible
        """
        # 1. Explicit field
        explicit = (model_config.get("provider_type") or model_config.get("provider") or "").lower().strip()
        if explicit:
            try:
                return ApiFormat(explicit)
            except ValueError:
                pass

        base_url = (model_config.get("base_url") or "").lower()
        model_id = (model_config.get("model_id") or "").lower()

        # 2. base_url domain patterns
        if "api.anthropic.com" in base_url or "anthropic.com/v1/messages" in base_url:
            return ApiFormat.ANTHROPIC
        if "api.longcat.chat/anthropic" in base_url:
            return ApiFormat.ANTHROPIC
        if "openrouter.ai/api/v1" in base_url and (
            "claude" in model_id or "anthropic" in model_id
        ):
            return ApiFormat.ANTHROPIC

        if "generativelanguage.googleapis.com" in base_url:
            return ApiFormat.GOOGLE
        if "aiplatform.googleapis.com" in base_url:
            return ApiFormat.GOOGLE
        if "openrouter.ai/api/v1" in base_url and "gemini" in model_id:
            return ApiFormat.GOOGLE

        if "api.openai.com" in base_url:
            return ApiFormat.OPENAI

        # 3. model_id prefix patterns
        if model_id.startswith("claude-"):
            return ApiFormat.ANTHROPIC
        if model_id.startswith("gemini-"):
            return ApiFormat.GOOGLE
        if model_id.startswith(("gpt-", "o1", "o3", "o4")):
            return ApiFormat.OPENAI

        # 4. Default: OpenAI-compatible (DeepSeek, Groq, TogetherAI, ...)
        return ApiFormat.OPENAI_COMPATIBLE

    def create_from_name(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model_name: str,
        **kwargs: Any,
    ) -> ProviderConfig:
        """Create ProviderConfig directly from parameters (for tests)."""
        try:
            api_format = ApiFormat(provider.lower().strip())
        except ValueError:
            api_format = ApiFormat.OPENAI_COMPATIBLE

        return ProviderConfig(
            api_format=api_format,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            **kwargs,
        )


# ── Module-level singleton ─────────────────────────────────────

provider_factory = ProviderFactory()


# ── SSE parsing helpers (reused across handlers) ───────────────


def _extract_reasoning(delta: dict) -> str | None:
    """Extract reasoning/thinking content from delta (multi-field-name compatible)."""
    for key in ("reasoning_content", "reasoning", "thinking", "thinking_content"):
        val = delta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_text_content(content: Any) -> str | None:
    """Extract text content, supporting string and array-of-blocks formats."""
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    texts.append(t)
        if texts:
            return "".join(texts)
    return None


def _parse_json_args(args: Any) -> dict:
    """Parse JSON arguments, handling both string and dict inputs."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        import json as _json
        try:
            return _json.loads(args)
        except (_json.JSONDecodeError, TypeError):
            return {}
    return {}
