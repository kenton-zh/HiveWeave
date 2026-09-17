"""claim / update_task_status / update_progress tools

Split from tools/task_tools.py (AI-friendly package layout). Behavior unchanged.
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.services import task as _task_svc
from hiveweave.tools.base import tool
from hiveweave.tools import helpers as _helpers

_coerce_to_list = _helpers.coerce_to_list
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)


def _parse_wake_at_ms(value: str | int | None) -> int | None:
    """Parse wakeAt (ISO-8601 or epoch ms) → epoch ms. None on unparseable.

    Epoch **seconds** are auto-detected: any positive value < 10^11 ms is
    older than 1973 as ms — realistically seconds — so it is scaled to ms
    (otherwise it would silently parse as an already-expired deadline).
    """
    if value is None:
        return None
    if isinstance(value, int):
        v_int = value
        if 0 < v_int < 10**11:
            v_int *= 1000
        return v_int
    text = str(value).strip()
    try:
        v_int = int(text)
        if 0 < v_int < 10**11:
            v_int *= 1000
        return v_int
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # naive ISO 按 UTC 解析（agent 通常发 UTC 时间）
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None

# ── claim_task ──────────────────────────────────────────


class ClaimTaskParams(BaseModel):
    """Parameters for claim_task tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to claim.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


@tool(
    "claim_task",
    "Pick up an unassigned draft (created → claimed). Not needed when create_task/"
    "dispatch already set assigneeId — assign is claim.",
    requires_workspace=False,
    security_level="standard",
)
async def claim_task_tool(
    params: ClaimTaskParams, agent_id: str, workspace: str
) -> ToolResult:
    """Claim a task."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = _task_svc.TaskService()
        await ts.claim_task(project_id, params.task_id, agent_id)
        output = f"Task {params.task_id} claimed by you."
        # P1-7: 收口期望随认领下发（与 dispatch 同一份推导），不等 submit 被拒
        try:
            from hiveweave.services.tasks.policy import (
                format_submit_expectations,
            )

            block = format_submit_expectations(
                await ts.get_task(project_id, params.task_id)
            )
            if block:
                output += f"\n\n{block}"
        except Exception as e:
            log.debug("claim_submit_expectations_failed", error=str(e))
        return ToolResult.ok(output)
    except Exception as e:
        return ToolResult.err(f"Failed to claim task: {e}")


# ── update_task_status ──────────────────────────────────


class UpdateTaskStatusParams(BaseModel):
    """Parameters for update_task_status tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to update.",
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )
    status: str = Field(
        default="running",
        description="New status: 'running' (start/unblock) or 'blocked'. Defaults to 'running'.",
        json_schema_extra={"aliases": ["status", "state"]},
    )
    blocked_reason: str | None = Field(
        default=None,
        alias="blockedReason",
        description=(
            "Required when blocked. Human-readable note only — auto-unblock "
            "is declared via dependsOnTaskIds / wakeAt, never inferred from "
            "this text."
        ),
        json_schema_extra={"aliases": ["blockedReason", "blocked_reason", "reason"]},
    )
    depends_on_task_ids: list[str] | None = Field(
        default=None,
        alias="dependsOnTaskIds",
        description=(
            "When blocking on dependencies: the blocker task ids (list). "
            "Auto-unblocks when all of them are approved/closed. A block "
            "needs dependsOnTaskIds, wakeAt, or waitKind='user'/'external' — "
            "a block with none of these can never auto-unblock."
        ),
        json_schema_extra={
            "aliases": [
                "dependsOnTaskIds",
                "depends_on_task_ids",
                "dependsOnTaskId",
                "depends_on_task_id",
                "dependsOn",
            ]
        },
    )
    wait_kind: Literal["dependency", "timer", "user", "external"] | None = Field(
        default=None,
        alias="waitKind",
        description=(
            "Structured wait kind when blocking. Inferred from dependsOnTaskIds "
            "(dependency) or wakeAt (timer) when omitted. Use 'user' when "
            "waiting on a person's decision and 'external' when waiting on the "
            "outside world — these have no auto-unblock path, so the platform "
            "escalates them to your org parent instead. Never inferred from "
            "blockedReason text."
        ),
        json_schema_extra={"aliases": ["waitKind", "wait_kind"]},
    )
    wake_at: str | int | None = Field(
        default=None,
        alias="wakeAt",
        description=(
            "Deadline for timer waits: ISO-8601 datetime (naive = UTC) or "
            "epoch milliseconds. Auto-unblocks at this time."
        ),
        json_schema_extra={"aliases": ["wakeAt", "wake_at"]},
    )

    @field_validator("depends_on_task_ids", mode="before")
    @classmethod
    def _coerce_dep_ids(cls, v: Any) -> Any:
        """Accept a single id string or comma-separated list for convenience."""
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, str):
            return [s for s in (x.strip() for x in v.split(",")) if s]
        return v


