"""Task Ledger REST API (contract 19, task ledger group).

任务账本 HTTP 接口 — 围绕 TaskService 状态机:
    created → claimed → running → blocked/submitted → reviewing → approved/rework → closed

P0 Hard Gates: submit/review enforce attestation; create may require actor capability.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import structlog

from hiveweave.services import dispatch as dispatch_svc
from hiveweave.services.attestation import (
    attestation_service,
    required_attestation_kinds,
    resolve_task_policy,
)
from hiveweave.services.org import OrgService
from hiveweave.services.policy import policy_service
from hiveweave.services.task import TaskService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])

_tasks = TaskService()
_org = OrgService()


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
    actorAgentId: str | None = None


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
    actorAgentId: str | None = None


class TaskReview(BaseModel):
    decision: str  # "approve" or "rework"
    feedback: str | None = None
    actorAgentId: str | None = None


class TaskClaim(BaseModel):
    agentId: str


def _raise_from_value_error(e: ValueError) -> None:
    """Map ValueError to 404 (not found) or 400 (illegal transition)."""
    msg = str(e)
    if "not found" in msg.lower():
        raise HTTPException(status_code=404, detail=msg)
    raise HTTPException(status_code=400, detail=msg)


async def _gate_attestation_for_task(
    project_id: str, task: dict, evidence: dict
) -> None:
    tags = task.get("tags") or []
    if isinstance(tags, str):
        import json

        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    policy_id = (
        evidence.get("policy_id")
        or task.get("policy_id")
        or resolve_task_policy(
            title=task.get("title") or "",
            tags=tags if isinstance(tags, list) else [],
            description=task.get("description") or "",
        )
    )
    needed = required_attestation_kinds(policy_id)
    if not needed:
        return
    aids = evidence.get("attestation_ids") or []
    if not isinstance(aids, list):
        aids = []
    ok, err = await attestation_service.verify_ids(
        project_id,
        [str(x) for x in aids],
        expected_kinds=needed,
        task_id=task.get("id"),
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"Attestation gate failed ({policy_id}): {err}",
        )


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
    """创建任务。Agent actors need dispatch capability; user creator is open."""
    creator = body.creatorId or "user"
    actor_id = (body.actorAgentId or "").strip()
    if actor_id or (creator and creator not in ("user", "用户", "human")):
        aid = actor_id or creator
        actor = await _org.resolve_agent(aid)
        if actor is None:
            raise HTTPException(status_code=404, detail=f"Actor not found: {aid}")
        hard = policy_service.hard_check(actor, "create_task", {})
        if hard:
            raise HTTPException(status_code=403, detail=hard)
        creator = actor.get("id") or creator

    try:
        task_id = await _tasks.create_task(
            project_id,
            title=body.title,
            description=body.description or "",
            creator_id=creator,
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
        await _tasks.start_task(project_id, task_id)
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}


@router.post("/{task_id}/submit")
async def submit_task(project_id: str, task_id: str, body: TaskSubmit) -> dict:
    """提交任务（running → submitted），附 evidence + attestation gate."""
    task = await _tasks.get_task(project_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    evidence = dict(body.evidence or {})
    await _gate_attestation_for_task(project_id, task, evidence)
    try:
        await _tasks.submit_task(project_id, task_id, evidence)
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}


@router.post("/{task_id}/review")
async def review_task(project_id: str, task_id: str, body: TaskReview) -> dict:
    """审批任务（submitted/reviewing → approved/rework）。"""
    actor_id = (body.actorAgentId or "").strip()
    if not actor_id:
        raise HTTPException(
            status_code=400,
            detail="actorAgentId is required to review tasks",
        )
    actor = await _org.resolve_agent(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail=f"Actor not found: {actor_id}")
    hard = policy_service.hard_check(actor, "review_task", {})
    if hard:
        raise HTTPException(status_code=403, detail=hard)

    try:
        task = await _tasks.get_task(project_id, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if body.decision.lower() == "approve":
            evidence = task.get("evidence") or {}
            if isinstance(evidence, str):
                import json

                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            await _gate_attestation_for_task(project_id, task, evidence)
            if task.get("assignee_id"):
                try:
                    from hiveweave.services.git_worktree import ensure_executor_worktree

                    await ensure_executor_worktree(
                        project_id, str(task["assignee_id"])
                    )
                except Exception:
                    pass
            from hiveweave.services.worktree_review import review_worktree_gate

            wt_deny, _ = await review_worktree_gate(project_id, task, evidence)
            if wt_deny:
                raise HTTPException(status_code=400, detail=wt_deny)
        current_status = task["status"]
        decision = body.decision.lower()
        if decision == "approve":
            if current_status == "submitted":
                await _tasks.start_review(project_id, task_id)
            elif current_status != "reviewing":
                raise HTTPException(
                    status_code=400,
                    detail=f"Task must be 'submitted' or 'reviewing' to approve, "
                           f"but is '{current_status}'",
                )
        else:
            if current_status == "submitted":
                await _tasks.start_review(project_id, task_id)
            elif current_status not in ("reviewing", "approved"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Task must be 'submitted', 'reviewing', or 'approved' "
                           f"to rework, but is '{current_status}'",
                )
        await _tasks.review_task(
            project_id, task_id, body.decision, body.feedback
        )
    except ValueError as e:
        _raise_from_value_error(e)
    return {"success": True}
