"""P0-4 (TEST_DSH_33): defer_task_advance 同 reason 连发断路器。

现场：砚舟 4h17m 内 defer 17 次、reason 几乎一字不差，平台无限静默接受，
M-C 永久停在 approved。断路器契约（services/turn_session.py）：
- 同一 agent 连续同 reason（归一化前 80 字符为 key）defer ≥3 次 → 拒绝
- 拒绝时同时解除 [TASK ADVANCE] 抑制（否则旧 flag 让催办永久静音）
- streak 只在 reason 变化或本轮真正推动账本时清零（跨唤醒复读正是形态）
"""

from __future__ import annotations

import pytest

from hiveweave.hooks.handlers.task_advance import (
    decide_task_advance_nudge,
    on_agent_turn_after,
)
from hiveweave.services.turn_session import (
    DEFER_REASON_STREAK_LIMIT,
    clear_defer_reason_streak,
    clear_task_advance_deferred,
    defer_breaker_tripped,
    defer_reason_streak,
    is_task_advance_deferred,
    normalize_defer_reason,
    record_defer_reason,
)

REASON = "QA 2013d080 已批复并完成 no-op merge (0 commits) — 等平台收口"


# 本文件触碰的所有 agent id（模块级 streak dict 需要测试间彻底隔离）
_AGENTS = ("agent-dfb", "agent-dfb-a", "agent-dfb2", "agent-dfb-empty")


@pytest.fixture(autouse=True)
def _reset_streak():
    for aid in _AGENTS:
        clear_defer_reason_streak(aid)
        clear_task_advance_deferred(aid)
    yield
    for aid in _AGENTS:
        clear_defer_reason_streak(aid)
        clear_task_advance_deferred(aid)


# ── 归一化与 streak 计数 ───────────────────────────────────


def test_normalize_defer_reason_whitespace_case_insensitive():
    assert normalize_defer_reason("Wait  For HR") == normalize_defer_reason(
        "wait for hr"
    )
    # 尾部改写不影响 key（前 80 字符）
    long_a = "x" * 100 + "AAA"
    long_b = "x" * 100 + "BBB"
    assert normalize_defer_reason(long_a) == normalize_defer_reason(long_b)


def test_record_defer_reason_streak_and_reset():
    assert record_defer_reason("agent-dfb-a", REASON) == 1
    assert record_defer_reason("agent-dfb-a", REASON) == 2
    assert record_defer_reason("agent-dfb-a", REASON) == 3
    assert defer_breaker_tripped("agent-dfb-a")
    # 换真实变化的 reason → streak 重开
    assert record_defer_reason("agent-dfb-a", "另一个真实原因") == 1
    assert not defer_breaker_tripped("agent-dfb-a")
    clear_defer_reason_streak("agent-dfb-a")
    assert defer_reason_streak("agent-dfb-a") == 0


def test_streak_limit_is_three():
    assert DEFER_REASON_STREAK_LIMIT == 3


# ── 工具侧：第 3 次拒绝 + 解除抑制 + 留痕 ───────────────────


@pytest.mark.asyncio
async def test_defer_tool_rejects_third_same_reason(monkeypatch):
    from hiveweave.tools.turn_tools import (
        DeferTaskAdvanceParams,
        defer_task_advance_tool,
    )

    logs: list[dict] = []

    async def fake_project_id(agent_id):
        return "proj-dfb"

    class FakeWorkLog:
        async def write_work_log(self, project_id, agent_id, task_id,
                                 log_type, summary, details=None):
            logs.append({"type": log_type, "summary": summary,
                         "details": details})

    monkeypatch.setattr(
        "hiveweave.db.meta.get_agent_project_id", fake_project_id
    )
    monkeypatch.setattr(
        "hiveweave.services.work_log.WorkLogService", FakeWorkLog
    )

    clear_defer_reason_streak("agent-dfb")
    r1 = await defer_task_advance_tool(
        DeferTaskAdvanceParams(reason=REASON), "agent-dfb", "", ctx=None
    )
    assert r1.success
    # 未达阈值也要写 work_log 留痕（可审计）
    assert logs[-1]["type"] == "task_advance_deferred"
    assert is_task_advance_deferred("agent-dfb")

    r2 = await defer_task_advance_tool(
        DeferTaskAdvanceParams(reason=REASON), "agent-dfb", "",
        ctx=None,
    )
    assert r2.success

    r3 = await defer_task_advance_tool(
        DeferTaskAdvanceParams(reason=REASON), "agent-dfb", "",
        ctx=None,
    )
    assert not r3.success
    assert "断路器" in r3.error
    # 三条出路必须齐全（等收口 / commit_turn waiting / 上报上级）
    assert "commit_turn" in r3.error
    assert "ledger" in r3.error
    assert "ask_agent" in r3.error
    # 拒绝的同时解除抑制：早前 defer 设下的 flag 不得让催办永久静音
    assert not is_task_advance_deferred("agent-dfb")
    # 留痕升级为 breaker 事件
    assert logs[-1]["type"] == "task_advance_defer_breaker"
    assert logs[-1]["details"]["same_reason_streak"] == 3


