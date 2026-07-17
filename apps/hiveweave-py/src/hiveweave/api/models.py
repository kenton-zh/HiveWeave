"""LLM model registry CRUD endpoints (contract 19, group 9).

契约 19: Extra — LLM Models — 模型注册 CRUD + 探测测试
- GET    /api/llm-models        列出所有模型（api_key 脱敏）
- POST   /api/llm-models        创建模型（自动检测 context_window）
- GET    /api/llm-models/{id}   查单个模型
- PATCH  /api/llm-models/{id}   更新模型
- PUT    /api/llm-models/{id}   同 PATCH
- DELETE /api/llm-models/{id}   删除模型
- POST   /api/llm-models/{id}/test  探测请求（15s 超时，返回 detectedContextWindow）
"""

from __future__ import annotations

import json
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import structlog

from hiveweave.services.model import ModelService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/llm-models", tags=["models"])

_model = ModelService()

#: 契约 19: LLM 探测超时 15s
_PROBE_TIMEOUT = 15.0

#: OpenRouter 模型列表 API（公开，无需认证）
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

#: 常见模型 context_window 预设（按 model_id 子串匹配）
#  按 token 数降序排列，第一个匹配的生效。
_KNOWN_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    # ── Anthropic / Claude ──
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-3-opus", 200_000),
    ("claude-3-sonnet", 200_000),
    ("claude-3-haiku", 200_000),
    ("claude-2", 100_000),
    ("longcat", 200_000),        # LongCat-2.0
    # ── OpenAI ──
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_385),
    ("o1", 200_000),
    ("o3", 200_000),
    # ── Google / Gemini ──
    ("gemini-1.5-pro", 2_000_000),
    ("gemini-1.5-flash", 1_000_000),
    ("gemini-2.0", 1_000_000),
    # ── Meta / Llama ──
    ("llama-3.1-405b", 128_000),
    ("llama-3.1-70b", 128_000),
    ("llama-3.1-8b", 128_000),
    ("llama-3-70b", 8_192),
    ("llama-3-8b", 8_192),
    # ── Mistral ──
    ("mistral-large", 128_000),
    ("mistral-medium", 32_000),
    ("mistral-small", 32_000),
    ("mixtral", 32_000),
    # ── DeepSeek ──
    ("deepseek-v3", 64_000),
    ("deepseek-r1", 64_000),
    ("deepseek-coder", 64_000),
    # ── Qwen / 通义千问 ──
    ("qwen2.5-72b", 128_000),
    ("qwen2.5-32b", 128_000),
    ("qwen2.5-14b", 128_000),
    ("qwen2.5-7b", 128_000),
    ("qwen2-72b", 128_000),
    # ── Tencent / Hunyuan ──
    ("hunyuan", 32_000),         # 混元标准版 32K
    ("hy3", 262_144),            # Hunyuan 3 (OpenRouter 实测 256K)
    # ── 其他 ──
    ("yi-34b", 4_000),
    ("yi-large", 16_000),
    ("glm-4", 128_000),
    ("glm-4-plus", 128_000),
    ("baichuan", 4_096),
    ("spark", 8_192),
    ("ernie-4", 8_192),
    ("ernie-bot", 8_192),
]


