"""Tool-definition builders for LLM calls.

Extracted from agent.py — behavior-preserving mechanical split (P1).
"""

from __future__ import annotations

import hashlib


def _short_hash(data: str) -> str:
    """Short SHA256 hash for tool args/results dedup identification."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


# PermissionService 返回工具名列表；_build_tool_definitions 经
# get_tool_schema_for_llm / get_tool_description 取 schema：
# 优先 TOOL_PARAM_SCHEMAS，否则回退 @tool 注册表（防空 schema）。


def _build_tool_definitions(tool_names: list[str]) -> list[dict]:
    """将工具名列表转为 LLM 工具定义。

    Schema 来自 executor.get_tool_schema_for_llm：优先手写 TOOL_PARAM_SCHEMAS，
    否则回退 @tool 注册表的 Pydantic 模型（避免 waive_attestation 等空 schema）。
    """
    tools: list[dict] = []
    for name in tool_names:
        from hiveweave.tools.executor import (
            get_tool_description,
            get_tool_schema_for_llm,
        )

        params = get_tool_schema_for_llm(name)
        desc = get_tool_description(name)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": params,
                },
            }
        )
    return tools
