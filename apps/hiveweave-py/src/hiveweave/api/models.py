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
    ("hy3", 32_000),             # Hunyuan 3 简写
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

    如果用户未提供 contextWindow，自动检测：
    1. OpenRouter 模型 → 查询 OpenRouter /api/v1/models
    2. 其他模型 → 内置预设表匹配
    检测到的值会写入数据库并返回给用户。
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

        result = await _model.create(attrs)
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
    """探测请求 — 使用多格式 handler 测延迟 + 检测 context_window。

    契约 19 特别流程 5: 15s 超时，返回 {ok, latencyMs, response|error}。
    支持所有 provider 格式（OpenAI/Anthropic/Google/OpenAI-compatible）。
    额外返回 detectedContextWindow（自动检测值）+ contextWindowWarning（配置异常提示）。
    """
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    base_url = (model.get("base_url") or "").rstrip("/")
    api_key = model.get("api_key") or ""
    model_name = model.get("model_id") or ""
    configured_ctx = model.get("context_window") or 0

    # Use format handler to build correct URL/headers/body per provider
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
        max_tokens=10,
        tools=None,
    )

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=body,
                headers=headers,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)

        # 检测 context_window（探测请求后执行，不阻塞主路径）
        detected_ctx = await _detect_context_window(base_url, api_key, model_name)

        # 构建响应
        result: dict = {"ok": False, "latencyMs": latency_ms}

        if resp.status_code == 200:
            data = resp.json()
            # Try OpenAI format first
            choices = data.get("choices") or []
            if choices:
                response_text = choices[0].get("message", {}).get("content", "")
                result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
            # Try Anthropic format
            else:
                content_blocks = data.get("content") or []
                if content_blocks:
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            result = {"ok": True, "latencyMs": latency_ms, "response": block.get("text", "")}
                            break
                # Try Gemini format
                else:
                    candidates = data.get("candidates") or []
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts") or []
                        for part in parts:
                            if isinstance(part, dict) and "text" in part:
                                result = {"ok": True, "latencyMs": latency_ms, "response": part["text"]}
                                break
                    else:
                        result = {"ok": True, "latencyMs": latency_ms, "response": json.dumps(data, ensure_ascii=False)[:200]}
        else:
            result = {"ok": False, "latencyMs": latency_ms, "error": f"HTTP {resp.status_code}"}

        # ── 附加 context_window 检测结果 ──
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
            # 如果用户配置的值与检测值差异超过 2 倍，发出警告
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

        return result
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        detected_ctx = await _detect_context_window(base_url, api_key, model_name)
        result: dict = {"ok": False, "latencyMs": latency_ms, "error": "request timed out"}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        return result
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        detected_ctx = await _detect_context_window(base_url, api_key, model_name)
        result: dict = {"ok": False, "latencyMs": latency_ms, "error": str(e)}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        return result
