"""SSE parse / chunk conversion / tool-call merge."""
from __future__ import annotations

import json
import uuid
from typing import Any

def parse_sse(buffer: str) -> tuple[list[dict], str]:
    """解析 SSE 缓冲区，返回 (events, leftover)。

    SSE 格式: 事件之间用空行分隔（\\n\\n 或 \\r\\n\\r\\n），每个事件是
    data: {json} 的行。最后一段可能是不完整的，作为 leftover 返回供下次拼接。

    R1: 同时处理 \\r\\n\\r\\n 和 \\n\\n 分隔符 —— 某些代理/CDN（如 Cloudflare、
    Nginx 默认）会把 SSE 事件的空行分隔符规范化为 CRLF。先做 CRLF→LF 归一化，
    再按 \\n\\n 分割，兼容两种分隔符。

    对齐 Elixir parse_sse/1。
    """
    if not buffer:
        return [], ""

    # R1: 规范化 CRLF → LF，使 \r\n\r\n 成为 \n\n（兼容 CDN/代理的 CRLF 分隔）
    normalized = buffer.replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    # 最后一段可能不完整（无结尾 \n\n）
    *complete, leftover = parts

    events: list[dict] = []
    for part in complete:
        event = _extract_data(part)
        if event is not None:
            events.append(event)

    return events, leftover


def _extract_data(block: str) -> dict | None:
    """从 SSE 事件块提取 data + event 字段并解析 JSON。

    支持 OpenAI SSE（仅 data: 行）和 Anthropic SSE（event: + data: 行）。
    一个事件块可能有多行 data:（多行 JSON 拼接），对齐 Elixir extract_data/1。
    如果有 event: 行，存储为 _event_type 字段供 handler 使用。
    """
    if not block:
        return None

    data_parts: list[str] = []
    event_type: str | None = None
    for line in block.split("\n"):
        if line.startswith("data:"):
            value = line[5:]  # 去掉 "data:" 前缀
            data_parts.append(value.strip())
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        # 忽略 id:/retry: 等其他 SSE 字段

    if not data_parts:
        return None

    data_str = "".join(data_parts)
    if data_str == "[DONE]":
        return {"__done__": True}

    try:
        parsed = json.loads(data_str)
        if isinstance(parsed, dict):
            # Preserve SSE event type for Anthropic-style SSE
            if event_type and "type" not in parsed:
                parsed["_event_type"] = event_type
            return parsed
        return None
    except (json.JSONDecodeError, TypeError):
        return None


# ── SSE event → chunks 转换 ─────────────────────────────────


def sse_to_chunks(event: dict) -> list[dict]:
    """将单个 SSE event 转为 chunk 列表。

    一个 delta 可能同时携带 reasoning + text + tool_calls + finish_reason，
    我们逐字段提取，返回多个 chunk（对齐 Elixir sse_to_chunks/1）。

    chunk 类型:
    - {type:"text", content:str}
    - {type:"reasoning", content:str}
    - {type:"tool_call_delta", tool_call:{index, id, name, arguments}}
    - {type:"finish", reason:str}
    - {type:"error", content:str}
    """
    if event.get("__done__"):
        return []

    # 错误响应
    if "error" in event and isinstance(event["error"], dict):
        msg = event["error"].get("message") or str(event["error"])
        return [{"type": "error", "content": msg}]

    choices = event.get("choices")
    if not choices or not isinstance(choices, list):
        return []

    choice = choices[0]
    if not isinstance(choice, dict):
        return []

    delta = choice.get("delta") or {}
    finish_reason = choice.get("finish_reason")

    chunks: list[dict] = []

    # 1. Reasoning content — 检查所有已知字段名变体
    reasoning_text = _extract_reasoning(delta)
    if reasoning_text:
        chunks.append({"type": "reasoning", "content": reasoning_text})

    # 2. Text content — 支持 string 和 array-of-content-blocks 两种格式
    text_content = _extract_text_content(delta.get("content"))
    if text_content:
        chunks.append({"type": "text", "content": text_content})

    # 3. Tool calls — 支持 function 包装和 flat 两种格式
    tool_calls_raw = delta.get("tool_calls")
    if isinstance(tool_calls_raw, list) and tool_calls_raw:
        for tc in tool_calls_raw:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            arguments = fn.get("arguments") or tc.get("arguments") or ""
            chunks.append({
                "type": "tool_call_delta",
                "tool_call": {
                    "index": tc.get("index", 0),
                    "id": tc.get("id"),
                    "name": name,
                    "arguments": arguments,
                },
            })

    # 4. Finish reason（最后处理，不阻塞其他字段）
    if finish_reason is not None and finish_reason != "null":
        chunks.append({"type": "finish", "reason": finish_reason})

    return chunks


def _extract_reasoning(delta: dict) -> str | None:
    """提取 reasoning/thinking 内容（多字段名兼容）。"""
    for key in ("reasoning_content", "reasoning", "thinking", "thinking_content"):
        val = delta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_text_content(content: Any) -> str | None:
    """提取 text content，支持 string 和 array-of-blocks 格式。"""
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        # array of content blocks: [{"type":"text","text":"..."}]
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    texts.append(t)
        if texts:
            return "".join(texts)
    return None


# ── Tool calls 合并 ─────────────────────────────────────────


def merge_tool_calls(
    existing: list[dict],
    new_deltas: list[dict],
) -> list[dict]:
    """将流式 tool_call deltas 合并为完整的 tool_calls。

    流式返回的 tool_calls 是分片的: name 和 arguments 分多次到达。
    按 index 分组，拼接 name 和 arguments fragments。

    对齐 Elixir merge_tool_calls/2。

    Args:
        existing: 已合并的 tool_calls 列表
        new_deltas: 新的 delta 列表 [{index, id, name, arguments}]

    Returns:
        合并后的完整 tool_calls 列表
    """
    all_deltas = existing + new_deltas
    if not all_deltas:
        return []

    # 按 index 分组
    groups: dict[int, list[dict]] = {}
    for d in all_deltas:
        idx = d.get("index", 0)
        groups.setdefault(idx, []).append(d)

    result: list[dict] = []
    for idx in sorted(groups.keys()):
        deltas = groups[idx]
        # name fragments 按顺序拼接
        name = "".join(
            d["name"] for d in deltas if d.get("name")
        )
        # arguments fragments 按顺序拼接
        arguments = "".join(
            d["arguments"] for d in deltas if d.get("arguments")
        )
        # id 取第一个非空值
        call_id = next(
            (d["id"] for d in deltas if d.get("id")),
            str(uuid.uuid4()),
        )
        result.append({
            "id": call_id,
            "name": name,
            "arguments": arguments,
        })

    return result

