"""Known-provider presets (data source: @earendil-works/pi-ai@0.82.1 builtin catalog).

Selecting a preset in the UI means the user only types an API key — base_url,
model list and capability flags all come from here. Values were extracted from
the pi-ai package (``dist/providers/<id>.js`` + ``dist/providers/data/<id>.json``),
NOT guessed. Do not hand-edit base_urls without re-checking upstream.

``api_format`` uses HiveWeave ``provider_type`` spellings (wire_endpoint.py).
``thinking_format`` uses llm/thinking.py dialect spellings; empty = auto.
"""

from __future__ import annotations

from typing import Any

# Wire formats
_FMT_CHAT = "openai-compatible"
_FMT_ANTHROPIC = "anthropic"

# Thinking dialects (llm/thinking.py)
_THINK_DEEPSEEK = "deepseek"  # thinking.type=enabled wire; also GLM/Z.AI spelling
_THINK_OPENAI = "openai-effort"

# ── Shared model lists (CN/Global twins serve the same catalog) ─────────────

# pi-ai 给 kimi-k2 系标 maxTokens == contextWindow（262144），过不了 service 层
# max_output < context_window 物理不变量（且 0 输入空间本就无意义）。钳到
# 131072——pi-ai 自家 kimi-k3（1M ctx）条目用的就是这个输出上限。
_KIMI_CLAMPED_MAX_OUTPUT = 131072

_KIMI_MODELS: list[dict[str, Any]] = [
    {"id": "kimi-k2-0711-preview", "name": "Kimi K2 0711", "context_window": 131072, "max_output_tokens": 16384, "reasoning": False, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2-0905-preview", "name": "Kimi K2 0905", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": False, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2-thinking", "name": "Kimi K2 Thinking", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2-thinking-turbo", "name": "Kimi K2 Thinking Turbo", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2-turbo-preview", "name": "Kimi K2 Turbo", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": False, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2.5", "name": "Kimi K2.5", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2.6", "name": "Kimi K2.6", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2.7-code", "name": "Kimi K2.7 Code", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k2.7-code-highspeed", "name": "Kimi K2.7 Code HighSpeed", "context_window": 262144, "max_output_tokens": _KIMI_CLAMPED_MAX_OUTPUT, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
    {"id": "kimi-k3", "name": "Kimi K3", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": _THINK_OPENAI},
]

_GLM_MODELS: list[dict[str, Any]] = [
    {"id": "glm-4.5-air", "name": "GLM-4.5-Air", "context_window": 131072, "max_output_tokens": 98304, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "glm-4.7", "name": "GLM-4.7", "context_window": 204800, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "glm-5-turbo", "name": "GLM-5-Turbo", "context_window": 200000, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "glm-5.1", "name": "GLM-5.1", "context_window": 200000, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "glm-5.2", "name": "GLM-5.2", "context_window": 1000000, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
    {"id": "glm-5v-turbo", "name": "GLM-5V-Turbo", "context_window": 200000, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
]

_MINIMAX_MODELS: list[dict[str, Any]] = [
    {"id": "MiniMax-M2.7", "name": "MiniMax-M2.7", "context_window": 204800, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": ""},
    {"id": "MiniMax-M2.7-highspeed", "name": "MiniMax-M2.7-highspeed", "context_window": 204800, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": ""},
    {"id": "MiniMax-M3", "name": "MiniMax-M3", "context_window": 1000000, "max_output_tokens": 128000, "reasoning": True, "vision": True, "thinking_format": ""},
]

# ── Preset registry ─────────────────────────────────────────────────────────

PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "api_format": _FMT_CHAT,
        "models": [
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "context_window": 1000000, "max_output_tokens": 384000, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "context_window": 1000000, "max_output_tokens": 384000, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
        ],
    },
    {
        "id": "moonshotai-cn",
        "name": "Kimi（国内）",
        "base_url": "https://api.moonshot.cn/v1",
        "api_format": _FMT_CHAT,
        "models": [dict(m) for m in _KIMI_MODELS],
    },
    {
        "id": "moonshotai",
        "name": "Kimi（国际）",
        "base_url": "https://api.moonshot.ai/v1",
        "api_format": _FMT_CHAT,
        "models": [dict(m) for m in _KIMI_MODELS],
    },
    {
        "id": "zai-coding-cn",
        "name": "智谱 BigModel（国内）",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "api_format": _FMT_CHAT,
        "models": [dict(m) for m in _GLM_MODELS],
    },
    {
        "id": "zai",
        "name": "Z.AI（智谱国际）",
        "base_url": "https://api.z.ai/api/coding/paas/v4",
        "api_format": _FMT_CHAT,
        "models": [dict(m) for m in _GLM_MODELS],
    },
    {
        "id": "xiaomi",
        "name": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_format": _FMT_CHAT,
        "models": [
            {"id": "mimo-v2-flash", "name": "MiMo-V2-Flash", "context_window": 262144, "max_output_tokens": 65536, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
            {"id": "mimo-v2-omni", "name": "MiMo-V2-Omni", "context_window": 262144, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
            {"id": "mimo-v2-pro", "name": "MiMo-V2-Pro", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
            {"id": "mimo-v2.5", "name": "MiMo-V2.5", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": _THINK_DEEPSEEK},
            {"id": "mimo-v2.5-pro", "name": "MiMo-V2.5-Pro", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
            {"id": "mimo-v2.5-pro-ultraspeed", "name": "MiMo-V2.5-Pro-UltraSpeed", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": False, "thinking_format": _THINK_DEEPSEEK},
        ],
    },
    {
        "id": "minimax-cn",
        "name": "MiniMax（国内）",
        "base_url": "https://api.minimaxi.com/anthropic",
        "api_format": _FMT_ANTHROPIC,
        "models": [dict(m) for m in _MINIMAX_MODELS],
    },
    {
        "id": "minimax",
        "name": "MiniMax（国际）",
        "base_url": "https://api.minimax.io/anthropic",
        "api_format": _FMT_ANTHROPIC,
        "models": [dict(m) for m in _MINIMAX_MODELS],
    },
    {
        "id": "kimi-coding",
        "name": "Kimi For Coding",
        "base_url": "https://api.kimi.com/coding",
        "api_format": _FMT_ANTHROPIC,
        "models": [
            {"id": "k3", "name": "Kimi K3", "context_window": 1048576, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": ""},
            {"id": "k3-256k", "name": "Kimi K3-256K", "context_window": 262144, "max_output_tokens": 131072, "reasoning": True, "vision": True, "thinking_format": ""},
            {"id": "kimi-for-coding", "name": "Kimi K2.7 Code", "context_window": 262144, "max_output_tokens": 32768, "reasoning": True, "vision": True, "thinking_format": ""},
            {"id": "kimi-for-coding-highspeed", "name": "Kimi For Coding HighSpeed", "context_window": 262144, "max_output_tokens": 32768, "reasoning": True, "vision": True, "thinking_format": ""},
        ],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_format": _FMT_CHAT,
        # Hundreds of upstream models — no builtin list; form falls back to
        # hand-typed model id + the detect-capabilities probe.
        "models": [],
    },
]

_BY_ID: dict[str, dict[str, Any]] = {p["id"]: p for p in PRESETS}


def get_preset(preset_id: str) -> dict[str, Any] | None:
    """Return one preset by id, or None."""
    return _BY_ID.get((preset_id or "").strip())


def list_presets() -> list[dict[str, Any]]:
    """All presets in display order (custom-model entry is a frontend concern)."""
    return PRESETS
