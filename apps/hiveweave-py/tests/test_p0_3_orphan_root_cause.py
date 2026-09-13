"""P0-3 真根因守卫（2026-09-13 TEST_DSH_55）—— 115 步「结果未知」的两处来源。

背景（独立核实全文见 `docs/platform-issue-research/verify-2026-09-13/issue-3.md`）：
- 报告给的根因「run 结束不 drain 在飞 tool_calls」**不成立**：92/115 孤儿步在
  run 结束前 >60s 已被放弃，86/115 有 ±2s 内完成的同批兄弟步。
- 真链条：工具失败结果未声明 `fact` → `fact_positions.assert_fact_complete`
  **硬抛 AssertionError** → 逃逸 `agents/streaming.on_tool_call` 里**未加防护栏**的
  `await execute()` → `record_step_end` 被跳过 → 该行滞留 `status='running'`
  → 被 sweep 一律改判 `outcome_unknown`（实测 ≥48% 结果其实已知）。

这两处**此前没有任何测试守卫**（216 个相关用例在修复前后均全绿）—— 本文件补上。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# ── 守卫 1：失败结果缺 fact 不得抛异常（fail loud 但不 fail hard）──


def test_finalize_tool_result_does_not_raise_when_fact_missing():
    """失败结果缺 fact ⇒ 兜底 runner_failed，**不抛**。

    阳性对照：把 fact_positions.finalize_tool_result 尾部改回裸调
    `assert_fact_complete(tool_name, out)` → 本用例以 AssertionError 转红。
    """
    from hiveweave.tools.fact_positions import finalize_tool_result

    out = finalize_tool_result("read_file", {"success": False, "error": "boom"})
    # ⚠ 兜底必须是 `outcome_unknown`（「结果未知」），**不能**是 runner_failed：
    # 后者语义是「命令从未执行」⇒ 下游读成「可直接重试、无副作用」，而本路径
    # 的触发点在**执行之后** ⇒ 会诱发**副作用双发**（审计 P0-3 第 1 条）。
    assert out["fact"] == "outcome_unknown"


def test_invalid_fact_kind_is_rejected_at_construction():
    """边界守卫：非法 fact 在 `ToolResult` 构造期即被拦，**不**走 fail-soft。

    fail-soft 只兜「fact 缺失」（平台侧构造点漏声明）；「fact 非法」是构造期
    契约错误，必须继续 fail loud（`tools/result.py` 的 `__post_init__`）。
    这条钉住边界，免得后人以为可以往 finalize 塞任意 fact 字符串。
    """
    from hiveweave.tools.fact_positions import finalize_tool_result

    with pytest.raises(ValueError, match="unknown FactKind"):
        finalize_tool_result(
            "read_file",
            {"success": False, "error": "boom", "fact": "not_a_real_kind"},
        )


def test_finalize_tool_result_keeps_declared_fact_when_valid():
    """反向守卫：**合法** fact 必须原样保留（别把 fail-soft 做成一律覆盖）。"""
    from hiveweave.tools.fact_positions import finalize_tool_result

    out = finalize_tool_result(
        "read_file", {"success": False, "error": "boom", "fact": "bad_args"}
    )
    assert out["fact"] == "bad_args"


# ── 守卫 2：execute() 抛异常时账本仍须落 end ──


def _make_streaming_agent(execute_side_effect):
    agent = SimpleNamespace()
    agent.id = "a1"
    agent.project_id = "p1"
    agent._current_run_id = "run-1"
    agent._run_step_counter = 0

    async def _ws():
        return "/tmp/ws"

    agent._get_workspace_path = _ws
    agent._stop_heartbeat = lambda: None

    ledger = AsyncMock()
    ledger.record_step_start = AsyncMock(return_value="step-1")
    agent._run_ledger = ledger

    executor = AsyncMock()
    executor.execute = AsyncMock(side_effect=execute_side_effect)
    agent._tool_executor = executor
    return agent


@pytest.mark.asyncio
async def test_execute_exception_still_records_step_end(monkeypatch):
    """execute() 抛异常 ⇒ 账本仍必须落 `status='failed'` + `runner_failed=True`。

    这是 115 步的直接来源：防护栏缺失时 `record_step_end` 被整段跳过，
    该行永久滞留 `running`（直到 sweep 误判）。

    阳性对照：把 streaming.on_tool_call 里包住 execute() 的 try/except 去掉
    → 本用例在 `record_step_end.assert_awaited()` 处转红。
    """
    from hiveweave.agents import streaming as st

    agent = _make_streaming_agent(RuntimeError("boom from fact_positions"))
    monkeypatch.setattr(st, "broadcast_stream_event", lambda *a, **k: None)
    monkeypatch.setattr(
        st.meta_db, "get_project_workspace", AsyncMock(return_value="/tmp/ws")
    )

    # 异常必须**继续向上一层抛**（上层 tool_exec 的 [Tool Error] 路径与模型
    # 可见内容保持不变 —— 本修复只补记账，不改语义）。
    with pytest.raises(RuntimeError):
        await st.on_tool_call(agent, "read_file", '{"filePath":"x"}', "call-1")

    agent._run_ledger.record_step_end.assert_awaited()
    kw = agent._run_ledger.record_step_end.await_args.kwargs
    assert kw["status"] == "failed"
    # ⚠ 不得标 runner_failed（=「命令从未执行」⇒「无副作用、可直接重试」）：
    # 本 except 的触发点在**执行之后**，命令可能已执行 —— 留 None 才是保守方向。
    assert kw.get("runner_failed") is None, (
        "触发点在执行之后，标 runner_failed 会把「可能已有副作用」误报成"
        "「必然无副作用」⇒ 副作用双发（审计 P0-3 第 1 条）"
    )
    assert kw["step_id"] == "step-1"
    assert "boom" in str(kw["error"])


@pytest.mark.asyncio
async def test_execute_exception_does_not_swallow(monkeypatch):
    """异常不得被吞掉（否则模型看不到「工具出错」，会出现静默失败）。"""
    from hiveweave.agents import streaming as st

    agent = _make_streaming_agent(ValueError("distinct-sentinel"))
    monkeypatch.setattr(st, "broadcast_stream_event", lambda *a, **k: None)
    monkeypatch.setattr(
        st.meta_db, "get_project_workspace", AsyncMock(return_value="/tmp/ws")
    )

    with pytest.raises(ValueError, match="distinct-sentinel"):
        await st.on_tool_call(agent, "read_file", '{"filePath":"x"}', "call-1")
