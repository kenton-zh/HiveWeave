"""streamer.py 纯函数单测 — 防止 text chunk 丢弃 bug 回归.

历史 bug: `if ctype == "text": pass` 导致所有 LLM 文本响应被丢弃.
本测试覆盖 streamer.py 中 6 个不依赖网络/DB 的纯函数:
  - parse_sse
  - _extract_data
  - sse_to_chunks
  - _extract_reasoning
  - _extract_text_content
  - merge_tool_calls

被测模块: hiveweave.llm.streamer
"""

from __future__ import annotations

import pytest

from hiveweave.llm.streamer import (
    _extract_data,
    _extract_reasoning,
    _extract_text_content,
    merge_tool_calls,
    parse_sse,
    sse_to_chunks,
)


# ── parse_sse ────────────────────────────────────────────────


class TestParseSse:
    """SSE 缓冲区解析 — \\n\\n / \\r\\n\\r\\n 分隔、leftover、空缓冲、[DONE]."""

    def test_empty_buffer_returns_empty(self):
        """空缓冲区返回 ([], '')."""
        events, leftover = parse_sse("")
        assert events == []
        assert leftover == ""

    def test_lf_separator_parses_events(self):
        """标准 \\n\\n 分隔: 完整事件被解析, leftover 为空."""
        buf = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        events, leftover = parse_sse(buf)
        assert leftover == ""
        assert len(events) == 1
        assert events[0]["choices"][0]["delta"]["content"] == "hi"

    def test_crlf_separator_cdn_compat(self):
        """\\r\\n\\r\\n 分隔（CDN/代理规范化）: 规范化为 \\n\\n 后正常解析."""
        buf = 'data: {"choices":[{"delta":{"content":"ok"}}]}\r\n\r\n'
        events, leftover = parse_sse(buf)
        assert leftover == ""
        assert len(events) == 1
        assert events[0]["choices"][0]["delta"]["content"] == "ok"

    def test_incomplete_block_kept_as_leftover(self):
        """没有结尾 \\n\\n 的不完整事件块应作为 leftover 返回, 不解析."""
        buf = 'data: {"choices":[{"delta":{"content":"partial"'
        events, leftover = parse_sse(buf)
        assert events == []
        # leftover 保留原文（CRLF 已被规范化为 LF）
        assert leftover == buf.replace("\r\n", "\n")

    def test_done_marker_produces_done_event(self):
        """data: [DONE] 标记应解析为 {'__done__': True} 事件."""
        buf = "data: [DONE]\n\n"
        events, leftover = parse_sse(buf)
        assert leftover == ""
        assert events == [{"__done__": True}]


# ── _extract_data ────────────────────────────────────────────


class TestExtractData:
    """从 SSE 事件块提取 data — 多行拼接、Anthropic event:、无效 JSON."""

    def test_multiline_data_concatenated(self):
        """多行 data: 应拼接后解析为单个 JSON 对象."""
        # 多行 data: 拼接（某些代理会分行发送大 JSON）
        block = 'data: {"a":\ndata: 1, "b":\ndata: 2}'
        result = _extract_data(block)
        assert result == {"a": 1, "b": 2}

    def test_anthropic_event_type_attached(self):
        """Anthropic 格式 event: + data: 应将 event 值附加为 _event_type."""
        block = 'event: content_block_delta\ndata: {"type":"text_delta","text":"x"}'
        result = _extract_data(block)
        # data JSON 自带 type 字段 → _event_type 不覆盖
        assert result is not None
        assert result["type"] == "text_delta"
        # 当 data JSON 已含 type 时不附加 _event_type
        assert "_event_type" not in result

    def test_anthropic_event_type_when_no_type_in_data(self):
        """data JSON 不含 type 时, event: 值附加为 _event_type."""
        block = 'event: message_start\ndata: {"message":{"id":"msg_1"}}'
        result = _extract_data(block)
        assert result is not None
        assert result["message"]["id"] == "msg_1"
        assert result["_event_type"] == "message_start"

    def test_invalid_json_returns_none(self):
        """无效 JSON 返回 None（不抛异常）."""
        block = "data: {not valid json"
        result = _extract_data(block)
        assert result is None


# ── sse_to_chunks ────────────────────────────────────────────


