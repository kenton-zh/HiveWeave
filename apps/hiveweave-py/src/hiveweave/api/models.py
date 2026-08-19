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

import ipaddress
import json
import time
from urllib.parse import urlparse

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

#: 通用 /models 元数据探测超时
_MODELS_API_TIMEOUT = 10.0

#: Cloud metadata / internal hosts — never probe (SSRF).
_BLOCKED_PROBE_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "kubernetes.default",
    "kubernetes.default.svc",
})


def _normalize_models_probe_base(base_url: str) -> str:
    """Strip accidental transport suffixes before appending /models."""
    from hiveweave.llm.wire_endpoint import probe_base_url

    return probe_base_url(base_url)


def _probe_url_blocked_reason(base_url: str) -> str | None:
    """Return a human reason if ``base_url`` must not be probed, else None.

    Local-first: private/loopback hosts stay allowed (Ollama / LAN gateways).
    Blocks cloud metadata, link-local, credentialed URLs, and non-http(s).
    """
    raw = (base_url or "").strip()
    if not raw:
        return "missing baseUrl"
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except Exception:
        return "invalid URL"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r} (http/https only)"
    if parsed.username or parsed.password:
        return "URL must not embed credentials"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "missing host"
    if host in _BLOCKED_PROBE_HOSTS or host.endswith(".internal"):
        return f"blocked host {host}"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    # Literal IP: allow private/loopback for local gateways; block link-local
    # and the classic cloud-metadata address.
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return f"blocked address {host}"
    if str(ip) == "169.254.169.254":
        return "blocked metadata address"
    return None


async def _fetch_models_metadata(
    base_url: str, api_key: str, model_id: str
) -> dict | None:
    """通用能力探测：GET {base_url}/models 拉取 provider 模型元数据。

    对任意 OpenAI 兼容网关生效（OpenRouter / opencode.ai / ARK / 自建等），
    不做域名特判、不依赖任何预制数据。端点可达且 id 匹配时返回：
    {context_window, supports_thinking, max_output_tokens}（缺失字段为 None）；
    端点不可达 / 非 200 / 无匹配条目 → None（调用方把失败原因透传给用户）。
    """
    base = _normalize_models_probe_base(base_url)
    if not base or not model_id:
        return None
    blocked = _probe_url_blocked_reason(base)
    if blocked:
        log.warning(
            "models_metadata_probe_blocked",
            base=base,
            model_id=model_id,
            reason=blocked,
        )
        return None
    url = f"{base}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=_MODELS_API_TIMEOUT, follow_redirects=False
        ) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            log.warning(
                "models_metadata_probe_http_status",
                url=url,
                model_id=model_id,
                status=resp.status_code,
            )
            return None
        data = resp.json()
        if not isinstance(data, dict):
            log.warning(
                "models_metadata_probe_non_object",
                url=url,
                model_id=model_id,
            )
            return None
        models_list = data.get("data") or data.get("models") or []
    except Exception as e:
        log.warning(
            "models_metadata_probe_failed",
            url=url,
            model_id=model_id,
            error=str(e),
        )
        return None

    for m in models_list:
        if not isinstance(m, dict):
            continue
        m_id = str(m.get("id") or "")
        if m_id.lower() != model_id.lower():
            continue
        caps: dict = {
            "context_window": None,
            "supports_thinking": None,
            "max_output_tokens": None,
        }
        ctx = m.get("context_length") or m.get("context_window")
        if isinstance(ctx, int) and ctx > 0:
            caps["context_window"] = ctx
        arch = m.get("architecture") or {}
        if isinstance(arch, dict):
            modalities = list(arch.get("input_modalities") or []) + list(
                arch.get("output_modalities") or []
            )
            if any(str(x).lower() == "reasoning" for x in modalities):
                caps["supports_thinking"] = True
        # max_output 候选：顶层字段，或 OpenRouter 的 top_provider 子对象
        # （真实 schema 里 max_completion_tokens 只在 top_provider 下）。
        # 都是真实元数据（非预制），最终过 _sanitize_max_output 物理校验。
        max_tok = m.get("max_completion_tokens") or m.get("max_output_tokens")
        if max_tok is None:
            top_provider = m.get("top_provider") or {}
            if isinstance(top_provider, dict):
                max_tok = top_provider.get("max_completion_tokens")
        if isinstance(max_tok, int) and max_tok > 0:
            caps["max_output_tokens"] = max_tok
        return caps
    log.warning(
        "models_metadata_probe_model_missing",
        url=url,
        model_id=model_id,
    )
    return None


