"""vision.extract_nonstream_text 的 openai-responses 非流式形态（TEST_DSH_62）。

muse-spark 走 Responses 协议网关：HTTP 200、object=response、
output=[{type:reasoning,...},{type:message,content:[{type:"output_text",...}]}]。
旧解析只认 chat choices / Anthropic content / Gemini candidates 三形态，把
完整回答读成空 ⇒ analyze_image 抛 "Vision model returned empty content"。
本文件锁死第四形态的取数路径 + 旧三形态防回归。
"""

from __future__ import annotations

from hiveweave.services.vision import extract_nonstream_text


# ── 1. openai-responses 非流式形态 ──────────────────────────────


def test_responses_shape_extracts_output_text():
    """实测形态：reasoning 条目在前（须跳过），文本在 message.content 的
    output_text 条目里。"""
    data = {
        "object": "response",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text",
                                              "text": "thinking..."}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "图中是一只猫。"}],
            },
        ],
    }
    assert extract_nonstream_text(data) == "图中是一只猫。"


def test_responses_shape_multiple_text_parts_joined():
    """多个 message / 多段 output_text 依序拼接。"""
    data = {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "第一段。"},
                    {"type": "output_text", "text": "第二段。"},
                ],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "第三段。"}],
            },
        ],
    }
    assert extract_nonstream_text(data) == "第一段。第二段。第三段。"


def test_responses_shape_completed_without_text_returns_empty():
    """status=completed 但只有 reasoning 条目 ⇒ 返回空串，交给既有
    "Vision model returned empty content" 报错路径（不伪造文本）。"""
    data = {
        "object": "response",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text",
                                              "text": "only thinking"}]},
        ],
    }
    assert extract_nonstream_text(data) == ""


def test_responses_shape_without_object_marker_still_parsed():
    """识别条件是 object=="response" **或** 存在 output 列表——部分网关
    省略 object 标记，仅有 output 列表时也要取到。"""
    data = {
        "status": "completed",
        "output": [
            {"type": "function_call", "call_id": "c1", "name": "x",
             "arguments": "{}"},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "工具调用之外的回答"}],
            },
        ],
    }
    assert extract_nonstream_text(data) == "工具调用之外的回答"


# ── 2. 旧三形态防回归 ──────────────────────────────────────────


def test_openai_choices_shape_still_parsed():
    data = {
        "object": "chat.completion",
        "choices": [
            {"message": {"role": "assistant", "content": "chat 形态回答"}}
        ],
    }
    assert extract_nonstream_text(data) == "chat 形态回答"


def test_anthropic_content_shape_still_parsed():
    data = {
        "content": [
            {"type": "text", "text": "anthropic "},
            {"type": "text", "text": "形态回答"},
        ]
    }
    assert extract_nonstream_text(data) == "anthropic 形态回答"


def test_gemini_candidates_shape_still_parsed():
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "gemini "}, {"text": "形态回答"}]
                }
            }
        ]
    }
    assert extract_nonstream_text(data) == "gemini 形态回答"