class TestSseToChunks:
    """SSE event → chunk 列表. 重点: text chunk 必须被正确产出."""

    def test_text_delta_produces_text_chunk(self):
        """【回归核心】delta.content 必须产出 {type:text, content} chunk.

        历史 bug: 上层循环 `if ctype == "text": pass` 丢弃所有文本.
        本测试锁定 sse_to_chunks 的契约 — 文本 delta 必须返回 text chunk,
        确保上层循环修复后不会再因 sse_to_chunks 漏产 text chunk 而丢文本.
        """
        event = {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
        chunks = sse_to_chunks(event)
        text_chunks = [c for c in chunks if c["type"] == "text"]
        assert len(text_chunks) == 1
        assert text_chunks[0]["content"] == "hello"

    def test_text_reasoning_tool_calls_finish_all_present(self):
        """同一 delta 同时携带 reasoning + text + tool_calls + finish_reason → 4 个 chunk."""
        event = {
            "choices": [{
                "delta": {
                    "reasoning_content": "thinking...",
                    "content": "answer",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "bash", "arguments": "{\"cmd\":"},
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }
        chunks = sse_to_chunks(event)
        types = [c["type"] for c in chunks]
        # 顺序: reasoning → text → tool_call_delta → finish
        assert types == ["reasoning", "text", "tool_call_delta", "finish"]
        assert chunks[0]["content"] == "thinking..."
        assert chunks[1]["content"] == "answer"
        assert chunks[2]["tool_call"]["name"] == "bash"
        assert chunks[3]["reason"] == "tool_calls"

    def test_empty_choices_returns_empty(self):
        """choices 为空列表 → 返回空 chunk 列表."""
        assert sse_to_chunks({"choices": []}) == []
        # choices 缺失也应返回空
        assert sse_to_chunks({}) == []

    def test_done_event_returns_empty(self):
        """__done__ 事件 → 空 chunk 列表（不产生任何 chunk）."""
        assert sse_to_chunks({"__done__": True}) == []


# ── _extract_reasoning ───────────────────────────────────────


class TestExtractReasoning:
    """reasoning 提取 — 多字段名变体兼容."""

    @pytest.mark.parametrize(
        "field_name",
        ["reasoning_content", "reasoning", "thinking", "thinking_content"],
    )
    def test_multiple_field_name_variants(self, field_name):
        """不同 provider 使用不同字段名, 都应被识别."""
        delta = {field_name: "推理内容"}
        assert _extract_reasoning(delta) == "推理内容"

    def test_no_reasoning_field_returns_none(self):
        """delta 中无任何 reasoning 字段 → 返回 None."""
        assert _extract_reasoning({"content": "text only"}) is None
        # 空字符串也视为无 reasoning
        assert _extract_reasoning({"thinking": ""}) is None


# ── _extract_text_content ────────────────────────────────────


class TestExtractTextContent:
    """text content 提取 — string / array-of-blocks / 空值."""

    def test_string_content(self):
        """content 为字符串 → 原样返回（非空）."""
        assert _extract_text_content("hello world") == "hello world"

    def test_array_of_blocks_content(self):
        """content 为 content blocks 数组 → 拼接所有 text block."""
        content = [
            {"type": "text", "text": "part1 "},
            {"type": "image", "image_url": "..."},  # 非 text block 跳过
            {"type": "text", "text": "part2"},
        ]
        assert _extract_text_content(content) == "part1 part2"

    def test_empty_or_non_text_returns_none(self):
        """空值/None/空数组/无 text block → 返回 None."""
        assert _extract_text_content("") is None
        assert _extract_text_content(None) is None
        assert _extract_text_content([]) is None
        assert _extract_text_content([{"type": "image", "image_url": "x"}]) is None


# ── merge_tool_calls ─────────────────────────────────────────


class TestMergeToolCalls:
    """流式 tool_call deltas 合并 — 分组/拼接/取首 id."""

    def test_multi_index_grouping(self):
        """不同 index 的 deltas 分组为独立的 tool_call."""
        deltas = [
            {"index": 0, "id": "call_a", "name": "bash", "arguments": "{}"},
            {"index": 1, "id": "call_b", "name": "grep", "arguments": "{}"},
        ]
        result = merge_tool_calls([], deltas)
        assert len(result) == 2
        assert result[0]["id"] == "call_a"
        assert result[0]["name"] == "bash"
        assert result[1]["id"] == "call_b"
        assert result[1]["name"] == "grep"

    def test_name_arguments_fragment_concat(self):
        """name 和 arguments 分片到达时按顺序拼接成完整字符串."""
        # 模拟流式分片: name 和 arguments 分多次到达
        deltas = [
            {"index": 0, "id": "call_1", "name": "bas", "arguments": "{\"cmd\":"},
            {"index": 0, "id": None, "name": "h", "arguments": "\"ls\"}"},
        ]
        result = merge_tool_calls([], deltas)
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        assert result[0]["arguments"] == '{"cmd":"ls"}'

    def test_id_takes_first_non_empty(self):
        """id 取首个非空值; 后续 delta 的 id（含 None）不覆盖."""
        deltas = [
            {"index": 0, "id": None, "name": "bash", "arguments": ""},
            {"index": 0, "id": "first_id", "name": "", "arguments": "{}"},
            {"index": 0, "id": "later_id", "name": "", "arguments": ""},
        ]
        result = merge_tool_calls([], deltas)
        assert result[0]["id"] == "first_id"

    def test_merges_with_existing(self):
        """新 deltas 与 existing 列表合并, 同 index 累积."""
        existing = [{"index": 0, "id": "call_x", "name": "ba", "arguments": "{"}]
        new = [{"index": 0, "id": None, "name": "sh", "arguments": "}"}]
        result = merge_tool_calls(existing, new)
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        assert result[0]["arguments"] == "{}"
        assert result[0]["id"] == "call_x"
