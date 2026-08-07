"""_clean_messages 修复截断 tool_call arguments — BUGFIX 回归测试。

事故（taskflow CEO A044 砖化）：流式中断(stall_break)时 assistant 的
tool_call function.arguments 被截断成非法 JSON 并持久化。后续每次 LLM
请求都携带该历史 → ARK 网关 400「Invalid request body」→ 确定性
死循环，agent 永久砖化（consecutive_errors 7+，每次新 inbox 都触发失败）。

修复：ConversationStore._clean_messages 读时净化 —— 非法 arguments
就地替换为合法占位 JSON（保留 preview），None/非字符串归一化。
"""

from __future__ import annotations

import json

from hiveweave.conversation.store import ConversationStore


def _assistant_with_args(arguments: object, call_id: str = "call_x") -> dict:
    return {
        "role": "assistant",
        "content": "calling tool",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "ask_agent", "arguments": arguments},
            }
        ],
    }


class TestSanitizeToolCallArguments:
    def test_truncated_arguments_repaired_to_valid_json(self) -> None:
        """截断的 arguments（事故原貌）→ 修复后 json.loads 可通过。"""
        truncated = '{"message": "前端 VERIFY 证据 commit 一直是 9d25753c，根因：海盐'
        msgs = [
            _assistant_with_args(truncated, "call_bad"),
            {"role": "tool", "tool_call_id": "call_bad",
             "content": "Error: Parameter error in 'ask_agent'"},
        ]

        cleaned = ConversationStore._clean_messages(msgs)

        assistant = cleaned[0]
        args = assistant["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args)  # 必须合法 —— 这是 ARK 400 的根治点
        assert parsed["_repaired_truncated_arguments"] is True
        assert "9d25753c" in parsed["preview"]
        # tool 结果消息保留（tool_call_id 匹配）
        assert cleaned[1]["role"] == "tool"

    def test_valid_arguments_untouched(self) -> None:
        """合法 arguments 不被改写（幂等，避免误伤正常历史）。"""
        valid = '{"taskId": "abc-123", "decision": "approve"}'
        msgs = [_assistant_with_args(valid)]

        ConversationStore._clean_messages(msgs)

        assert msgs[0]["tool_calls"][0]["function"]["arguments"] == valid

    def test_none_arguments_become_empty_object(self) -> None:
        msgs = [_assistant_with_args(None)]

        ConversationStore._clean_messages(msgs)

        assert msgs[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_non_string_arguments_serialized(self) -> None:
        """dict 形式的 arguments（上游误传对象）→ 序列化为合法 JSON 字符串。"""
        msgs = [_assistant_with_args({"a": 1})]

        ConversationStore._clean_messages(msgs)

        args = msgs[0]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(args, str)
        assert json.loads(args) == {"a": 1}

    def test_empty_string_arguments_repaired(self) -> None:
        """空字符串不是合法 JSON → 修复。"""
        msgs = [_assistant_with_args("")]

        ConversationStore._clean_messages(msgs)

        args = msgs[0]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args)
        assert parsed["_repaired_truncated_arguments"] is True

    def test_orphan_tool_result_still_dropped(self) -> None:
        """既有行为不回退：孤立 tool 结果仍被清理。"""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_ghost", "content": "x"},
        ]

        cleaned = ConversationStore._clean_messages(msgs)

        assert len(cleaned) == 1
        assert cleaned[0]["role"] == "user"

    def test_full_request_body_valid_after_repair(self) -> None:
        """端到端断言：修复后整包可序列化为合法请求体（模拟 ARK 校验）。"""
        poisoned_turn = [
            {"role": "user", "content": "## Messages (chronological)"},
            _assistant_with_args(
                '{"message": "截断于此', "call_t1"),
            {"role": "tool", "tool_call_id": "call_t1",
             "content": "Error: Parameter error"},
            {"role": "assistant", "content": "⚠️ Turn ended early"},
            {"role": "user", "content": "next inbox"},
        ]

        cleaned = ConversationStore._clean_messages(poisoned_turn)
        body = {"model": "ark-code-latest", "messages": cleaned}

        # ARK 侧校验等价物：每个 tool_call arguments 必须 json 可解析
        serialized = json.dumps(body, ensure_ascii=False)
        assert serialized
        for m in cleaned:
            for tc in m.get("tool_calls") or []:
                json.loads(tc["function"]["arguments"])
