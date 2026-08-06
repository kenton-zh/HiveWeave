"""force-approve 逃生门端点回归（abacfa9 引入 + 2026-08-05 审计 P2 加固）。

端点：POST /api/projects/{pid}/tasks/{tid}/force-approve（human-operator 通道）。

锁死行为：
- reason 为空 → 400
- 任务不存在 → 404
- 非 submitted/reviewing → 400
- 死锁探针确认死锁 → submitted 先 start_review 再 approve，审计戳齐备
- 探针未确认死锁且无 confirm → 409（防把逃生门当常规审批捷径）
- 探针未确认死锁但 confirm=true + operator 归因 → 放行
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from hiveweave.api import tasks as tasks_api


def _body(**kw):
    base = {"reason": "approval deadlock: sole reviewer waived already"}
    base.update(kw)
    return tasks_api.TaskForceApprove(**base)


def _task(status="submitted"):
    return {
        "id": "task-1",
        "status": status,
        "title": "blocked task",
        "assignee_id": "exec-1",
        "evidence": {},
    }


def _mock_tasks(task):
    svc = AsyncMock()
    svc.get_task = AsyncMock(return_value=task)
    return svc


async def _call(body, task, deadlock="no lawful approver exists"):
    svc = _mock_tasks(task)
    with (
        patch.object(tasks_api, "_tasks", svc),
        patch(
            "hiveweave.services.unblock_soft.no_lawful_approver",
            new_callable=AsyncMock,
            return_value=deadlock,
        ),
    ):
        return await tasks_api.force_approve_task("proj", "task-1", body), svc


@pytest.mark.asyncio
async def test_empty_reason_rejected():
    with pytest.raises(HTTPException) as ei:
        await _call(_body(reason="   "), _task())
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_task_not_found():
    with pytest.raises(HTTPException) as ei:
        await _call(_body(), None)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_wrong_status_rejected():
    for status in ("running", "approved", "closed", "created"):
        with pytest.raises(HTTPException) as ei:
            await _call(_body(), _task(status))
        assert ei.value.status_code == 400, status


@pytest.mark.asyncio
async def test_deadlock_submitted_full_path():
    """死锁确认 + submitted → start_review + approve，审计戳 human-operator。"""
    resp, svc = await _call(_body(note="E2E feature-test"), _task("submitted"))
    assert resp["success"] is True
    assert resp["deadlock"]
    svc.start_review.assert_awaited_once()
    assert svc.start_review.await_args.kwargs["reviewer_id"] == "human-operator"
    svc.review_task.assert_awaited_once()
    kw = svc.review_task.await_args
    assert kw.args[2] == "approve"
    assert kw.kwargs["reviewer_id"] == "human-operator"
    feedback = kw.args[3]
    assert "operator-force-approve" in feedback
    assert "E2E feature-test" in feedback
    assert "deadlock:" in feedback


@pytest.mark.asyncio
async def test_reviewing_skips_start_review():
    """reviewing 状态不再 start_review（非法转移）。"""
    resp, svc = await _call(_body(), _task("reviewing"))
    assert resp["success"] is True
    svc.start_review.assert_not_awaited()
    svc.review_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_deadlock_requires_confirm():
    """P2-3：探针未确认死锁 + 无 confirm → 409。"""
    with pytest.raises(HTTPException) as ei:
        await _call(_body(), _task(), deadlock=None)
    assert ei.value.status_code == 409
    assert "confirm=true" in ei.value.detail


@pytest.mark.asyncio
async def test_no_deadlock_with_confirm_and_operator_attribution():
    """P2-3/P3-1：confirm=true + operator 字段 → 放行且可归因。"""
    resp, svc = await _call(
        _body(confirm=True, operator="kenton"), _task(), deadlock=None
    )
    assert resp["success"] is True
    kw = svc.review_task.await_args
    assert kw.kwargs["reviewer_id"] == "human-operator:kenton"
