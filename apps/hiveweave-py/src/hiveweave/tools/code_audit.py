"""request_code_audit tool — one-shot second-pass LLM audit of the worktree diff.

出口合同前置：累计代码变更超过 CODE_AUDIT_LINE_THRESHOLD(20) 行时必须先
request_code_audit 再 submit_task。审计实现/台账在 services/code_audit.py
（并行拆分，独立模块）——此处只做工具壳：参数解析、agent 身份/任务解析、
结果短契约格式化。审计 LLM 走 ctx.oneshot_llm_callback（与 review 套件同一条
一次性 HTTP 路径），模型从本项目在职队友当前解析到的模型里选一个
vendor model_id 与作者不同的；团队只有一种模型时退回作者自己的。
审计是只读分析 + 有成本 LLM 调用，软失败（无 worktree / 无回调 / 无模型 /
LLM 失败 / 内部错误）一律回 ToolResult.ok 带 reason，仅意外异常回 err。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, ConfigDict, Field

from hiveweave.tools import helpers as _helpers
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult

log = structlog.get_logger(__name__)


class RequestCodeAuditParams(BaseModel):
    """Parameters for request_code_audit tool."""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description=(
            "ID of the task whose changes are audited. If omitted, "
            "auto-detects your current running task."
        ),
        json_schema_extra={"aliases": ["taskId", "task_id", "id"]},
    )


async def _resolve_task_id(project_id: str, agent_id: str) -> str | None:
    """Auto-detect current running/claimed task（submit_task 同款逻辑）。

    只取唯一活动任务；无/多个活动任务时返回 None，审计仍可在 worktree 级
    运行（run_code_audit 接受 task_id=None）。
    """
    from hiveweave.services import task as _task_svc

    ts = _task_svc.TaskService()
    tasks = await ts.list_tasks(project_id, assignee_id=agent_id)
    active = [t for t in tasks if t.get("status") in ("running", "claimed")]
    if len(active) == 1:
        return active[0]["id"]
    if len(active) > 1:
        log.info(
            "request_code_audit.multiple_active_tasks",
            agent_id=agent_id,
            count=len(active),
        )
    return None


def _format_verdict(result: dict) -> ToolResult:
    """短契约：审计结论 / 行数 / top issues / 报告路径 / 凭证。"""
    lines = [
        f"审计结论: {result.get('verdict') or 'UNKNOWN'}",
        f"审计行数: {int(result.get('lines_audited') or 0)}",
    ]
    if result.get("verdict") == "ISSUES":
        lines.append(f"问题数: {int(result.get('issues_count') or 0)}")
        for i, issue in enumerate(result.get("top_issues") or [], 1):
            lines.append(f"{i}. {issue}")
    model_id = result.get("audit_model_id")
    source = result.get("audit_model_source")
    if model_id:
        if source == "peer":
            lines.append(f"审计模型: {model_id} (团队其它)")
        else:
            lines.append(f"审计模型: {model_id} (本模型；团队无其它)")
    report_path = result.get("report_path")
    if report_path:
        lines.append(f"报告: {report_path}")
    attestation_id = result.get("attestation_id")
    if attestation_id:
        lines.append(f"凭证: {attestation_id}")
    return ToolResult.ok("\n".join(lines))


@tool(
    "request_code_audit",
    "One-shot second-pass LLM audit of your worktree git diff. "
    "REQUIRED before submit_task when your cumulative code edits exceed 20 lines. "
    "Returns VERDICT PASS/ISSUES + top issues; full report persisted to disk. "
    "The audit runs as a one-shot sub-call. It uses a teammate's currently "
    "used model when that model differs from yours; otherwise your own model.",
    requires_workspace=False,
    security_level="standard",
)
async def request_code_audit_tool(
    params: RequestCodeAuditParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Run one-shot code audit on the agent's worktree diff (short contract)."""
    from hiveweave.services import code_audit as _code_audit

    project_id = await _helpers.get_project_id(agent_id)
    if not project_id:
        return ToolResult.err(f"Agent {agent_id} has no project")

    task_id = params.task_id
    if not task_id:
        try:
            task_id = await _resolve_task_id(project_id, agent_id)
        except Exception as e:  # noqa: BLE001 — 解析失败降级为 worktree 级审计
            log.info("request_code_audit.task_resolve_failed", agent_id=agent_id, error=str(e))
            task_id = None
    call_llm = getattr(ctx, "review_llm_callback", None) if ctx else None
    oneshot_llm = getattr(ctx, "oneshot_llm_callback", None) if ctx else None

    try:
        result = await _code_audit.run_code_audit(
            project_id, agent_id, task_id,
            call_llm=call_llm,
            oneshot_llm=oneshot_llm,
        )
    except Exception as e:
        log.warning("request_code_audit.crashed", agent_id=agent_id, error=repr(e))
        return ToolResult.err(f"code audit failed: {e}")

    if not result.get("audited"):
        reason = result.get("reason") or "unknown"
        # s3-clone_06 P0-1/P0-3：fail-loud 之后"直接 submit"会被门禁拒——
        # 旧文案（soft gate — does not block）误导 Agent 走一条必然失败的路。
        return ToolResult.ok(
            f"审计未执行: {reason}. "
            "Next: retry request_code_audit（审计对真实 diff 需 30-90s，"
            "超时帽见 CODE_AUDIT_LLM_TIMEOUT_S）；若反复失败，submit_task "
            "会被门禁拦下，需请 coordinator 走 "
            "waive_attestation(taskId=..., reason=...) 并附上你已有的替代"
            "证据（如 test_run）。"
        )
    return _format_verdict(result)
