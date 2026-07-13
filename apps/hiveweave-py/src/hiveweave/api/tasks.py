"""Task Ledger REST API (contract 19, task ledger group).

任务账本 HTTP 接口 — 围绕 TaskService 状态机:
    created → claimed → running → blocked/submitted → reviewing → approved/rework → closed

- GET    /api/projects/{project_id}/tasks            列表（可按 status/assignee 过滤）
- POST   /api/projects/{project_id}/tasks            创建
- GET    /api/projects/{project_id}/tasks/{task_id}  详情（含关联 work_logs）
- PATCH  /api/projects/{project_id}/tasks/{task_id}  更新可变字段
- POST   /api/projects/{project_id}/tasks/{task_id}/claim    认领
- POST   /api/projects/{project_id}/tasks/{task_id}/submit   提交（带 evidence）
- POST   /api/projects/{project_id}/tasks/{task_id}/review   审批（approve/rework）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import structlog

from hiveweave.services import dispatch as dispatch_svc
from hiveweave.services.task import TaskService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])

_tasks = TaskService()


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    assigneeId: str | None = None
    creatorId: str = "user"
    priority: int = 2
    dueAt: int | None = None
    acceptanceCriteria: list[dict] | None = None
    parentTaskId: str | None = None
    dependsOn: list[str] | None = None
    expectedModules: list[str] | None = None
    tags: list[str] | None = None
    source: str = "user"


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    assigneeId: str | None = None
    priority: int | None = None
    dueAt: int | None = None
    progress: int | None = None
    tags: list[str] | None = None
    expectedModules: list[str] | None = None


class TaskSubmit(BaseModel):
    evidence: dict[str, Any]


class TaskReview(BaseModel):
    decision: str  # "approve" or "rework"
    feedback: str | None = None


class TaskClaim(BaseModel):
    agentId: str


def _raise_from_value_error(e: ValueError) -> None:
    """Map ValueError to 404 (not found) or 400 (illegal transition)."""
    msg = str(e)
    if "not found" in msg.lower():
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=400, detail=msg)


@router.get("")
async def list_tasks(
    project_id: str,
    status: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
) -> dict:
    """列出任务（可按 status / assignee 过滤，排除已归档）。"""
    tasks = await _tasks.list_tasks(
        project_id, status=status, assignee_id=assignee
    )
    return {"tasks": tasks}


@router.post("")
async def create_task(project_id: str, body: TaskCreate) -> dict:
    """创建任务。"""
    try:
        task_id = await _tasks.create_task(
            project_id,
            title=body.title,
            description=body.description or "",
            creator_id=body.creatorId,
            assignee_id=body.assigneeId,
            priority=body.priority,
            due_at=body.dueAt,
            acceptance_criteria=body.acceptanceCriteria,
            parent_task_id=body.parentTaskId,
            depends_on=body.dependsOn,
            expected_modules=body.expectedModules,
            tags=body.tags,
            source=body.source,
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return {"task_id": task_id, "success": True}


@router.get("/{task_id}")
async def get_task(project_id: str, task_id: str) -> dict:
    """查任务详情（含关联 work_logs）。"""
    task = await _tasks.get_task(project_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    # 查关联 work_logs（通过 DispatchService 公开 API）
    try:
        ds = dispatch_svc.DispatchService()
        logs = await ds.get_work_logs_for_task(project_id, task_id)
        task["work_logs"] = logs
    except Exception as e:
        log.warning("get_task_work_logs_failed", task_id=task_id, error=str(e))
        task["work_logs"] = []
    return task


@router.patch("/{task_id}")
async def update_task(project_id: str, task_id: str, body: TaskUpdate) -> dict:
    """部分更新（仅传入字段）。progress 走 update_progress 单独处理。"""
    raw = body.model_dump(exclude_none=True)
    progress = raw.pop("progress", None)
    if progress is not None:
        try:
            await _tasks.update_progress(project_id, task_id, progress)
        except ValueError as e:
            _raise_from_value_error(e)
    # camelCase → snake_case 映射（TaskService.update_task 用 snake_case）
    camel_to_snake = {
        "assigneeId": "assignee_id",
        "dueAt": "due_at",
        "expectedModules": "expected_modules",
    }
    fields = {camel_to_snake.get(k, k): v for k, v in raw.items()}
    if fields:
        await _tasks.update_task(project_id, task_id, **fields)
    return {"success": True}


@router.post("/{task_id}/claim")
async def claim_task(project_id: str, task_id: str, body: TaskClaim) -> dict:
    """认领任务（created → claimed → running，一步到位）。"""
    try:
        await _tasks.claim_task(project_id, task_id, body.agentId)
        # 认领即开始执行，自动推进到 running
        await _tasks.start_task(project_id, task_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}


@router.post("/{task_id}/submit")
async def submit_task(project_id: str, task_id: str, body: TaskSubmit) -> dict:
    """提交任务（running → submitted），附 evidence。"""
    try:
        await _tasks.submit_task(project_id, task_id, body.evidence)
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}


@router.post("/{task_id}/review")
async def review_task(project_id: str, task_id: str, body: TaskReview) -> dict:
    """审批任务（submitted/reviewing → approved/rework）。

    decision='approve' → approved; decision='rework' → rework → running.
    若任务处于 submitted，自动先推进到 reviewing。
    """
    try:
        # Check current status and only call start_review when needed,
        # instead of try/except pass which silently swallows real errors.
        task = await _tasks.get_task(project_id, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        current_status = task["status"]
        if current_status == "submitted":
            await _tasks.start_review(project_id, task_id)  # submitted → reviewing
        elif current_status != "reviewing":
            raise HTTPException(
                status_code=400,
                detail=f"Task must be 'submitted' or 'reviewing' to review, "
                       f"but is '{current_status}'",
            )
        await _tasks.review_task(
            project_id, task_id, body.decision, body.feedback
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}
