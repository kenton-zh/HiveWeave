"""build_display_segments — 整轮块序列展平（DSH 风格主栏渲染数据源）。

覆盖：旁白/工具时序、tool 结果按 tool_call_id 回填、失败标记
（tool_history ok=False）、final_content 去重（含 budget 拼接结构 /
长差量防误截 / 空 tail 弹出）、坏 JSON 参数兜底、结果截断。
纯同步逻辑，用例不使用 async。
"""

from __future__ import annotations

import json

from hiveweave.agents.completion import build_display_segments


def _tc(tc_id: str, name: str, args: str) -> dict:
    return {"id": tc_id, "type": "function", "function": {"name": name, "arguments": args}}


def test_interleaved_narration_and_tools_in_order():
    turn = [
        {"role": "assistant", "content": "先看文件"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "read_file", '{"path":"a.py"}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        {"role": "assistant", "content": "再搜索"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c2", "search_files", '{"pattern":"x"}')]},
        {"role": "tool", "tool_call_id": "c2", "content": "no match"},
        {"role": "assistant", "content": "完成"},
    ]
    segs = build_display_segments(turn, "完成", [])
    kinds = [(s["type"], s.get("tool")) for s in segs]
    assert kinds == [
        ("text", None),
        ("tool_call", "read_file"),
        ("text", None),
        ("tool_call", "search_files"),
        ("text", None),
    ]
    assert segs[1]["result"] == "file body"
    assert segs[1]["status"] == "ok"
    assert segs[1]["input"] == {"path": "a.py"}
    # final_content 与末轮 text 相同 → 不重复追加
    assert sum(1 for s in segs if s["type"] == "text") == 3


def test_reasoning_content_becomes_inline_thinking_blocks():
    """DSH 整轮视图：每轮 reasoning 作为 thinking 块原位保留（含末轮）。

    对齐 deepseek-harness 把 reasoning 作为有序块持久化 —— done reload
    后流式期间可见的思考段不丢失。
    """
    turn = [
        {"role": "assistant", "content": "先看文件", "reasoning_content": "我要先读文件"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "read_file", '{"path":"a.py"}')], "reasoning_content": "分析路径"},
        {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        {"role": "assistant", "content": "完成", "reasoning_content": "总结结果"},
    ]
    segs = build_display_segments(turn, "完成", [])
    kinds = [(s["type"], s.get("tool")) for s in segs]
    assert kinds == [
        ("thinking", None),
        ("text", None),
        ("thinking", None),
        ("tool_call", "read_file"),
        ("thinking", None),
        ("text", None),
    ]
    assert segs[0]["content"] == "我要先读文件"
    assert segs[4]["content"] == "总结结果"


def test_final_round_thinking_persisted_via_final_msg():
    """末轮 thinking 由 tool_loop 写入 final assistant 的 reasoning_content。

    流式期间 thinking_delta 段在 done reload 后仍以 thinking 块呈现，
    不再只靠 thinking 列（且 MessageBubble 在 segments 含 thinking 时
    会隐藏 _thinking，若不写 segments 末轮思考即丢失）。
    """
    turn = [
        {"role": "assistant", "content": "中间旁白", "reasoning_content": "中间推理"},
        {"role": "assistant", "content": "最终回答", "reasoning_content": "末轮推理"},
    ]
    segs = build_display_segments(turn, "最终回答", [])
    assert [s["type"] for s in segs] == ["thinking", "text", "thinking", "text"]
    assert segs[2]["content"] == "末轮推理"


def test_failed_tool_marked_error_via_tool_history():
    turn = [
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "bash", "{}")]},
        {"role": "tool", "tool_call_id": "c1", "content": "Error: boom"},
    ]
    history = [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}, "ok": False}]
    segs = build_display_segments(turn, "", history)
    assert segs[0]["status"] == "error"
    assert segs[0]["result"] == "Error: boom"


def test_tool_without_result_finalized_by_ok_map():
    turn = [
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "read_file", "{}")]},
    ]
    segs = build_display_segments(turn, "done", [])
    assert segs[0]["status"] == "ok"


def test_final_content_appended_when_missing_from_turn():
    segs = build_display_segments([], "纯文本回复", [])
    assert segs == [{"type": "text", "content": "纯文本回复"}]


def test_bad_json_arguments_fallback_raw():
    turn = [
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "write_file", "{broken")]},
    ]
    segs = build_display_segments(turn, "", [])
    assert "_raw" in segs[0]["input"]


def test_tool_result_excerpt_truncated():
    turn = [
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "bash", "{}")]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 5000},
    ]
    segs = build_display_segments(turn, "", [])
    assert len(segs[0]["result"]) == 2000


