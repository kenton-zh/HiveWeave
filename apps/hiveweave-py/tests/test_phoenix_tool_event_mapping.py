"""phoenix_adapter _map_event — 工具事件归一化（canonical + streamer 直连）。

M1 契约：tool_call_start/end 与原始 tool_use/tool_result 四种后端事件
都必须落到前端 stream_tool 频道事件，payload 形状一致——否则 solo/
streamer 直连路径的工具行永远停在 running（事件被静默丢弃）。
"""

from __future__ import annotations

from hiveweave.realtime.phoenix_adapter import _map_event


def test_canonical_tool_call_end_maps_to_stream_tool():
    name, payload = _map_event({
        "type": "tool_call_end",
        "tool_name": "bash",
        "tool_call_id": "c1",
        "success": True,
        "result": "ok output",
    })
    assert name == "stream_tool"
    assert payload["type"] == "tool_result"
    assert payload["toolName"] == "bash"
    assert payload["toolCallId"] == "c1"
    assert payload["success"] is True
    assert payload["result"] == "ok output"


def test_raw_tool_result_normalized_to_stream_tool():
    """streamer 直连路径：原始 tool_result（content 全量）→ 归一 + 截断。"""
    name, payload = _map_event({
        "type": "tool_result",
        "tool_call_id": "c2",
        "tool_name": "bash",
        "success": False,
        "content": "x" * 3000,
    })
    assert name == "stream_tool"
    assert payload["type"] == "tool_result"
    assert payload["toolCallId"] == "c2"
    assert payload["success"] is False
    assert len(payload["result"]) == 500
    assert payload["result"] == "x" * 500


def test_raw_tool_result_legacy_shape_defaults_success_true():
    """旧形状（无 tool_name/success）：success 默认 True，result 取 content。"""
    name, payload = _map_event({
        "type": "tool_result",
        "tool_call_id": "c3",
        "content": "plain",
    })
    assert name == "stream_tool"
    assert payload["type"] == "tool_result"
    assert payload["success"] is True
    assert payload["result"] == "plain"
    assert payload["toolName"] == ""


def test_raw_tool_use_normalized_to_stream_tool():
    """streamer 直连路径：原始 tool_use → 与 tool_call_start 同形状。"""
    name, payload = _map_event({
        "type": "tool_use",
        "tool_call_id": "c4",
        "tool_name": "read_file",
        "arguments": '{"path":"a"}',
    })
    assert name == "stream_tool"
    assert payload["type"] == "tool_use"
    assert payload["toolName"] == "read_file"
    assert payload["toolCallId"] == "c4"
    assert payload["arguments"] == '{"path":"a"}'


def test_canonical_tool_call_start_unchanged():
    name, payload = _map_event({
        "type": "tool_call_start",
        "tool_name": "bash",
        "tool_call_id": "c5",
        "arguments": "{}",
    })
    assert name == "stream_tool"
    assert payload["type"] == "tool_use"
    assert payload["toolName"] == "bash"
