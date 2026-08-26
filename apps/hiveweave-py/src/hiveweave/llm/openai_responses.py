"""OpenAI Responses API handler (POST /v1/responses).

Protocol is first-class (`provider_type=openai-responses`). Base URL is the
gateway prefix (stop at /v1). Leftover `/responses` on old rows is a request-
time belt; model-id allowlists are not the authority.
"""

from __future__ import annotations

import json
from typing import Any

from hiveweave.llm.provider import FORMAT_HANDLERS, ApiFormat, FormatHandler, OpenAIHandler
from hiveweave.llm.thinking import (
    apply_responses_thinking,
    resolve_effort,
    resolve_thinking_format,
    thinking_enabled,
)
from hiveweave.llm.wire_endpoint import (
    looks_like_responses_endpoint,
    split_wire_endpoint,
)

_MAX_OUTPUT_HARD_CAP = 128_000
_DEFAULT_OUTPUT_CAP = 32_000


def rewrite_to_responses_url(base_url: str) -> str:
    """Idempotent: strip any known transport suffix, then append /responses."""
    prefix, _ = split_wire_endpoint(base_url)
    if not prefix:
        return (base_url or "").strip().rstrip("/") or "/responses"
    return f"{prefix}/responses"


class OpenAIResponsesHandler(FormatHandler):
    """OpenAI Responses API: `input` + SSE `response.*` events."""

    def build_url(self, base_url: str, model_id: str) -> str:
        return rewrite_to_responses_url(base_url)

    def build_headers(
        self, api_key: str, model_config: dict | None = None
    ) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def normalize_tools(self, tools: list[dict]) -> list[dict]:
        out: list[dict] = []
        for t in tools or []:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                item: dict[str, Any] = {
                    "type": "function",
                    "name": fn.get("name") or "",
                    "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or {},
                }
                if fn.get("strict") is not None:
                    item["strict"] = fn["strict"]
                out.append(item)
            else:
                out.append(t)
        return out

    def build_body(
        self,
        messages: list[dict],
        model_id: str,
        *,
        stream: bool = True,
        temperature: float = 0.7,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int = 8192,
        tools: list[dict] | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        supports_thinking: bool = False,
        reasoning_effort: str | None = None,
        thinking_format: str | None = None,
        supports_prompt_cache: bool = False,
        supports_images: bool = True,
    ) -> dict[str, Any]:
        del include_usage, supports_prompt_cache, top_k  # usage arrives on completed;
        # top_k: Responses API 无此参数（统一签名收下，不发）
        normalized = OpenAIHandler._normalize_messages_with_images(
            messages, supports_images=supports_images
        )
        fmt = resolve_thinking_format(
            thinking_format,
            supports_thinking=supports_thinking,
            protocol="openai-responses",
        )
        effort = resolve_effort(reasoning_effort, fmt)
        body: dict[str, Any] = {
            "model": model_id,
            "input": _messages_to_input(normalized),
            "stream": stream,
            "store": False,
        }
        if top_p is not None and not thinking_enabled(fmt):
            # 推理方言激活时不发采样参数（o 系对 top_p 修改直接 400，
            # 与 temperature 的 apply_responses_thinking 策略一致）
            body["top_p"] = top_p
        if thinking_enabled(fmt) and max_tokens > 0:
            body["max_output_tokens"] = min(max_tokens, _MAX_OUTPUT_HARD_CAP)
        else:
            body["max_output_tokens"] = min(
                max_tokens or _DEFAULT_OUTPUT_CAP, _DEFAULT_OUTPUT_CAP
            )
        apply_responses_thinking(
            body, fmt, effort, temperature,
            max_tokens=int(body["max_output_tokens"]),
        )
        if tools:
            body["tools"] = self.normalize_tools(tools)
        if extra:
            body.update(extra)
        return body

    def parse_stream_chunk(self, raw_json: dict) -> list[dict]:
        if raw_json.get("__done__"):
            return []
        err = _error_content(raw_json)
        if err:
            return [{"type": "error", "content": err}]

        typ = str(raw_json.get("type") or raw_json.get("_event_type") or "")
        if typ in (
            "response.output_text.delta",
            "response.content_part.delta",
        ):
            text = _delta_text(raw_json)
            return [{"type": "text", "content": text}] if text else []
        if typ in (
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        ):
            text = _delta_text(raw_json)
            return [{"type": "reasoning", "content": text}] if text else []
        if typ == "response.output_item.added":
            return _tool_delta_from_item(
                raw_json.get("item"),
                index=raw_json.get("output_index", 0),
            )
        if typ == "response.function_call_arguments.delta":
            delta = raw_json.get("delta")
            if not isinstance(delta, str) or not delta:
                return []
            return [{
                "type": "tool_call_delta",
                "tool_call": {
                    "index": raw_json.get("output_index", 0),
                    "id": None,
                    "name": None,
                    "arguments": delta,
                },
            }]
        if typ in ("response.completed", "response.incomplete"):
            response = raw_json.get("response") or {}
            status = response.get("status") if isinstance(response, dict) else None
            reason = "length" if (
                typ == "response.incomplete" or status == "incomplete"
            ) else "stop"
            output = response.get("output") if isinstance(response, dict) else None
            if _output_has_function_call(output):
                reason = "tool_calls"
            return [{"type": "finish", "reason": reason}]
        if typ == "response.failed":
            return [{"type": "error", "content": _error_content(raw_json) or typ}]

        # Non-stream JSON object (some gateways emit one payload).
        if raw_json.get("object") == "response" and isinstance(
            raw_json.get("output"), list
        ):
            return _chunks_from_complete_response(raw_json)
        return []

    @staticmethod
    def extract_usage(chunk: dict) -> dict | None:
        u = chunk.get("usage")
        if not u and isinstance(chunk.get("response"), dict):
            u = chunk["response"].get("usage")
        if not isinstance(u, dict):
            return None
        from hiveweave.llm.util import usage_int

        out: dict = {}
        if "input_tokens" in u:
            out["input"] = usage_int(u.get("input_tokens"))
        elif "prompt_tokens" in u:
            out["input"] = usage_int(u.get("prompt_tokens"))
        if "output_tokens" in u:
            out["output"] = usage_int(u.get("output_tokens"))
        elif "completion_tokens" in u:
            out["output"] = usage_int(u.get("completion_tokens"))
        if "input" in out or "output" in out:
            out["total"] = out.get("input", 0) + out.get("output", 0)
        details = u.get("input_tokens_details") or u.get("prompt_tokens_details")
        cached = None
        if isinstance(details, dict) and "cached_tokens" in details:
            cached = usage_int(details.get("cached_tokens"))
        elif "cache_read" in u:
            cached = usage_int(u.get("cache_read"))
        if cached is not None:
            out["cache_read"] = cached
            out["prompt_cache_hit_tokens"] = cached
        return out or None


