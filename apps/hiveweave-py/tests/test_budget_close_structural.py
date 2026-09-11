"""预算耗尽收口的结构化 content 回归（09-08 去复读重构）。

用户否决了文本相似度折叠（换语言/换措辞即失效——「永远不要通过文案
猜意图」）。结构性修法：``_budget_exhausted_result`` 的 content 不再
使用 text_acc 全量拼接 blob，而是从 ``tool_turn_acc`` 的轮次结构里取
最后一轮 assistant 正文 + 收口说明——语言无关、零猜测。

完整逐轮时间线仍由 tool_turn_acc（conversation context）与
metadata.segments（前端渲染）承载，信息零损失。
"""

from __future__ import annotations

import pytest

from hiveweave.llm.streamer.core import Streamer


def _loop() -> Streamer:
    return Streamer()


def test_budget_close_content_is_last_round_plus_note():
    loop = _loop()
    tool_turn_acc = [
        {"role": "assistant", "content": "收到：任务 A。先盘点仓库现状。"},
        {"role": "tool", "content": "12 modules", "tool_call_id": "t1"},
        {"role": "assistant", "content": "盘点完成：仓库共 12 个模块。"},
    ]
    blob = "\n\n".join([
        "收到：任务 A。先盘点仓库现状。",
        "盘点完成：仓库共 12 个模块。",
    ])
    result = loop._budget_exhausted_result(
        text_acc=blob,
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=tool_turn_acc,
        round_num=3,
        last_usage=None,
    )
    assert result["status"] == "ok"
    # content 只含末轮正文 + 收口说明，不再整轮拼接
    assert result["content"].startswith("盘点完成：仓库共 12 个模块。")
    assert "[TURN BUDGET]" in result["content"]
    assert "先盘点仓库现状" not in result["content"]
    # 逐轮历史仍在 conversation context（信息零损失）
    assert tool_turn_acc[0]["content"] == "收到：任务 A。先盘点仓库现状。"
    # 收口消息已作为最后一条 assistant 落账
    assert tool_turn_acc[-1]["role"] == "assistant"
    assert tool_turn_acc[-1]["content"] == result["content"]


def test_budget_close_with_empty_history_falls_back_to_note():
    loop = _loop()
    tool_turn_acc = []
    result = loop._budget_exhausted_result(
        text_acc="",
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=tool_turn_acc,
        round_num=1,
        last_usage=None,
    )
    assert result["content"].startswith("[TURN BUDGET]")
    assert result["content"] == tool_turn_acc[-1]["content"]


def test_budget_close_custom_note_respected():
    loop = _loop()
    tool_turn_acc = [
        {"role": "assistant", "content": "第二轮进展：已定位根因。"},
    ]
    result = loop._budget_exhausted_result(
        text_acc="第一轮文本\n\n第二轮进展：已定位根因。",
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=tool_turn_acc,
        round_num=2,
        last_usage=None,
        note="[TURN BUDGET] custom guidance",
    )
    assert result["content"] == (
        "第二轮进展：已定位根因。\n\n[TURN BUDGET] custom guidance"
    )


def test_budget_close_skips_tool_and_empty_messages():
    loop = _loop()
    tool_turn_acc = [
        {"role": "assistant", "content": "第一轮：开始。"},
        {"role": "tool", "content": "result", "tool_call_id": "t1"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "第三轮：结论已得出。"},
    ]
    result = loop._budget_exhausted_result(
        text_acc="第一轮：开始。\n\n第三轮：结论已得出。",
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=tool_turn_acc,
        round_num=3,
        last_usage=None,
    )
    assert result["content"].startswith("第三轮：结论已得出。")


@pytest.mark.asyncio
async def test_budget_close_content_never_repeats_earlier_rounds():
    """端到端语义断言：任何语言、任何措辞，content 都不再包含前几轮旁白
    ——不依赖文案匹配，换法语也一样成立。"""
    loop = _loop()
    tool_turn_acc = [
        {"role": "assistant", "content": "Reçu : empaqueter Godot dans HIVE."},
        {"role": "tool", "content": "ok", "tool_call_id": "t1"},
        {"role": "assistant", "content": "Structure vue, lecture des docs."},
    ]
    result = loop._budget_exhausted_result(
        text_acc=(
            "Reçu : empaqueter Godot dans HIVE.\n\n"
            "Structure vue, lecture des docs."
        ),
        thinking_acc="",
        tool_history=[],
        tool_turn_acc=tool_turn_acc,
        round_num=2,
        last_usage=None,
    )
    assert "Reçu :" not in result["content"]
    assert result["content"].startswith("Structure vue")