@tool(
    "update_task_status",
    "Set task to 'running' (start/unblock) or 'blocked'. For 'running', "
    "unblocks if currently blocked (clears wait metadata), else starts.",
    requires_workspace=False,
    security_level="standard",
)
async def update_task_status_tool(
    params: UpdateTaskStatusParams, agent_id: str, workspace: str
) -> ToolResult:
    """Update task status (running or blocked)."""
    status = params.status.lower()
    if status not in ("running", "blocked"):
        return ToolResult.err(
            "update_task_status requires 'status' of 'running' or 'blocked'"
        )

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    ts = _task_svc.TaskService()
    try:
        if status == "blocked":
            reason = params.blocked_reason or "Blocked by agent"
            deps = params.depends_on_task_ids or []
            wake_ms = _parse_wake_at_ms(params.wake_at)
            if params.wake_at is not None and wake_ms is None:
                return ToolResult.err(
                    f"update_task_status: wakeAt={params.wake_at!r} is not a "
                    f"parseable ISO-8601 datetime or epoch-milliseconds value. "
                    f"Pass a valid wakeAt or dependsOnTaskIds for the "
                    f"auto-unblock path."
                )
            if params.wait_kind and params.wait_kind != "timer" and wake_ms is not None:
                return ToolResult.err(
                    f"update_task_status: wakeAt only applies to timer waits, "
                    f"but waitKind={params.wait_kind!r} was given. Drop one of "
                    f"them (wakeAt implies waitKind=timer)."
                )
            # OCR 评审（2026-09-17）：deps 与 user/external 语义矛盾 ——
            # 有未满足依赖就有自动解封路径（reconcile 解封），且升级判据
            # 对 deps 非空的任务**不升级**（「出口未到」≠「出口失效」）。
            # 同传会让回执谎称「无自动解封、会被升级」⇒ 工具层直接拒。
            if params.wait_kind in ("user", "external") and deps:
                return ToolResult.err(
                    f"update_task_status: waitKind={params.wait_kind!r} "
                    f"conflicts with dependsOnTaskIds — a task with unmet "
                    f"dependencies has an auto-unblock path (reconcile wakes "
                    f"it), i.e. a 'dependency' wait, not a person/external "
                    f"wait. Pass one of them."
                )
            # 2026-09-17（PLATFORM-ISSUES §11.6）：waitKind=user/external 是
            # **合法且必要**的出口 —— 它表达「等一个 agent 做裁决 / 等外部世界」，
            # 这类等待**没有机器可判的自动解封路径**，由平台的升级兜底负责
            # （obligations.arbitration → scan_overdue → org parent）。
            # 此前这两值虽在枚举里，却因下面「必须有 deps 或 wakeAt」的校验
            # **无法产出** ⇒ agent 只能把裁决等待**伪装成 timer**（现场
            # TEST_DSH_61 309e2489 实证），平台因此看不到真实意图。
            kind: str
            if params.wait_kind:
                kind = params.wait_kind
            elif deps:
                kind = "dependency"
            else:
                kind = "timer"
            if kind in ("user", "external"):
                # 无自动解封路径是有意的：这类 blocked 由升级兜底接手。
                pass
            elif not deps and wake_ms is None:
                return ToolResult.err(
                    "update_task_status: blocking requires an auto-unblock "
                    "path — pass dependsOnTaskIds (blocker task ids), "
                    "wakeAt (ISO-8601 or epoch-ms deadline), or "
                    "waitKind='user'/'external' (waiting on a person / the "
                    "outside world; the platform escalates these instead of "
                    "auto-unblocking). A block with none of these parks the "
                    "task for everyone waiting on it."
                )
            await ts.block_task(
                project_id,
                params.task_id,
                reason,
                depends_on_task_ids=deps,
                wait_kind=kind,
                wake_at=wake_ms,
            )
            if kind in ("user", "external"):
                return ToolResult.ok(
                    f"Task {params.task_id} blocked: {reason} (wait_kind={kind}; "
                    f"no auto-unblock path — the platform will escalate to your "
                    f"org parent if this stays blocked, so a decision is required)."
                )
            return ToolResult.ok(
                f"Task {params.task_id} blocked: {reason} (wait_kind={kind})"
            )
        # status == "running": deterministic by current state (TEST11 #5-L1)
        cur = await ts.get_task(project_id, params.task_id)
        cur_status = (cur or {}).get("status")
        if cur_status == "blocked":
            await ts.unblock_task(project_id, params.task_id)
            return ToolResult.ok(
                f"Task {params.task_id} unblocked (running)."
            )
        await ts.start_task(project_id, params.task_id)
        return ToolResult.ok(f"Task {params.task_id} started (running).")
    except Exception as e:
        return ToolResult.err(f"Failed to update task status: {e}")


# ── update_progress ─────────────────────────────────────


class UpdateProgressParams(BaseModel):
    """Parameters for update_progress tool."""
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId",
        description="ID of the task to update.",
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )
    progress: int = Field(
        description="Progress percentage (0-100).",
        json_schema_extra={"aliases": ["progress", "percent"]},
    )


@tool(
    "update_progress",
    "Set task progress (0-100). Does not change task status.",
    requires_workspace=False,
    security_level="standard",
)
async def update_progress_tool(
    params: UpdateProgressParams, agent_id: str, workspace: str
) -> ToolResult:
    """Update task progress."""
    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")
    try:
        ts = _task_svc.TaskService()
        await ts.update_progress(project_id, params.task_id, params.progress)
        return ToolResult.ok(
            f"Task {params.task_id} progress set to {params.progress}%."
        )
    except Exception as e:
        return ToolResult.err(f"Failed to update progress: {e}")

