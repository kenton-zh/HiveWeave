"""rework 后快速 re-submit 的 [TASK SUBMITTED] 判重吞唤醒竞态（TEST_DSH_26）.

被测: services/inbox.py send_message 幂等键判重分支

事故形态（2026-08-22 TEST_DSH_26）: 星轨 21:21 被 rework 打回 → 21:23
re-submit。submit 直推的 [TASK SUBMITTED] 与上一轮同幂等键（from/to/
task/message 全同）→ 判重吞掉（should_wake=False 静默 return）→
trigger_coordinator 同秒跑但查不到新消息（trigger_no_context）→ CEO 干等
到 relay 60s 补投或 10min watchdog。

修复: dedupe 命中且旧行**已读**（reviewer 已消费上一轮）且是 task 类
带 task_id → 改投新行（新 key 加时间戳序号）；旧行未读（真重复）维持吞。

变异验证: 删 rearm 分支（换回无条件 dedupe return）→ 用例 1 失败。
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.db.project import ensure_project_db
from hiveweave.services.inbox import InboxService

PROJECT_ID = "test-dsh26-dedupe"
CEO_ID = "test-dsh26-ceo"
EXEC_ID = "test-dsh26-exec"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_get_project_workspace(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        async def fake_get_agent_by_id(aid: str):
            return {"id": aid, "project_id": PROJECT_ID, "status": "active"}

        with patch(
            "hiveweave.db.meta.get_project_workspace",
            fake_get_project_workspace,
        ), patch(
            "hiveweave.db.meta.get_agent_by_id",
            fake_get_agent_by_id,
        ):
            project_db._agent_cache[CEO_ID] = workspace_path
            project_db._agent_cache[EXEC_ID] = workspace_path
            # 预建连接：get_project_db 走 cache-only 快路径（ws in _cache）
            await ensure_project_db(workspace_path)
            yield {"workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
            project_db._agent_cache.pop(CEO_ID, None)
            project_db._agent_cache.pop(EXEC_ID, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def _send_task_notice(env, task_id: str, *, from_id=EXEC_ID):
    return await InboxService().send_message(
        from_agent_id=from_id,
        to_agent_id=CEO_ID,
        message=(
            f"[TASK SUBMITTED] Task 'demo' has been submitted for your "
            f"review. Use review_task(taskId='{task_id}', "
            f"decision='approve'/'rework') to review."
        ),
        message_type="task",
        priority="normal",
        task_id=task_id,
        wake=True,
    )


async def test_resubmit_task_notice_rearms_after_read(env):
    """rework→re-submit：旧行已读 → 新行落库（wake 语义不丢）."""
    task_id = str(uuid.uuid4())
    r1 = await _send_task_notice(env, task_id)
    assert r1["deduped"] is False
    assert r1["should_wake"] is True

    # reviewer 消费了第一轮通知（mark read）并打回 rework
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute(
        "UPDATE inbox SET read=1 WHERE id = ?", [r1["id"]]
    )
    await conn.commit()

    # 同任务同文案快速 re-submit → 必须改投新行而非吞掉
    r2 = await _send_task_notice(env, task_id)
    assert r2["deduped"] is False, "已读旧行的 task 通知要重投新行（rearm）"
    assert r2["should_wake"] is True, "rearm 行必须保 wake 语义"

    rows = await conn.execute(
        "SELECT COUNT(*) AS c FROM inbox WHERE to_agent_id = ? "
        "AND message LIKE '[TASK SUBMITTED]%'",
        [CEO_ID],
    )
    row = await rows.fetchone()
    await rows.close()
    assert int(row["c"]) == 2


async def test_duplicate_task_notice_unread_still_dedupes(env):
    """旧行未读（真重复投递）→ 维持判重吞掉（防风暴）."""
    task_id = str(uuid.uuid4())
    r1 = await _send_task_notice(env, task_id)
    assert r1["should_wake"] is True

    r2 = await _send_task_notice(env, task_id)
    assert r2["deduped"] is True, "未读旧行的重复投递必须吞"
    assert r2["should_wake"] is False

    conn = await ensure_project_db(env["workspace_path"])
    rows = await conn.execute(
        "SELECT COUNT(*) AS c FROM inbox WHERE to_agent_id = ? "
        "AND message LIKE '[TASK SUBMITTED]%'",
        [CEO_ID],
    )
    row = await rows.fetchone()
    await rows.close()
    assert int(row["c"]) == 1


async def test_non_task_message_dedupes_even_if_read(env):
    """非 task 类（如 normal 文本）即使已读仍维持判重——rearm 只开给
    任务状态类通知（有明确的「状态变化重申」语义）。"""
    msg = "普通消息内容 " + "x" * 20
    r1 = await InboxService().send_message(
        from_agent_id=EXEC_ID,
        to_agent_id=CEO_ID,
        message=msg,
        message_type="normal",
    )
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE inbox SET read=1 WHERE id = ?", [r1["id"]])
    await conn.commit()

    r2 = await InboxService().send_message(
        from_agent_id=EXEC_ID,
        to_agent_id=CEO_ID,
        message=msg,
        message_type="normal",
    )
    assert r2["deduped"] is True
    assert r2["should_wake"] is False


async def test_explicit_idempotency_key_never_rearms(env):
    """审计 D：显式 idempotency_key 调用方（ship_nudge/verify_spawn 的
    「只通知一次」契约）不参与 rearm——已读后同 key 仍吞。"""
    task_id = str(uuid.uuid4())
    key = "ship-ready:test-anchor-1"
    send = dict(
        from_agent_id="system",
        to_agent_id=CEO_ID,
        message=f"[SHIP READY] anchor test task {task_id[:8]}",
        message_type="task",
        task_id=task_id,
        wake=True,
        idempotency_key=key,
    )
    r1 = await InboxService().send_message(**send)
    assert r1["deduped"] is False

    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE inbox SET read=1 WHERE id = ?", [r1["id"]])
    await conn.commit()

    r2 = await InboxService().send_message(**send)
    assert r2["deduped"] is True, "显式 key 的只通知一次契约不得被 rearm 击穿"
    assert r2["should_wake"] is False


async def test_rearm_row_unread_suppresses_third(env):
    """审计 B/F 收敛：rearm 新行未读时，第三次同文案 submit 不再叠加
    （reviewer 还没消费上一轮重申）——吞掉而非再投。"""
    task_id = str(uuid.uuid4())
    r1 = await _send_task_notice(env, task_id)
    conn = await ensure_project_db(env["workspace_path"])
    await conn.execute("UPDATE inbox SET read=1 WHERE id = ?", [r1["id"]])
    await conn.commit()

    r2 = await _send_task_notice(env, task_id)   # rearm 新行（未读）
    assert r2["deduped"] is False

    r3 = await _send_task_notice(env, task_id)   # rearm 行未读 → 吞
    assert r3["deduped"] is True, "未读 rearm 行存在时必须收敛"
    assert r3["should_wake"] is False

    rows = await conn.execute(
        "SELECT COUNT(*) AS c FROM inbox WHERE to_agent_id = ? "
        "AND message LIKE '[TASK SUBMITTED]%'",
        [CEO_ID],
    )
    row = await rows.fetchone()
    await rows.close()
    assert int(row["c"]) == 2, "两行封顶：原始 + 一次 rearm"