def test_segments_json_serializable_for_metadata_column():
    turn = [
        {"role": "assistant", "content": "n"},
        {"role": "assistant", "content": None, "tool_calls": [_tc("c1", "read_file", '{"path":"a"}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    segs = build_display_segments(turn, "n", [])
    # metadata 列存 JSON 字符串 —— 必须可序列化
    json.dumps({"segments": segs}, ensure_ascii=False)


def test_budget_exhausted_concatenated_final_deduped():
    """budget_exhausted：最后段 = 前段拼接 + 短尾注 → 只留尾注（保留原文分隔符）。"""
    turn = [
        {"role": "assistant", "content": "第一轮旁白"},
        {"role": "assistant", "content": "第二轮旁白"},
        {"role": "assistant", "content": "第一轮旁白第二轮旁白\n\n⚠️ 预算耗尽"},
    ]
    final = "第一轮旁白第二轮旁白\n\n⚠️ 预算耗尽"
    segs = build_display_segments(turn, final, [])
    texts = [s["content"] for s in segs if s["type"] == "text"]
    # 尾注保留原文（含 \n\n）→ 各段拼接与 content 列逐字一致
    assert texts == ["第一轮旁白", "第二轮旁白", "\n\n⚠️ 预算耗尽"]
    assert "".join(texts) == final


def test_final_beyond_joined_appends_short_tail():
    """final = 全部段落拼接 + 短尾注（拼接段不在 turn 里）→ 只追加尾注。"""
    turn = [
        {"role": "assistant", "content": "旁白A"},
        {"role": "assistant", "content": "旁白B"},
    ]
    final = "旁白A旁白B\n\n⚠️ 已收口"
    segs = build_display_segments(turn, final, [])
    texts = [s["content"] for s in segs if s["type"] == "text"]
    assert texts == ["旁白A", "旁白B", "\n\n⚠️ 已收口"]
    assert "".join(texts) == final


def test_final_beyond_joined_appends_long_tail_without_duplication():
    """elif 分支长差量：只追加差量（任意长度），零复述、总拼接==final。"""
    long_tail = "（长收口说明" + "细节" * 500 + "）"
    turn = [
        {"role": "assistant", "content": "旁白A"},
        {"role": "assistant", "content": "旁白B"},
    ]
    final = "旁白A旁白B" + long_tail
    segs = build_display_segments(turn, final, [])
    texts = [s["content"] for s in segs if s["type"] == "text"]
    assert texts == ["旁白A", "旁白B", long_tail]
    assert "".join(texts) == final


def test_long_tail_not_collapsed_legit_narration_kept():
    """L1 防误截：末段差量为长文本（合法旁白恰好以前文开头）→ 不折叠、不丢字。"""
    long_tail = "（后续完整旁白" + "内容" * 300 + "）"
    turn = [
        {"role": "assistant", "content": "开场白"},
        {"role": "assistant", "content": "开场白" + long_tail},
    ]
    final = "开场白" + long_tail
    segs = build_display_segments(turn, final, [])
    texts = [s["content"] for s in segs if s["type"] == "text"]
    # 长差量：末段原样保留（final==last，无重复、无截断）
    assert texts == ["开场白", "开场白" + long_tail]


def test_empty_tail_pops_last_segment():
    """末段 == 前段拼接且无差量 → 弹出冗余末段。"""
    turn = [
        {"role": "assistant", "content": "A"},
        {"role": "assistant", "content": "A"},
    ]
    segs = build_display_segments(turn, "A", [])
    texts = [s["content"] for s in segs if s["type"] == "text"]
    assert texts == ["A"]


# ── P2-⑩：result_summary 恢复「一句话摘要」语义 ────────────────────


def test_result_summary_takes_last_narration_not_head():
    """续跑拼接的 content 取**末条**旁白，不再堆同义句。

    实测形态：8/8 agent 该字段长度精确等于 200，最长样本 9 个同义句
    （「正在收尾…收尾冲刺中…正在修复构建链…」）——根因是取 content[:200]，
    而 content 是各轮旁白拼接。
    """
    from hiveweave.agents.completion import (
        RESULT_SUMMARY_MAX,
        build_run_result_summary,
    )

    content = (
        "正在收尾，准备提交。\n\n"
        "收尾冲刺中，还差两个测试。\n\n"
        "正在修复构建链，预计一轮内完成。"
    )
    summary = build_run_result_summary(content)
    assert summary == "正在修复构建链，预计一轮内完成。"
    assert len(summary) <= RESULT_SUMMARY_MAX
    # 关键回归：旧口径会把最早那句放在头部并堆到 200 字
    assert not summary.startswith("正在收尾，准备提交。")


def test_result_summary_skips_platform_note_but_keeps_it_as_fallback():
    """平台尾注不冒充 agent 摘要；但无旁白时仍保留其信息。"""
    from hiveweave.agents.completion import build_run_result_summary

    note = "[TURN BUDGET] Hard turn budget exhausted — all progress is kept."
    assert build_run_result_summary(f"模块 A 已联调通过。\n\n{note}") == (
        "模块 A 已联调通过。"
    )
    assert build_run_result_summary(note).startswith("[TURN BUDGET]")


def test_result_summary_edges():
    """空内容 / 纯空白 / 超长单句（尾部截断保留结束语义）。"""
    from hiveweave.agents.completion import (
        RESULT_SUMMARY_MAX,
        build_run_result_summary,
    )

    assert build_run_result_summary("") == ""
    assert build_run_result_summary("\n\n   \n\n") == ""
    # 单行内的换行被折叠成空格（一句话语义）
    assert build_run_result_summary("第一行\n第二行") == "第一行 第二行"
    long_tail = "x" * 50 + "结论在这里"
    summary = build_run_result_summary("前情。\n\n" + long_tail, max_chars=20)
    assert len(summary) == 20
    assert summary.endswith("结论在这里")
