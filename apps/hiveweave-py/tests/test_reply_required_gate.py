"""P1(TEST9) reply_required 退出门禁 — collect_unreplied_asks 强化回归。

背景：inbox 消息带 reply_required（expect_report=1 / message_type=ask），
agent 只输出 assistant 文字、未真正 message sender，仍以 done_slice 退出，
对方 agent_waits(wake_on=ask_reply/message_from_ref) 永远不满足。

强化点（全部基于结构化字段，不猜文案）：
1. 回复证据扩展：inbox 落库记录（extra_replied_to）= send_message 成功送达。
2. 回复工具集合补全：message_peer/team/subordinate/user 也算回复。
3. 豁免边界：sender 已归档/不存在/user/system 不死锁门禁。
"""

from __future__ import annotations

import pytest

from hiveweave.services.turn_exit import (
    ExitContext,
    collect_unreplied_asks,
    evaluate_turn_exit,
)
from hiveweave.services.turn_session import (
    clear_pending_turn_result,
    set_pending_turn_result,
)

AGENT = "agent-under-test"
SENDER = "sender-1"


def _ask_msg(fid: str = SENDER, mid: str = "msg-1") -> dict:
    return {
        "id": mid,
        "from_agent_id": fid,
        "message": "please report back",
        "expect_report": 1,
        "message_type": "ask",
    }


def _tc_msg_tool(name: str, recipients: list[str]) -> dict:
    import json

    return {
        "id": "call-1",
        "function": {
            "name": name,
            "arguments": json.dumps({"recipients": recipients}),
        },
    }


@pytest.fixture(autouse=True)
def _clean_turn_session():
    clear_pending_turn_result(AGENT)
    yield
    clear_pending_turn_result(AGENT)


# ── collect_unreplied_asks ────────────────────────────────


def test_unreplied_when_no_message_tool_called():
    """只输出 assistant 文字（无消息工具调用）→ 必须判未回复。"""
    out = collect_unreplied_asks([_ask_msg()], tool_calls=[])
    assert len(out) == 1
    assert out[0]["from_agent_id"] == SENDER


def test_replied_via_send_message_tool_call():
    out = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[_tc_msg_tool("send_message", [SENDER])]
    )
    assert out == []


def test_replied_via_message_peer_tool_call():
    """message_peer 等 message 工具也算回复（此前工具名单缺失）。"""
    out = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[_tc_msg_tool("message_peer", [SENDER])]
    )
    assert out == []


def test_replied_via_inbox_delivery_evidence():
    """extra_replied_to（inbox 落库 = 成功送达证据）算回复。"""
    out = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[], extra_replied_to={SENDER}
    )
    assert out == []


def test_archived_sender_exempt():
    """sender 已归档/不存在 → 豁免，不得死锁门禁。"""
    out = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[], exempt_senders={SENDER}
    )
    assert out == []


def test_other_sender_still_unreplied():
    """回复了别人 ≠ 回复了 ask 的发送方。"""
    out = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[_tc_msg_tool("send_message", ["someone-else"])]
    )
    assert len(out) == 1


# ── evaluate_turn_exit 门禁集成 ───────────────────────────


def _commit_done_slice():
    set_pending_turn_result(
        AGENT,
        {
            "schema_version": 1,
            "phase": "done_slice",
            "summary": "finished slice",
            "waiting_on": [],
            "result": {},
            "extensions": {},
        },
    )


def test_gate_blocks_done_slice_with_unreplied_required():
    """有未回复的 reply_required 消息时 done_slice 被拒，提示含 sender。"""
    _commit_done_slice()
    unreplied = collect_unreplied_asks([_ask_msg()], tool_calls=[])
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id=AGENT,
            project_id="p1",
            tool_calls=[],
            pending_inbox_msgs=[_ask_msg()],
            unreplied_asks=unreplied,
        )
    )
    assert not decision.ok
    assert "UNREPLIED_ASKS" in decision.violations
    assert decision.should_repair  # 走 [TURN EXIT BLOCKED] 修复重跑
    assert "UNREPLIED_ASKS" in decision.hint or "回复" in decision.hint


def test_gate_passes_after_reply_delivered():
    """本 turn 已 message sender（inbox 证据）→ 正常退出。"""
    _commit_done_slice()
    unreplied = collect_unreplied_asks(
        [_ask_msg()], tool_calls=[], extra_replied_to={SENDER}
    )
    assert unreplied == []
    decision = evaluate_turn_exit(
        ExitContext(
            agent_id=AGENT,
            project_id="p1",
            tool_calls=[],
            pending_inbox_msgs=[_ask_msg()],
            unreplied_asks=unreplied,
        )
    )
    assert decision.ok
