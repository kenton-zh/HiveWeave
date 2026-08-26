"""TOOL_OUTCOME_UNKNOWN：副作用工具执行中抛异常时提示模型结果未知。

参考 DSH 的 TOOL_OUTCOME_UNKNOWN —— 工具可能已产生副作用时，不简单
当作「执行失败」，而是明确告诉模型「结果未知，先验证再行动」，避免
盲目重试造成重复副作用。
"""

from __future__ import annotations

from hiveweave.llm.streamer.tool_exec import (
    _SIDE_EFFECT_TOOLS,
    _unknown_outcome_content,
)


def test_side_effect_tool_injects_unknown_outcome():
    out = _unknown_outcome_content("bash", "RuntimeError", "boom")
    assert out.startswith("[Tool Error] RuntimeError: boom")
    assert "[TOOL OUTCOME UNKNOWN]" in out
    assert "验证实际状态" in out


def test_readonly_tool_keeps_tool_error_only():
    out = _unknown_outcome_content("read_file", "ValueError", "bad path")
    assert out == "[Tool Error] ValueError: bad path"
    assert "[TOOL OUTCOME UNKNOWN]" not in out


def test_side_effect_tool_set_covers_writers():
    # 写文件/跑命令/发消息/浏览器类工具必须在列（审计修复：与 doom_loop
    # 的重试容忍分组语义解耦，按「写/命令/外发/外部状态变更」界定）
    for name in (
        "bash", "bash_main", "run_command", "python_script",
        "apply_patch", "write_file", "edit_file", "move_file",
        "delete_file", "create_directory", "delete_directory",
        "send_message", "ask_agent", "notify_agent", "message_user",
        "browse", "browse_main", "spawn_subagent", "generate_image",
    ):
        assert name in _SIDE_EFFECT_TOOLS, name
    # 纯只读/未注册工具不在列（过度谨慎反而误导）
    for name in ("read_file", "websearch", "get_tasks", "execute_code"):
        assert name not in _SIDE_EFFECT_TOOLS, name