def _delta_text(raw_json: dict) -> str:
    delta = raw_json.get("delta")
    if isinstance(delta, str) and delta:
        return delta
    part = raw_json.get("part")
    if isinstance(part, dict):
        text = part.get("text")
        if isinstance(text, str) and text:
            return text
    return ""


def _error_content(raw_json: dict) -> str | None:
    err = raw_json.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str) and err:
        return err
    response = raw_json.get("response")
    if isinstance(response, dict):
        nested = response.get("error")
        if isinstance(nested, dict):
            return str(nested.get("message") or nested)
        if isinstance(nested, str) and nested:
            return nested
    return None


def _tool_delta_from_item(item: Any, *, index: int) -> list[dict]:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return []
    call_id = item.get("call_id") or item.get("id")
    name = item.get("name") or ""
    arguments = item.get("arguments") or ""
    return [{
        "type": "tool_call_delta",
        "tool_call": {
            "index": index,
            "id": call_id,
            "name": name or None,
            "arguments": arguments if isinstance(arguments, str) else "",
        },
    }]


def _output_has_function_call(output: Any) -> bool:
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in output
    )


def _chunks_from_complete_response(raw_json: dict) -> list[dict]:
    chunks: list[dict] = []
    has_fn = False
    for idx, item in enumerate(raw_json.get("output") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            has_fn = True
            chunks.extend(_tool_delta_from_item(item, index=idx))
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text:
                continue
            ptype = part.get("type")
            if ptype in ("output_text", "text"):
                chunks.append({"type": "text", "content": text})
            elif ptype in ("reasoning", "reasoning_text"):
                chunks.append({"type": "reasoning", "content": text})
    status = raw_json.get("status")
    reason = "tool_calls" if has_fn else (
        "length" if status == "incomplete" else "stop"
    )
    chunks.append({"type": "finish", "reason": reason})
    return chunks


def _messages_to_input(messages: list[dict]) -> list[dict]:
    """Chat-completions history → Responses `input` items."""
    items: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or ""
        if role == "tool":
            output = msg.get("content")
            if not isinstance(output, str):
                output = "" if output is None else str(output)
            call_id = msg.get("tool_call_id") or msg.get("id") or ""
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            })
            continue
        if role == "assistant":
            content = msg.get("content")
            if content:
                items.append({
                    "role": "assistant",
                    "content": _chat_content_to_responses(content, as_user=False),
                })
            for tc in msg.get("tool_calls") or []:
                item = _function_call_item(tc)
                if item:
                    items.append(item)
            continue
        if role in ("system", "developer", "user"):
            items.append({
                "role": role,
                "content": _chat_content_to_responses(
                    msg.get("content"), as_user=role != "assistant"
                ),
            })
            continue
        items.append(msg)
    return items


