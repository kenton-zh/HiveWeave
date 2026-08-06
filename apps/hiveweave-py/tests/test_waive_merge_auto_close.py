"""BUG-MERGE-WAIVE-CLOSE 回归：waive_merge 后 approved 任务自动闭环。

8af5d22 引入：approve 时 worktree 仍有 ahead 未 auto-close，CEO 随后
waive merge → 任务永远停 approved 95%，无任何在册义务触发后续动作。
修复后 waive_merge 是最后一个能一致化账本的位置：任务已 approved 时
直接代闭环（close_task 内部 merge gate 因 merge_waived 放行）。

锁死三分支：
- approved → 自动 close_task(reason_code="merge_waived")，回执明示已闭环
- 非 approved（如 running）→ 不 close，回执提示先走 approve
- close 抛错 → 回执上报 close_err，不吞异常
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_PROJ = "proj"
_CEO = "ceo-1"
_REASON = "docs-only delivery already on main via prior commit abc123"


def _params():
    return SimpleNamespace(task_id="task-1", reason=_REASON)


async def _drive(task_status, close_side_effect=None):
    """驱动 waive_merge_tool，返回 (result, ts_mock, stamped_evidence)。"""
    stamped: dict = {}

    async def _fake_execute(project_id, sql, args):
        import json as _json

        stamped["evidence"] = _json.loads(args[0])

    task = {
        "id": "task-1",
        "status": task_status,
        "evidence": {},
        "title": "docs",
    }
    with (
        patch(
            "hiveweave.tools.helpers.get_project_id",
            new_callable=AsyncMock,
            return_value=_PROJ,
        ),
        patch("hiveweave.services.task.TaskService") as TS,
        patch(
            "hiveweave.services.task._execute",
            new_callable=AsyncMock,
            side_effect=_fake_execute,
        ),
        patch(
            "hiveweave.services.obligation.ObligationLedger.fulfill",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        ts = TS.return_value
        ts.get_task = AsyncMock(return_value=task)
        ts._is_verify_task = lambda t: False
        ts.close_task = AsyncMock(side_effect=close_side_effect)

        from hiveweave.tools.tasks.waive import waive_merge_tool

        result = await waive_merge_tool(_params(), _CEO, "/tmp/ws")
    return result, ts, stamped


@pytest.mark.asyncio
async def test_approved_task_auto_closed():
    """approved + waive_merge → close_task(reason_code=merge_waived) 代闭环。"""
    result, ts, stamped = await _drive("approved")
    assert result.success is True
    ts.close_task.assert_awaited_once()
    kw = ts.close_task.await_args.kwargs
    assert kw.get("reason_code") == "merge_waived"
    assert "auto-closed" in result.output
    # merge_waived 审计戳仍先落库（close 的 merge gate 靠它放行）
    assert stamped["evidence"].get("merge_waived") is True
    assert stamped["evidence"].get("merge_waived_by") == _CEO


@pytest.mark.asyncio
async def test_non_approved_task_not_closed():
    """running 任务 waive_merge → 不 close，回执提示先 approve。"""
    result, ts, _stamped = await _drive("running")
    assert result.success is True
    ts.close_task.assert_not_awaited()
    assert "not yet approved" in result.output


@pytest.mark.asyncio
async def test_close_failure_reported_not_swallowed():
    """close_task 抛错 → 回执含 close_err，任务保持 approved 文案一致。"""
    result, _ts, _stamped = await _drive(
        "approved", close_side_effect=RuntimeError("transition boom")
    )
    assert result.success is True
    assert "Auto-close failed" in result.output
    assert "transition boom" in result.output