@pytest.mark.asyncio
async def test_defer_tool_empty_reason_rejected():
    from hiveweave.tools.turn_tools import (
        DeferTaskAdvanceParams,
        defer_task_advance_tool,
    )

    clear_defer_reason_streak("agent-dfb-empty")
    r = await defer_task_advance_tool(
        DeferTaskAdvanceParams(reason="   "), "agent-dfb-empty", "", ctx=None
    )
    assert not r.success
    clear_defer_reason_streak("agent-dfb-empty")


# ── hook 侧：真实账本推动清零；tripped 后催办复活 ───────────


@pytest.mark.asyncio
async def test_hook_clears_streak_on_real_advance():
    record_defer_reason("agent-dfb2", REASON)
    record_defer_reason("agent-dfb2", REASON)
    record_defer_reason("agent-dfb2", REASON)
    assert defer_breaker_tripped("agent-dfb2")

    out: dict = {"hint": None}
    await on_agent_turn_after(
        {
            "agent_id": "agent-dfb2",
            "open_obligations": [],
            "tool_calls": [
                {"function": {"name": "submit_task",
                              "arguments": '{"taskId": "t-1"}'}}
            ],
            "tasks_advanced": ["t-1"],
            "phase": "done_slice",
            "disposition": "runnable",
            "gate_repairing": False,
            "continue_slice": False,
            "deferred": False,
        },
        out,
    )
    assert defer_reason_streak("agent-dfb2") == 0
    assert not defer_breaker_tripped("agent-dfb2")
    clear_defer_reason_streak("agent-dfb2")


@pytest.mark.asyncio
async def test_hook_keeps_streak_without_real_advance():
    """跨唤醒不清零是设计核心：无账本推动时外部唤醒不得绕过断路器
    （砚舟 17 次 defer 正是跨唤醒复读形态）。"""
    record_defer_reason("agent-dfb2", REASON)
    record_defer_reason("agent-dfb2", REASON)
    record_defer_reason("agent-dfb2", REASON)

    out: dict = {"hint": None}
    await on_agent_turn_after(
        {
            "agent_id": "agent-dfb2",
            "open_obligations": [],
            "tool_calls": [
                {"function": {"name": "commit_turn",
                              "arguments": '{"phase":"waiting"}'}}
            ],
            "tasks_advanced": [],
            "phase": "waiting",
            "disposition": "runnable",
            "gate_repairing": False,
            "continue_slice": False,
            "deferred": False,
        },
        out,
    )
    assert defer_reason_streak("agent-dfb2") == 3
    assert defer_breaker_tripped("agent-dfb2")


def test_decide_nudge_tripped_overrides_deferred():
    """deferred=True 本应跳过催办；但断路器已跳闸 → 催办必须回来。"""
    hint, skip = decide_task_advance_nudge(
        open_obligations=[{"id": "t-1", "title": "T", "role_hint": "assignee",
                           "status": "running"}],
        tool_calls=[],
        tasks_advanced=set(),
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
        deferred=True,
        defer_breaker_tripped=True,
    )
    assert hint is not None
    assert "[TASK ADVANCE]" in hint


def test_decide_nudge_deferred_still_mutes_when_not_tripped():
    hint, skip = decide_task_advance_nudge(
        open_obligations=[{"id": "t-1", "title": "T", "role_hint": "assignee",
                           "status": "running"}],
        tool_calls=[],
        tasks_advanced=set(),
        phase="done_slice",
        disposition="runnable",
        gate_repairing=False,
        continue_slice=False,
        deferred=True,
        defer_breaker_tripped=False,
    )
    assert hint is None
    assert skip == "deferred"