async def _detect_model_metadata(
    base_url: str, api_key: str, model_id: str
) -> dict:
    """通用能力探测入口：只做真实探测，无预制数据。

    成功（网关 /models 可达且模型条目命中）→ 真实元数据 + source="external-api"
    + error=None；失败 → 三项全 None + source="unknown" + error 说明原因，
    由前端/调用方明确告知用户，不静默填任何猜测值。
    """
    base = _normalize_models_probe_base(base_url)
    if not base or not model_id:
        return {
            "context_window": None,
            "supports_thinking": None,
            "max_output_tokens": None,
            "source": "unknown",
            "error": "baseUrl 与 modelId 必填",
        }
    blocked = _probe_url_blocked_reason(base)
    if blocked:
        return {
            "context_window": None,
            "supports_thinking": None,
            "max_output_tokens": None,
            "source": "unknown",
            "error": f"探测被拒绝：{blocked}。请使用合法的 http(s) 网关地址。",
        }
    caps = await _fetch_models_metadata(base, api_key, model_id)
    if caps is None:
        return {
            "context_window": None,
            "supports_thinking": None,
            "max_output_tokens": None,
            "source": "unknown",
            "error": (
                f"探测失败：GET {base}/models 不可达（非 200/超时/网络错误），"
                f"或未找到 id={model_id} 的条目。请手动填写能力字段，"
                "或用「测试」按钮做真实连通性验证。"
            ),
        }
    # 物理不变量校验（非预制数据）：max_output >= context_window → 脏数据丢弃
    if caps["max_output_tokens"] is not None:
        caps["max_output_tokens"] = _sanitize_max_output(
            caps["max_output_tokens"], caps["context_window"], model_id
        )
    caps["source"] = "external-api"
    caps["error"] = None
    return caps


