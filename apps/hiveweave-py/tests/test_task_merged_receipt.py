"""T4.4：MERGE_LANDED 回执 —— task.merged 事件 + relay 转 inbox。

此前 merge 成功后零 task_events 写入（_stamp_merge_fact_on_parent_tasks
直接 UPDATE tasks SET evidence，绕过 _transition），下游只能靠反复跑
「核验主分支落地状态」的 run 确认（TEST_DSH_35 实测 6 个重复 run，
等待—超时—重建循环的根源之一）。
"""
from __future__ import annotations

import json

import pytest

from hiveweave.services.task_event_relay import TaskEventRelay


def _task(assignee="agent-exec", creator="agent-coord"):
    return {"id": "t-full-uuid-1", "assignee_id": assignee,
            "creator_id": creator, "title": "实现井字棋"}


async def test_merged_event_recipients_cover_assignee_and_creator():
    relay = TaskEventRelay()
    # actor = 第三方合并人（如 CEO 收口）：assignee 与 creator 都收
    recipients = await relay._determine_recipients(
        "proj-1", "task.merged", "t-full-uuid-1", "agent-ceo", {},
        task=_task(),
    )
    assert "agent-exec" in recipients
    assert "agent-coord" in recipients
    # actor = creator（coordinator 自己 merge 自己创建的任务）→ 不自通知
    recipients_self = await relay._determine_recipients(
        "proj-1", "task.merged", "t-full-uuid-1", "agent-coord", {},
        task=_task(creator="agent-coord"),
    )
    assert "agent-coord" not in recipients_self
    assert "agent-exec" in recipients_self


async def test_merged_message_carries_receipt_payload():
    relay = TaskEventRelay()
    msg = relay._build_message(
        "task.merged",
        "t-full-uuid-1",
        {
            "merge_commit": "abcdef1234567890",
            "files": ["src/a.ts", "src/b.ts"],
            "target_branch": "main",
        },
        title="实现井字棋",
    )
    assert "[MERGE LANDED]" in msg
    assert "abcdef123456" in msg          # commit hash（12 位截断）
    assert "target=main" in msg
    assert "src/a.ts" in msg and "src/b.ts" in msg


async def test_merged_message_tolerates_sparse_payload():
    relay = TaskEventRelay()
    msg = relay._build_message("task.merged", "t-full-uuid-2", {}, title="x")
    assert "[MERGE LANDED]" in msg        # 不炸即可（payload 可能缺字段）