async def _detect_context_window(
    base_url: str, api_key: str, model_id: str
) -> int | None:
    """自动检测模型的 context_window。

    策略（按优先级）：
    1. OpenRouter: 调用 /api/v1/models 获取精确的 context_length
    2. 内置预设表: 按 model_id 子串匹配常见模型
    3. 返回 None（无法检测）
    """
    base_lower = (base_url or "").lower()

    # ── 策略 1: OpenRouter API ──
    if "openrouter.ai" in base_lower:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_OPENROUTER_MODELS_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    models_list = data.get("data") or data.get("models") or []
                    for m in models_list:
                        m_id = m.get("id", "")
                        # OpenRouter model_id 格式: "tencent/hy3:free"
                        if m_id == model_id or m_id.lower() == model_id.lower():
                            ctx = m.get("context_length")
                            if ctx and isinstance(ctx, int) and ctx > 0:
                                log.info(
                                    "context_window_detected",
                                    source="openrouter",
                                    model_id=model_id,
                                    context_window=ctx,
                                )
                                return ctx
                    log.warning(
                        "openrouter_model_not_found",
                        model_id=model_id,
                        total_models=len(models_list),
                    )
        except Exception as e:
            log.warning("openrouter_detection_failed", error=str(e))

    # ── 策略 2: 内置预设表 ──
    mid_lower = (model_id or "").lower()
    for pattern, ctx in _KNOWN_CONTEXT_WINDOWS:
        if pattern in mid_lower:
            log.info(
                "context_window_detected",
                source="preset",
                model_id=model_id,
                pattern=pattern,
                context_window=ctx,
            )
            return ctx

    return None


#: 推理模型预设表（按 model_id 子串匹配）
#  这些模型会产生 thinking/reasoning tokens，需要 supports_thinking=1
_REASONING_MODEL_PATTERNS: list[str] = [
    "o1", "o3", "o4",               # OpenAI reasoning 系列
    "claude-3-5-sonnet",            # Claude extended thinking
    "deepseek-r1",                  # DeepSeek reasoning
    "hy3",                          # Hunyuan 3 (推理模型)
    "qwen3",                        # Qwen 3 (推理模型)
    "gemini-2.5",                   # Gemini 2.5 (推理模型)
    "step-3",                       # Step 3.x (推理模型)
    "longcat",                      # LongCat (推理模型, Anthropic 格式 thinking block)
]


async def _detect_model_capabilities(
    base_url: str, api_key: str, model_id: str,
    context_window: int | None = None,
) -> dict:
    """自动检测模型的推理能力和最大输出 token 数。

    返回 dict:
    - supports_thinking: bool | None
    - max_output_tokens: int | None
    - source: str (preset / external-api / unknown)

    通用性设计：本函数对所有 provider 通用，不写死任何特定平台。
    - 预设表（人类已验证真值）优先于任何 API 返回值
    - 外部 API（OpenRouter 及未来其他 /models 端点）的返回值作为候选，
      必须通过 _sanitize_max_output 通用校验后才能采纳
    - 脏数据判据是物理不变量（max_output < context_window），不是魔数

    Args:
        context_window: 已检测到的上下文窗口（若有）。传入后用于脏数据校验，
            判据为 max_output >= context_window → 脏数据丢弃。若为 None，
            用合理上界兜底（见 _MAX_OUTPUT_SANITY_UPPER_BOUND 注释）。
    """
    result: dict = {"supports_thinking": None, "max_output_tokens": None, "source": "unknown"}
    base_lower = (base_url or "").lower()
    mid_lower = (model_id or "").lower()

    # ── 策略 1: 预设表（人类已验证真值，最高优先级）──
    # 预设值本身已合理，无需再过 sanitize（它是真值源，不是待校验候选）。
    # 为什么预设优先：外部 /models API 对部分模型（尤其免费模型）会返回
    # 不可靠的 max_completion_tokens（如把 context_length 串成它），
    # 盲信会产出物理不可能的配置。预设表是经过验证的真值，应先采纳。
    _PRESET_MAX_OUTPUT: list[tuple[str, int]] = [
        ("o1", 100_000), ("o3", 100_000),
        ("claude-3-5-sonnet", 8_192),
        ("deepseek-r1", 32_768),
        ("hy3", 32_000),
        ("qwen3", 32_000),
        ("gemini-2.5", 8_192),
        ("gpt-4o", 16_384),
        ("gpt-4-turbo", 4_096),
        ("longcat", 8_192),
    ]
    for pattern, max_tok in _PRESET_MAX_OUTPUT:
        if pattern in mid_lower:
            result["max_output_tokens"] = max_tok
            result["source"] = "preset"
            break

    # supports_thinking 预设兜底
    for pattern in _REASONING_MODEL_PATTERNS:
        if pattern in mid_lower:
            result["supports_thinking"] = True
            if result["source"] == "unknown":
                result["source"] = "preset"
            break

    # ── 策略 2: 外部 /models API（补充检测，不覆盖预设真值）──
    # 通用：任何提供 /models 端点的 provider 都可在此补充。
    # 当前实现 OpenRouter，未来接其他平台时在此扩展。
    # 所有外部值都是「候选」，必须通过 _sanitize_max_output 校验才能采纳。
    external_caps = await _fetch_caps_from_models_api(base_url, model_id)
    if external_caps is not None:
        # supports_thinking：architecture 信号可信，补充检测（不覆盖预设）
        if result["supports_thinking"] is None and external_caps.get("supports_thinking"):
            result["supports_thinking"] = external_caps["supports_thinking"]
        # max_output_tokens：仅预设未命中时采纳候选，且过通用 sanitize
        if result["max_output_tokens"] is None and external_caps.get("max_output_tokens") is not None:
            candidate = external_caps["max_output_tokens"]
            sanitized = _sanitize_max_output(candidate, context_window, model_id)
            if sanitized is not None:
                result["max_output_tokens"] = sanitized
                if result["source"] in ("unknown", "preset"):
                    result["source"] = "external-api"

        log.info(
            "model_capabilities_detected",
            source=result["source"],
            model_id=model_id,
            supports_thinking=result["supports_thinking"],
            max_output_tokens=result["max_output_tokens"],
        )

    return result


