"""duty 增强第一部分：task.blocked 通知附结构化解封路径说明。"""

from __future__ import annotations

from hiveweave.services.task_event_relay import TaskEventRelay


async def test_blocked_message_deps_path():
    relay = TaskEventRelay()
    hint = relay._blocked_unblock_hint({
        "depends_on": '["a1b2c3d4-1111-2222-3333-444444444444"]',
        "wait_kind": "dependency",
        "wake_at": None,
    })
    assert "解封路径：依赖 a1b2c3d4" in hint
    assert "reconcile 自动解封" in hint
    # 组装进通知正文
    msg = relay._build_message(
        "task.blocked", "t-1", {}, title="验收",
        unblock_hint=hint,
    )
    assert "[TASK BLOCKED]" in msg and "解封路径：依赖" in msg


async def test_blocked_message_timer_path():
    relay = TaskEventRelay()
    hint = relay._blocked_unblock_hint({
        "depends_on": [],
        "wait_kind": "timer",
        "wake_at": 1788500000000,
    })
    assert "解封路径：timer 到期" in hint
    assert "自动解封" in hint


async def test_blocked_message_no_path_warns():
    relay = TaskEventRelay()
    hint = relay._blocked_unblock_hint({
        "depends_on": None,
        "wait_kind": None,
        "wake_at": None,
    })
    assert "无自动解封" in hint
    assert "reassign / unblock / cancel" in hint
    assert "永久 parked" in hint


async def test_blocked_message_without_hint_unchanged():
    relay = TaskEventRelay()
    msg = relay._build_message("task.blocked", "t-1", {}, title="x")
    assert "[TASK BLOCKED]" in msg
    assert "解封路径" not in msg  # 未传 hint 时正文不变