#: max_output_tokens 合理性上界。语义：真实模型的 max_output 极少超过此值。
#: DeepSeek V4 Flash 等可到 ~384k；设 400k 作为「明显是 context_length 串线」的判据。
#: 仅当 context_window 未知（无法用物理不变量校验）时作为兜底判据使用；
#: context_window 已知时，判据是物理不变量 max_output < context_window。
_MAX_OUTPUT_SANITY_UPPER_BOUND = 400_000


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
        nested = data.get("response")
        if isinstance(nested, dict):
            usage = nested.get("usage") or {}
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}

    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens") or (input_tokens + output_tokens)

    # reasoning/thinking tokens (OpenRouter: completion_tokens_details;
    # Responses: output_tokens_details)
    reasoning_tokens = 0
    details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or {}
    )
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

    # SSRF: same gate as metadata probe — refuse link-local / metadata /
    # credentialed URLs before POSTing with the API key.
    probe_base = _normalize_models_probe_base(base_url)
    blocked = _probe_url_blocked_reason(probe_base or base_url)
    if blocked:
        return {
            "ok": False,
            "latencyMs": 0,
            "error": f"自检被拒绝：{blocked}。请使用合法的 http(s) 网关地址。",
        }

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
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT, follow_redirects=False
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        latency_ms = int((time.perf_counter() - start) * 1000)
        # 即使超时也检测 context_window 和 capabilities
        meta = await _detect_model_metadata(base_url, api_key, model_name)
        detected_ctx = meta.get("context_window")
        caps = meta
        result = {"ok": False, "latencyMs": latency_ms, "error": "request timed out"}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        result["detectedSupportsThinking"] = caps.get("supports_thinking")
        result["detectedMaxOutputTokens"] = caps.get("max_output_tokens")
        return result
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        meta = await _detect_model_metadata(base_url, api_key, model_name)
        detected_ctx = meta.get("context_window")
        caps = meta
        result = {"ok": False, "latencyMs": latency_ms, "error": str(e)}
        if detected_ctx is not None:
            result["detectedContextWindow"] = detected_ctx
        result["detectedSupportsThinking"] = caps.get("supports_thinking")
        result["detectedMaxOutputTokens"] = caps.get("max_output_tokens")
        return result

    # ── 顺序探测：真实 /models 元数据（context + caps 一次获取）──
    meta = await _detect_model_metadata(base_url, api_key, model_name)
    detected_ctx = meta.get("context_window")
    caps = meta

    result = {"ok": False, "latencyMs": latency_ms}

    # 解析响应
    response_text = ""
    usage_data = None
    runtime_detected_thinking = False  # 运行时检测到推理模型（thinking block 或 reasoning_tokens）
    if resp.status_code == 200:
        data = resp.json()
        # 提取 usage
        usage_data = _extract_usage_from_response(data)

        from hiveweave.llm.wire_endpoint import (
            extract_nonstream_text,
            is_responses_envelope,
        )

        response_text = extract_nonstream_text(data)
        if response_text:
            result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
        elif is_responses_envelope(data):
            result = {"ok": True, "latencyMs": latency_ms, "response": ""}
        else:
            choices = data.get("choices") or []
            if choices:
                result = {"ok": True, "latencyMs": latency_ms, "response": ""}
            else:
                content_blocks = data.get("content") or []
                if content_blocks:
                    found_text = False
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            response_text = block.get("text", "")
                            result = {"ok": True, "latencyMs": latency_ms, "response": response_text}
                            found_text = True
                            break
                    if not found_text:
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "thinking":
                                thinking_text = block.get("thinking", "")
                                result = {
                                    "ok": True,
                                    "latencyMs": latency_ms,
                                    "response": f"[thinking only] {thinking_text[:100]}",
                                }
                                runtime_detected_thinking = True
                                break
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
    providerType: str | None = None  # openai-compatible | openai-responses | anthropic | google
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    supportsThinking: bool | None = None
    thinkingFormat: str | None = None
    defaultReasoningEffort: str | None = None
    temperature: float | None = None
    isActive: bool | None = None
    tier: str | None = None  # management | executor (None = 未分类)


class ModelUpdate(BaseModel):
    """更新模型请求体（所有字段可选）。"""

    name: str | None = None
    modelId: str | None = None
    baseUrl: str | None = None
    apiKey: str | None = None
    providerType: str | None = None  # openai-compatible | openai-responses | anthropic | google
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    supportsThinking: bool | None = None
    thinkingFormat: str | None = None
    defaultReasoningEffort: str | None = None
    temperature: float | None = None
    isActive: bool | None = None
    tier: str | None = None  # management | executor (None = 未分类)


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
        "thinkingFormat": "thinking_format",
        "defaultReasoningEffort": "default_reasoning_effort",
        "isActive": "is_active",
    }
    out: dict = {}
    for k, v in data.items():
        out[mapping.get(k, k)] = v
    return out


def _mask_api_key(key: str | None) -> str | None:
    """HTTP 出口统一脱敏：有值则只露末 4 位，避免 list/get 行为不一致泄密。"""
    if key is None:
        return None
    if key == "":
        return ""
    if len(key) <= 4:
        return "****"
    return ("*" * (len(key) - 4)) + key[-4:]