def _function_call_item(tc: Any) -> dict | None:
    if not isinstance(tc, dict):
        return None
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
    name = (fn or {}).get("name") or tc.get("name")
    arguments = (fn or {}).get("arguments") if fn else tc.get("arguments")
    if not name:
        return None
    # 必须产出能被网关 json.loads 的合法 JSON（Console Go 对空串 400
    # "`arguments` must be valid JSON"）。空参调用模型不一定发 "{}"，
    # 可能发 "" / None；dict 直接 str() 会变成非法 Python repr。
    if arguments is None or arguments == "":
        arguments = "{}"
    elif not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments)
        except (TypeError, ValueError):
            arguments = "{}"
    else:
        # 防御纵深：非空字符串也校验 JSON 合法性（SSE 截断产生半截 JSON、
        # 空白串等直通网关会 400 "must be valid JSON"）。tool_loop 已在
        # 历史写入前拦截，此处兜底 DB 回放/其他 provider 路径的畸形。
        trimmed = arguments.strip()
        if not trimmed:
            arguments = "{}"
        else:
            try:
                json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                arguments = "{}"
    return {
        "type": "function_call",
        "call_id": tc.get("id") or tc.get("call_id") or "",
        "name": name,
        "arguments": arguments,
    }


def _chat_content_to_responses(content: Any, *, as_user: bool) -> str | list[dict]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text", "output_text"):
            text = part.get("text") or ""
            parts.append({
                "type": "input_text" if as_user else "output_text",
                "text": text,
            })
        elif ptype == "image_url":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url") or ""
            if url:
                parts.append({"type": "input_image", "image_url": url})
        elif ptype == "input_image":
            parts.append(part)
    if len(parts) == 1 and parts[0].get("type") in ("input_text", "output_text"):
        return parts[0].get("text") or ""
    return parts


FORMAT_HANDLERS.setdefault(ApiFormat.OPENAI_RESPONSES, OpenAIResponsesHandler())
