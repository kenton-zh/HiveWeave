"""run_smoke —— 交付级冒烟的**预跑通道**（s3-clone_07 GAP · 疏导设计）。

冒烟门在 submit 时强制执行（交付探针契约）；本工具让 agent 在提交**之前**
随时自查同一份探针——把门从"考官突袭"变成"考纲公开的考试"。

用法：`run_smoke(taskId="<里程碑任务id>")`。探针契约来自任务的
contract_json.acceptance 中 type=service_smoke 的条款（由设计者派任务时冻结）；
服务在**调用者自己的工作区**启动——实现者用它验证自己的工作树，coordinator
用它验证合并结果。
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.tools.base import tool
from hiveweave.tools.helpers import get_project_id

log = structlog.get_logger(__name__)


class RunSmokeParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        ...,
        alias="taskId",
        description=(
            "Milestone task id (copy whole from get_tasks listing). The task's "
            "contract_json must contain a service_smoke acceptance clause."
        ),
        json_schema_extra={"aliases": ["taskId", "task_id"]},
    )


@tool(
    "run_smoke",
    "Pre-submit delivery smoke check (自查通道). Boots the project service per "
    "the task's frozen service_smoke contract, runs the probe script against the "
    "real service (true protocol client), reports pass/fail + log tail. Use it "
    "BEFORE submit_task on milestone tasks so you never discover a broken "
    "delivery at the gate. Does NOT change task status.",
    requires_workspace=True,
    security_level="standard",
)
async def run_smoke_tool(
    params: RunSmokeParams, agent_id: str, workspace: str
) -> Any:
    from hiveweave.services.smoke_gate import run_service_smoke_clause
    from hiveweave.services.task import TaskService
    from hiveweave.services.task_contract import parse_contract

    project_id = await get_project_id(agent_id)
    if not project_id:
        return _err(f"Agent {agent_id} has no project")

    ts = TaskService()
    try:
        task = await ts.get_task(project_id, params.task_id)
    except Exception as e:  # noqa: BLE001
        return _err(f"Task not found: {e}")
    if not task:
        return _err(f"Task not found: {params.task_id}")

    contract = parse_contract(task.get("contract_json"))
    clause = None
    for c in (contract or {}).get("acceptance") or []:
        if isinstance(c, dict) and c.get("type") == "service_smoke":
            clause = c
            break
    if clause is None:
        return _err(
            "该任务契约里没有 service_smoke 条款——冒烟门不适用（非里程碑交付"
            "或设计者未配置探针）。可在设计定稿时由架构师在 dispatch 的 "
            "contractJson.acceptance 中加入 {type: service_smoke, script, "
            "startCommand, deps, timeout}。"
        )

    result, freeze = await run_service_smoke_clause(
        clause, workspace_root=workspace, frozen=contract.get("smoke_freeze")
    )
    # 冻结信息持久化（首次验证时间点），submit 门禁用同一份做防篡改比对
    if freeze:
        contract = dict(contract or {})
        contract["smoke_freeze"] = freeze
        try:
            await ts._persist_contract_json(project_id, params.task_id, contract)
        except Exception as e:  # noqa: BLE001 — 冻结持久化失败不阻断自查
            log.warning("smoke_freeze_persist_failed", error=str(e))

    lines = [
        f"[run_smoke] {'PASS ✓' if result.passed else 'FAIL ✗'} "
        f"clause={result.id}",
        result.message,
    ]
    if result.evidence:
        lines.append("--- probe output tail ---")
        lines.append(result.evidence)
    if not result.passed:
        lines.append(
            "修复后可再次 run_smoke 自查；submit_task 的门禁会跑同一份探针。"
        )
    text = "\n".join(lines)
    return _err(text) if not result.passed else _ok(text)


def _ok(text: str) -> Any:
    from hiveweave.tools.executor import ToolResult

    return ToolResult.ok(text)


def _err(text: str) -> Any:
    from hiveweave.tools.executor import ToolResult

    return ToolResult.err(text)
