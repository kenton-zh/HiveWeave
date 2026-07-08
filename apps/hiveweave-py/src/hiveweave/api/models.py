"""LLM model registry CRUD endpoints (contract 19, group 9).

契约 19: Extra — LLM Models — 模型注册 CRUD + 探测测试
- GET    /api/llm-models        列出所有模型（api_key 脱敏）
- POST   /api/llm-models        创建模型
- GET    /api/llm-models/{id}   查单个模型
- PATCH  /api/llm-models/{id}   更新模型
- PUT    /api/llm-models/{id}   同 PATCH
- DELETE /api/llm-models/{id}   删除模型
- POST   /api/llm-models/{id}/test  探测请求（15s 超时）
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
    """创建模型。"""
    try:
        attrs = _normalize_attrs(body)
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
    """探测请求 — 使用多格式 handler 测延迟。

    契约 19 特别流程 5: 15s 超时，返回 {ok, latencyMs, response|error}。
    支持所有 provider 格式（OpenAI/Anthropic/Google/OpenAI-compatible）。
    """
    model = await _model.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    base_url = (model.get("base_url") or "").rstrip("/")
    api_key = model.get("api_key") or ""
    model_name = model.get("model_id") or ""

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
        if resp.status_code == 200:
            data = resp.json()
            # Try OpenAI format first
            choices = data.get("choices") or []
            if choices:
                response_text = choices[0].get("message", {}).get("content", "")
                return {"ok": True, "latencyMs": latency_ms, "response": response_text}
            # Try Anthropic format
            content_blocks = data.get("content") or []
            if content_blocks:
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return {"ok": True, "latencyMs": latency_ms, "response": block.get("text", "")}
            # Try Gemini format
            candidates = data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or []
                for part in parts:
                    if isinstance(part, dict) and "text" in part:
                        return {"ok": True, "latencyMs": latency_ms, "response": part["text"]}
            # Generic — return raw JSON preview
            return {"ok": True, "latencyMs": latency_ms, "response": json.dumps(data, ensure_ascii=False)[:200]}
        return {"ok": False, "latencyMs": latency_ms, "error": f"HTTP {resp.status_code}"}
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"ok": False, "latencyMs": latency_ms, "error": "request timed out"}
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"ok": False, "latencyMs": latency_ms, "error": str(e)}