def _model_response(model: dict) -> dict:
    """同时输出 snake_case 与 camelCase 字段（api_key 一律脱敏）。"""
    masked = _mask_api_key(model.get("api_key"))
    return {
        "id": model.get("id"),
        "name": model.get("name"),
        "model_id": model.get("model_id"),
        "modelId": model.get("model_id"),
        "base_url": model.get("base_url"),
        "baseUrl": model.get("base_url"),
        "api_key": masked,
        "apiKey": masked,
        "provider_type": model.get("provider_type"),
        "providerType": model.get("provider_type"),
        "context_window": model.get("context_window"),
        "contextWindow": model.get("context_window"),
        "max_output_tokens": model.get("max_output_tokens"),
        "maxOutputTokens": model.get("max_output_tokens"),
        "supports_thinking": model.get("supports_thinking"),
        "supportsThinking": model.get("supports_thinking"),
        "thinking_format": model.get("thinking_format"),
        "thinkingFormat": model.get("thinking_format"),
        "default_reasoning_effort": model.get("default_reasoning_effort"),
        "defaultReasoningEffort": model.get("default_reasoning_effort"),
        "temperature": model.get("temperature"),
        "is_active": model.get("is_active"),
        "isActive": model.get("is_active"),
        "tier": model.get("tier"),
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

    自动探测（仅真实探测，无预制数据）：能力字段有缺失时才探测 /models
    元数据，成功则填充；失败不报错，缺失字段由存储层落默认值
    （_DEFAULT_CONTEXT_WINDOW / _DEFAULT_MAX_OUTPUT，可后续手动修正）。

    创建后自动触发自检，一次请求完成连通性测试 + 自动修正配置。
    """
    try:
        attrs = _normalize_attrs(body)

        # ── 能力字段有缺失时才探测（真实 /models 元数据，无预制数据）──
        need_ctx = not attrs.get("context_window")
        need_thinking = "supports_thinking" not in attrs
        need_max_output = "max_output_tokens" not in attrs
        if need_ctx or need_thinking or need_max_output:
            meta = await _detect_model_metadata(
                attrs.get("base_url", ""),
                attrs.get("api_key", ""),
                attrs.get("model_id", ""),
            )
            if need_ctx and meta.get("context_window"):
                attrs["context_window"] = meta["context_window"]
                log.info(
                    "create_model_auto_detected",
                    model_id=attrs.get("model_id"),
                    context_window=meta["context_window"],
                )
            if need_thinking and meta.get("supports_thinking") is not None:
                attrs["supports_thinking"] = meta["supports_thinking"]
            if need_max_output and meta.get("max_output_tokens") is not None:
                attrs["max_output_tokens"] = meta["max_output_tokens"]

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


class DetectCapabilitiesRequest(BaseModel):
    """能力探测请求体 — 仅需连接信息，不要求已落库。"""

    baseUrl: str
    apiKey: str | None = None
    modelId: str


@router.post("/detect-capabilities")
async def detect_capabilities(body: DetectCapabilitiesRequest) -> dict:
    """探测模型能力（上下文窗口 / 推理支持 / 最大输出），不发真实对话、不落库。

    只做真实探测：GET {base_url}/models 拉取网关模型元数据，无预制数据。
    成功 → 真实值 + source="external-api"；失败 → 全 None + source="unknown"
    + error 说明原因（前端明确提示用户，不静默填充猜测值）。

    与 /{id}/test 的区别：test 会发起一次真实 chat completion 并自动修正
    DB 配置（含运行时推理 token 检测）；本端点仅查询元数据供前端「一键探测」。

    返回:
    - contextWindow: int | None
    - supportsThinking: bool | None
    - maxOutputTokens: int | None
    - source: str (external-api / unknown)
    - error: str | None（探测失败原因）
    """
    base_url = (body.baseUrl or "").strip()
    model_id = (body.modelId or "").strip()
    if not base_url or not model_id:
        raise HTTPException(status_code=400, detail="baseUrl and modelId are required")

    api_key = (body.apiKey or "").strip()
    meta = await _detect_model_metadata(base_url, api_key, model_id)

    return {
        "contextWindow": meta.get("context_window"),
        "supportsThinking": meta.get("supports_thinking"),
        "maxOutputTokens": meta.get("max_output_tokens"),
        "source": meta.get("source", "unknown"),
        "error": meta.get("error"),
    }