#: max_output_tokens 合理性上界。语义：真实模型的 max_output 极少超过此值。
#: 当前顶配是 o1/o3 的 100k。设 200k 作为「明显是 context_length 串线」的判据。
#: 仅当 context_window 未知（无法用物理不变量校验）时作为兜底判据使用；
#: context_window 已知时，判据是物理不变量 max_output < context_window。
_MAX_OUTPUT_SANITY_UPPER_BOUND = 200_000


def _sanitize_max_output(
    candidate: int,
    context_window: int | None,
    model_id: str,
) -> int | None:
    """通用脏数据校验：对所有 provider 的 max_output 候选值生效。

    判据是物理不变量，不是某个平台的特例：
    - 若 context_window 已知：candidate >= context_window → 脏数据（输出预算
      吃掉整个窗口，输入零空间，物理不可能），丢弃
    - 若 context_window 未知：candidate >= _MAX_OUTPUT_SANITY_UPPER_BOUND →
      疑似 context_length 串线，丢弃
    - 否则：采纳

    返回 None 表示脏数据应丢弃，调用方应保持 None（让存储层要求显式配置）。
    """
    if candidate is None or not isinstance(candidate, int) or candidate <= 0:
        return None

    if context_window is not None and candidate >= context_window:
        log.warning(
            "max_output_suspicious",
            model_id=model_id,
            candidate=candidate,
            context_window=context_window,
            reason=">= context_window, physically impossible, discarded",
        )
        return None

    if context_window is None and candidate >= _MAX_OUTPUT_SANITY_UPPER_BOUND:
        log.warning(
            "max_output_suspicious",
            model_id=model_id,
            candidate=candidate,
            reason=f">= {_MAX_OUTPUT_SANITY_UPPER_BOUND}, likely context_length leaked, discarded",
        )
        return None

    return candidate


