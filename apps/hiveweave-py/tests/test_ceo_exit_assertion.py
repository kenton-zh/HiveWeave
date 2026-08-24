"""E4 补（复盘 P0-1 G4）：CEO 完结断言前账本一致性核验。

复盘最后 10 分钟：CEO 宣布 SHIP READY 时账面上有 2 个 approved 未 closed +
1 条未读 inbox + FAIL 终验被 swallow——任何「全部完成」类交付结论都应先过
账本一致性核验。本文件锁定 message_user 出口：
- CEO + 完结断言 + 账本不干净 → 拒绝并列出阻塞项；
- 干净账本 / 非 CEO / 普通进度消息 → 放行（fail-open）。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.tools.misc_tools import (
    MessageUserParams,
    message_user_tool,
)

CEO_ID = "ceo-exit-uuid"
PROJECT_ID = "test-ceo-exit-assertion"


@pytest.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.discard(PROJECT_ID)

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace_path": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def _ceo_row():
    return {"id": CEO_ID, "role": "ceo", "permission_type": "coordinator"}


async def _insert_task(ws, task_id, *, status, evidence=None):
    conn = await project_db.ensure_project_db(ws)
    now = int(time.time() * 1000)
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, creator_id, assignee_id,"
        " status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [task_id, PROJECT_ID, "t", CEO_ID, CEO_ID, status, now, now],
    )
    if evidence is not None:
        import json

        await conn.execute(
            "UPDATE tasks SET evidence = ? WHERE id = ?",
            [json.dumps(evidence), task_id],
        )
    await conn.commit()


def _send_patch(**overrides):
    """message_user 通路的最小 mock 集。overrides: unread / role 判定可覆盖。"""
    return [
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch(
            "hiveweave.services.org.OrgService.get_agent",
            new=AsyncMock(return_value=_ceo_row()),
        ),
        patch(
            "hiveweave.services.policy.infer_role_family",
            return_value=overrides.get("role", "ceo"),
        ),
        patch(
            "hiveweave.services.inbox.InboxService.get_unread_count",
            new=AsyncMock(return_value=overrides.get("unread", 0)),
        ),
        patch(
            "hiveweave.services.chat_message.ChatMessageService.save_message",
            new=AsyncMock(),
        ),
        patch(
            "hiveweave.realtime.event_bus.status_event_bus.publish_chat_message",
            new=AsyncMock(),
        ),
    ]


async def _send(message: str, ws: str, *, role: str = "ceo", unread: int = 0) -> object:
    from contextlib import ExitStack

    with ExitStack() as stack:
        for cm in _send_patch(role=role, unread=unread):
            stack.enter_context(cm)
        return await message_user_tool(
            MessageUserParams(message=message), CEO_ID, ws
        )


@pytest.mark.asyncio
async def test_ceo_completion_assertion_blocked_on_dirty_ledger(env):
    """CEO 发「全部完成」但项目有 open FAIL + approved 未 closed + 未读 → 拦截。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["/_admin 404"]},
    )
    await _insert_task(env["workspace_path"], "t-apr", status="approved")

    result = await _send(
        "项目已全部完成，可以交付使用", env["workspace_path"]
    )
    assert result.success is False
    err = result.error or ""
    assert "账本一致性核验" in err
    assert "FAIL 终验" in err
    assert "approved 未 closed" in err


@pytest.mark.asyncio
async def test_ceo_completion_assertion_blocks_on_unread_inbox(env):
    """仅 CEO 自身未读 inbox 也足以拦截完结断言。"""
    result = await _send(
        "交付完成，全部搞定", env["workspace_path"], unread=3
    )
    assert result.success is False
    assert "3 条未读消息" in (result.error or "")


@pytest.mark.asyncio
async def test_ceo_completion_assertion_allowed_on_clean_ledger(env):
    """账本干净 → 完结断言正常发送。"""
    result = await _send("项目已全部完成，可以交付使用", env["workspace_path"])
    assert result.success is True, result.error


@pytest.mark.asyncio
async def test_non_ceo_completion_assertion_not_blocked(env):
    """非 CEO 角色不受该核验约束（fail-open）。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["x"]},
    )
    result = await _send(
        "模块已全部完成", env["workspace_path"], role="coordinator"
    )
    assert result.success is True, result.error


@pytest.mark.asyncio
async def test_ceo_progress_update_not_blocked(env):
    """普通进度汇报（非完结断言）即使账本不干净也放行。"""
    await _insert_task(
        env["workspace_path"],
        "t-fail",
        status="running",
        evidence={"verdict": "FAIL", "blocking_issues": ["x"]},
    )
    result = await _send("当前进度：3/5 模块已合入 MAIN", env["workspace_path"])
    assert result.success is True, result.error


def test_e4_fourth_query_present_source_guard():
    """E4 变异替代：源码级存在性守卫——删第四查询本测试即红。

    E4 验收提到「变异测试（删第四查询 → 测试红）」；变异操作不适合放进
    常规单测运行，此处以源码断言等价防回归（CEO 出口核验同样复用该查询）。
    """
    from pathlib import Path

    import hiveweave.services.turn_exit as te

    src = Path(te.__file__).read_text(encoding="utf-8")
    assert "upper(json_extract(evidence, '$.verdict')) = 'FAIL'" in src