async def _fetch_caps_from_models_api(
    base_url: str, model_id: str
) -> dict | None:
    """从 provider 的 /models 端点获取能力信息（supports_thinking, max_output）。

    通用性：当前实现 OpenRouter，未来接其他平台时在此扩展（如 OpenAI /v1/models、
    Anthropic /v1/models 等）。所有平台的返回值都是「候选」，由调用方过 sanitize。
    """
    base_lower = (base_url or "").lower()

    if "openrouter.ai" in base_lower:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_OPENROUTER_MODELS_URL)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                models_list = data.get("data") or data.get("models") or []
                for m in models_list:
                    m_id = m.get("id", "")
                    if m_id == model_id or m_id.lower() == model_id.lower():
                        caps: dict = {}
                        # supports_thinking：architecture.modality 含 "reasoning"
                        arch = m.get("architecture") or {}
                        if isinstance(arch, dict):
                            input_modal = arch.get("input_modalities") or []
                            output_modal = arch.get("output_modalities") or []
                            if isinstance(input_modal, list) and "reasoning" in [str(x).lower() for x in input_modal]:
                                caps["supports_thinking"] = True
                            elif isinstance(output_modal, list) and "reasoning" in [str(x).lower() for x in output_modal]:
                                caps["supports_thinking"] = True
                        # max_output_tokens 候选值（待 sanitize）
                        max_tokens = None
                        top_provider = m.get("top_provider") or {}
                        if top_provider and isinstance(top_provider, dict):
                            max_tokens = top_provider.get("max_completion_tokens")
                        if max_tokens is None:
                            max_tokens = m.get("max_completion_tokens")
                        if max_tokens and isinstance(max_tokens, int) and max_tokens > 0:
                            caps["max_output_tokens"] = max_tokens
                        return caps
        except Exception as e:
            log.warning("models_api_capability_detection_failed", provider="openrouter", error=str(e))

    # 未来扩展：其他 provider 的 /models 端点在此添加
    # if "api.openai.com" in base_lower: ...
    # if "api.anthropic.com" in base_lower: ...

    return None


def _extract_usage_from_response(data: dict) -> dict:
    """从 LLM 响应中提取 usage 信息。

    返回 dict:
    - input_tokens: int
    - output_tokens: int
    - reasoning_tokens: int (thinking tokens)
    - total_tokens: int
    """
    usage = data.get("usage") or {}
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}

    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens") or (input_tokens + output_tokens)

    # reasoning/thinking tokens (OpenRouter: completion_tokens_details.reasoning_tokens)
    reasoning_tokens = 0
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning_tokens = details.get("reasoning_tokens") or 0

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


async def _do_self_test(model: dict) -> dict:
    """统一自检函数：连通性测试 + 自动检测 + 自动修正 DB 配置。

    被 create_model 和 test_model 共用。
    返回完整的检测结果 dict。
    """
    model_pk = model.get("id", "")
    base_url = (model.get("base_url") or "").rstrip("/")
    api_key = model.get("api_key") or ""
    model_name = model.get("model_id") or ""
    configured_ctx = model.get("context_window") or 0
    configured_thinking = model.get("supports_thinking")
    configured_max_output = model.get("max_output_tokens") or 0

    from hiveweave.llm.provider import ProviderFactory, provider_factory

    factory: ProviderFactory = provider_factory
    try:
        config = factory.create(model)
    except ValueError as e:
        return {"ok": False, "latencyMs": 0, "error": str(e)}

    url = config.build_url()
    headers = config.build_headers()
    body = config.build_body(
        messages=[{"role": "user", "content": "Say 'OK' and nothing else."}],
        stream=False,
        max_tokens=256,  # 推理模型需要足够空间：thinking + 实际输出
        tools=None,
    )

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        # 即使超时也检测 context_window 和 capabilities
        detected_ctx = await _detect_context_window(base_url, api_key, model_name)
        caps = await _detect_model_capabilities(base_url, api_key, model_name, context_window=detected_ctx)
        result = {"ok": False, "latencyMs": latency_ms, "error": "request timed out"}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        result["detectedSupportsThinking"] = caps.get("supports_thinking")
        result["detectedMaxOutputTokens"] = caps.get("max_output_tokens")
        return result
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        detected_ctx = await _detect_context_window(base_url, api_key, model_name)
        caps = await _detect_model_capabilities(base_url, api_key, model_name, context_window=detected_ctx)
        result = {"ok": False, "latencyMs": latency_ms, "error": str(e)}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        result["detectedSupportsThinking"] = caps.get("supports_thinking")
        result["detectedMaxOutputTokens"] = caps.get("max_output_tokens")
        return result

    # ── 顺序检测：先 context_window，再 capabilities（用 ctx 真值做脏数据校验）──
    detected_ctx = await _detect_context_window(base_url, api_key, model_name)
    caps = await _detect_model_capabilities(base_url, api_key, model_name, context_window=detected_ctx)

    result = {"ok": False, "latencyMs": latency_ms}

    # 解析响应
    response_text = ""
    usage_data = None
    runtime_detected_thinking = False  # 运行时检测到推理模型（thinking block 或 reasoning_tokens）
    if resp.status_code == 200:
        data = resp.json()
        # 提取 usage
        usage_data = _extract_usage_from_response(data)

        # Try OpenAI format
        choices = data.get("choices") or []
        if choices:
            response_text = choices[0].get("message", {}).get("content", "")
            result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
        # Try Anthropic format
        else:
            content_blocks = data.get("content") or []
            if content_blocks:
                # 优先找 text block
                found_text = False
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text = block.get("text", "")
                        result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
                        found_text = True
                        break
                # 没有 text block 但有 thinking block → 推理模型，thinking 占满 token
                if not found_text:
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            thinking_text = block.get("thinking", "")
                            result = {
                                "ok": True,
                                "latencyMs": latency_ms,
                                "response": f"[thinking only] {thinking_text[:100]}",
                            }
                            # 响应中有 thinking block = 推理模型
                            runtime_detected_thinking = True
                            break
            # Try Gemini format
            else:
                candidates = data.get("candidates") or []
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts") or []
                    for part in parts:
                        if isinstance(part, dict) and "text" in part:
                            response_text = part["text"]
                            result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
                            break
                else:
                    result = {"ok": True, "latencyMs": latency_ms, "response": json.dumps(data, ensure_ascii=False)[:200]}
    else:
        result = {"ok": False, "latencyMs": latency_ms, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    # ── 附加检测结果 ──
    if detected_ctx is not None:
        result["detectedContextWindow"] = detected_ctx
        # context_window 异常警告
        if configured_ctx > 0:
            ratio = configured_ctx / detected_ctx
            if ratio > 2.0:
                result["contextWindowWarning"] = (
                    f"配置的 context_window ({configured_ctx:,}) 远大于"
                    f"检测到的实际值 ({detected_ctx:,})，"
                    f"可能导致上下文溢出。建议更新为 {detected_ctx:,}。"
                )
            elif ratio < 0.5:
                result["contextWindowWarning"] = (
                    f"配置的 context_window ({configured_ctx:,}) 远小于"
                    f"检测到的实际值 ({detected_ctx:,})，"
                    f"可能导致压缩过于频繁。建议更新为 {detected_ctx:,}。"
                )

    # ── 推理模型检测 ──
    detected_thinking = caps.get("supports_thinking")
    detected_max_output = caps.get("max_output_tokens")

    # 运行时推理检测（最权威）— 两个信号：reasoning_tokens > 0 或 Anthropic thinking block
    if usage_data and usage_data.get("reasoning_tokens", 0) > 0:
        detected_thinking = True
        result["reasoningTokens"] = usage_data["reasoning_tokens"]
    if runtime_detected_thinking:
        detected_thinking = True

    if detected_thinking is not None:
        result["detectedSupportsThinking"] = detected_thinking
        # 配置异常警告
        if configured_thinking is not None and configured_thinking != detected_thinking:
            result["thinkingWarning"] = (
                f"配置的 supports_thinking={configured_thinking} 与检测值"
                f"={detected_thinking} 不一致，已自动修正。"
            )

    if detected_max_output is not None:
        result["detectedMaxOutputTokens"] = detected_max_output
        if configured_max_output > 0 and configured_max_output < detected_max_output // 4:
            result["maxOutputWarning"] = (
                f"配置的 max_output_tokens ({configured_max_output:,}) 远小于"
                f"检测到的实际值 ({detected_max_output:,})，"
                f"可能导致推理模型输出不足。建议更新为 {detected_max_output:,}。"
            )

    # ── 自动修正 DB 配置 ──
    updates: dict = {}
    if detected_ctx is not None and (configured_ctx == 0 or configured_ctx != detected_ctx):
        # 仅在差异显著时更新
        if configured_ctx == 0 or abs(configured_ctx - detected_ctx) / max(detected_ctx, 1) > 0.1:
            updates["context_window"] = detected_ctx
    if detected_thinking is not None and configured_thinking != detected_thinking:
        updates["supports_thinking"] = detected_thinking
    if detected_max_output is not None and configured_max_output != detected_max_output:
        # 检测层已保证 detected_max_output 是真值（预设优先 + 外部 API 脏数据丢弃），
        # 配置值不一致就修正——包括 configured 过大（如历史脏数据 262144）和过小。
        updates["max_output_tokens"] = detected_max_output

    if updates:
        try:
            await _model.update(model_pk, updates)
            result["autoCorrected"] = updates
            log.info("model_auto_corrected", model_pk=model_pk, updates=updates)
        except Exception as e:
            log.warning("model_auto_correct_failed", model_pk=model_pk, error=str(e))

    return result


class ModelCreate(BaseModel):
    """创建模型请求体。"""

    name: str
    modelId: str | None = None
    baseUrl: str | None = None
    apiKey: str | None = None
    providerType: str | None = None  # openai/anthropic/google/openai-compatible
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    supportsThinking: bool | None = None
    defaultReasoningEffort: str | None = None
    temperature: float | None = None
    isActive: bool | None = None


class ModelUpdate(BaseModel):
    """更新模型请求体（所有字段可选）。"""

    name: str | None = None
    modelId: str | None = None
    baseUrl: str | None = None
    apiKey: str | None = None
    providerType: str | None = None  # openai/anthropic/google/openai-compatible
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    supportsThinking: bool | None = None
    defaultReasoningEffort: str | None = None
    temperature: float | None = None
    isActive: bool | None = None


def _normalize_attrs(body: BaseModel) -> dict:
    """将 camelCase 请求体转为 service 层期望的 snake_case dict。"""
    data = body.model_dump(exclude_none=True)
    mapping = {
        "modelId": "model_id",
        "baseUrl": "base_url",
        "apiKey": "api_key",
        "providerType": "provider_type",
        "contextWindow": "context_window",
        "maxOutputTokens": "max_output_tokens",
        "supportsThinking": "supports_thinking",
        "defaultReasoningEffort": "default_reasoning_effort",
        "isActive": "is_active",
    }
    out: dict = {}
    for k, v in data.items():
        out[mapping.get(k, k)] = v
    return out


def _model_response(model: dict) -> dict:
    """同时输出 snake_case 与 camelCase 字段。"""
    return {
        "id": model.get("id"),
        "name": model.get("name"),
        "model_id": model.get("model_id"),
        "modelId": model.get("model_id"),
        "base_url": model.get("base_url"),
        "baseUrl": model.get("base_url"),
        "api_key": model.get("api_key"),
        "apiKey": model.get("api_key"),
        "provider_type": model.get("provider_type"),
        "providerType": model.get("provider_type"),
        "context_window": model.get("context_window"),
        "contextWindow": model.get("context_window"),
        "max_output_tokens": model.get("max_output_tokens"),
        "maxOutputTokens": model.get("max_output_tokens"),
        "supports_thinking": model.get("supports_thinking"),
        "supportsThinking": model.get("supports_thinking"),
        "default_reasoning_effort": model.get("default_reasoning_effort"),
        "defaultReasoningEffort": model.get("default_reasoning_effort"),
        "temperature": model.get("temperature"),
        "is_active": model.get("is_active"),
        "isActive": model.get("is_active"),
        "created_at": model.get("created_at"),
        "createdAt": model.get("created_at"),
        "updated_at": model.get("updated_at"),
        "updatedAt": model.get("updated_at"),
    }


@router.get("")
async def list_models() -> dict:
    """列出所有模型（api_key 脱敏）。"""
    models = await _model.list_all()
    return {"models": [_model_response(m) for m in models]}


@router.post("")
async def create_model(body: ModelCreate) -> dict:
    """创建模型。

    自动检测并填充：
    - context_window（OpenRouter API / 预设表）
    - supports_thinking（推理模型预设表 / OpenRouter API）
    - max_output_tokens（OpenRouter API / 预设表）

    创建后自动触发自检，一次请求完成连通性测试 + 自动修正配置。
    """
    try:
        attrs = _normalize_attrs(body)

        # ── 自动检测 context_window ──
        if not attrs.get("context_window"):
            detected = await _detect_context_window(
                attrs.get("base_url", ""),
                attrs.get("api_key", ""),
                attrs.get("model_id", ""),
            )
            if detected:
                attrs["context_window"] = detected
                log.info(
                    "create_model_auto_detected",
                    model_id=attrs.get("model_id"),
                    context_window=detected,
                )

        # ── 自动检测 supports_thinking 和 max_output_tokens ──
        # 用 "key not in attrs" 而非 "not attrs.get(key)"，避免覆盖用户显式设的 False / 0
        if "supports_thinking" not in attrs or "max_output_tokens" not in attrs:
            caps = await _detect_model_capabilities(
                attrs.get("base_url", ""),
                attrs.get("api_key", ""),
                attrs.get("model_id", ""),
            )
            if "supports_thinking" not in attrs and caps.get("supports_thinking") is not None:
                attrs["supports_thinking"] = caps["supports_thinking"]
            if "max_output_tokens" not in attrs and caps.get("max_output_tokens") is not None:
                attrs["max_output_tokens"] = caps["max_output_tokens"]

        result = await _model.create(attrs)

        # ── 创建后自动触发自检（连通性 + 运行时推理 token 检测 + DB 修正）──
        created_model = await _model.get(result["id"])
        if created_model:
            try:
                test_result = await _do_self_test(created_model)
                log.info(
                    "create_model_self_test_done",
                    model_id=result["id"],
                    ok=test_result.get("ok"),
                    auto_corrected=test_result.get("autoCorrected"),
                )
            except Exception as e:
                log.warning("create_model_self_test_failed", error=str(e))
    except Exception as e:
        log.error("create_model_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create model")
    return {"ok": True, "id": result["id"]}


@router.get("/{model_id}")
async def get_model(model_id: str) -> dict:
    """查单个模型。"""
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return {"model": _model_response(model)}


async def _update_model(model_id: str, body: ModelUpdate) -> dict:
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    attrs = _normalize_attrs(body)
    try:
        await _model.update(model_id, attrs)
    except Exception as e:
        log.error("update_model_failed", model_id=model_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to update model")
    return {"ok": True}


@router.patch("/{model_id}")
async def patch_model(model_id: str, body: ModelUpdate) -> dict:
    """更新模型（PATCH）。"""
    return await _update_model(model_id, body)


@router.put("/{model_id}")
async def put_model(model_id: str, body: ModelUpdate) -> dict:
    """更新模型（PUT，同 PATCH）。"""
    return await _update_model(model_id, body)


@router.delete("/{model_id}")
async def delete_model(model_id: str) -> dict:
    """删除模型。"""
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    try:
        await _model.delete(model_id)
    except Exception as e:
        log.error("delete_model_failed", model_id=model_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to delete model")
    return {"ok": True}


@router.post("/{model_id}/test")
async def test_model(model_id: str) -> dict:
    """自检请求 — 连通性测试 + 自动检测 + 自动修正 DB 配置。

    契约 19 特别流程 5: 15s 超时，返回 {ok, latencyMs, response|error}。
    支持所有 provider 格式（OpenAI/Anthropic/Google/OpenAI-compatible）。

    额外返回：
    - detectedContextWindow: 自动检测的 context_window
    - detectedSupportsThinking: 是否推理模型
    - detectedMaxOutputTokens: 最大输出 token 数
    - reasoningTokens: 响应中的推理 token 数（运行时检测）
    - autoCorrected: 自动修正的配置项
    - contextWindowWarning / thinkingWarning / maxOutputWarning: 配置异常提示
    """
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    return await _do_self_test(model)
