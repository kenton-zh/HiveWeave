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
    appeal_notes: str | None = Field(
        default=None,
        alias="appealNotes",
        description=(
            "作者申诉（可选）：你认为 diff 中某些行为是任务规格/验收标准"
            "要求而非缺陷时，附规格原文与理由。仅参考——审计会独立核实，"
            "不因申诉自动放行。"
        ),
        json_schema_extra={"aliases": ["appealNotes", "appeal_notes", "appeal"]},
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
    """短契约：审计结论 / 行数 / top issues / 报告路径 / 凭证。

    审计 epic P0-1（42 轮报告）：缓存命中回执此前显示「ISSUES / 0 行 /
    0 问题」自相矛盾，agent 误判审计空转后 12 次重审赌结果 + 一次误导性
    豁免。现在缓存命中回放原 issue 列表 / 行数，并透出 message 与
    「缓存复用自凭证 X」——cached ISSUES 回执绝不能再出现「0 行/0 问题」。
    """
    # 遗留显示瑕疵（09-06 审计）：lines_audited 缺数据时显示「未知」而不是
    # 0——「0 行」会被 agent 误读为「审计了 0 行 = 空转」，触发无谓重审。
    _la = result.get("lines_audited")
    try:
        _la_shown: int | str = int(_la) if _la is not None else "未知（无数据，勿当 0 解读）"
    except (TypeError, ValueError):
        _la_shown = "未知（无数据，勿当 0 解读）"
    lines = [
        f"审计结论: {result.get('verdict') or 'UNKNOWN'}",
        f"审计行数: {_la_shown}",
    ]
    commit_hash = str(result.get("commit_hash") or "").strip()
    if commit_hash:
        lines.append(f"基于版本: HEAD {commit_hash[:12]}")
    if result.get("verdict") == "ISSUES":
        top = result.get("top_issues") or []
        if top:
            lines.append(f"问题数: {int(result.get('issues_count') or 0)}")
            for i, issue in enumerate(top, 1):
                lines.append(f"{i}. {issue}")
        elif result.get("cached_from_attestation_id") or result.get(
            "cached_diff_hash"
        ):
            # 审计 P1-2 修复：存量缓存行（升级前写入）top_issues 为 NULL，
            # 回放时 ISSUES 结论配「问题数: 0」自相矛盾——改为明示明细缺失。
            lines.append(
                "存量缓存无问题明细（升级前写入），如需明细请改代码使 "
                "diff 变化后重审"
            )
        else:
            lines.append(f"问题数: {int(result.get('issues_count') or 0)}")
        lines.append(
            "申诉通道：某发现若实为任务规格/验收标准要求的行为（如规格指定"
            "的默认 token），重新 request_code_audit 附 appealNotes（引用"
            "规格原文）走 finding 级申诉，不必改代码迎合。"
        )
    cached_from = result.get("cached_from_attestation_id")
    if cached_from:
        lines.append(
            f"缓存复用自凭证 {cached_from}：diff 未变，结论不变，"
            "重审无意义（同一 diff 只会得到同一结论）。"
        )
    message = result.get("message")
    if message:
        lines.append(str(message))
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
            appeal_notes=(params.appeal_notes or "").strip() or None,
        )
    except Exception as e:
        log.warning("request_code_audit.crashed", agent_id=agent_id, error=repr(e))
        return ToolResult.err(f"code audit failed: {e}")

    if not result.get("audited"):
        reason = result.get("reason") or "unknown"
        if reason == "llm_failed" and result.get("retry_queued"):
            # 审计 epic P1-4：失败已入队后台自动重试——回执改为重试队列
            # 口径，绝不再教 agent「反复失败就 waive」（42 轮实测 6/6
            # waive 级联全由此文案引发）。
            attempts = result.get("retry_attempts")
            if result.get("retry_exhausted"):
                return ToolResult.ok(
                    f"审计未执行: {reason}. 审计上游失败，自动重试已达上限"
                    f"（{attempts} 次）。平台不再自动重试——如确认上游长时间"
                    "不可用，可请 coordinator 走 waive_attestation（真实"
                    "人工决策）；否则稍后再重试 request_code_audit。"
                )
            if result.get("retry_resequence"):
                # 审计 P2 修复：耗尽后再人工重试会新插 attempts=1 行，
                # 措辞点明这是新一轮重试序列，与「平台不再自动重试」不打架。
                return ToolResult.ok(
                    f"审计未执行: {reason}. 此前的自动重试序列已耗尽；你本次"
                    f"人工重试已作为新一轮重试序列（第 {attempts} 次）排队，"
                    "成功后会通过收件箱通知你，无需申请豁免。"
                )
            return ToolResult.ok(
                f"审计未执行: {reason}. 审计上游失败，平台已排队自动重试"
                f"（第 {attempts} 次）；成功后会通过收件箱通知你，无需申请"
                "豁免，也无需循环重试 request_code_audit。等待重试通知即可，"
                "此期间不要提交（submit 门禁在审计凭证就绪前会拒绝）。"
            )
        # s3-clone_06 P0-1/P0-3：fail-loud 之后"直接 submit"会被门禁拒——
        # 旧文案（soft gate — does not block）误导 Agent 走一条必然失败的路。
        # 审计 epic P1-4：llm_failed 文案统一为重试队列口径（等待平台自动
        # 重试通知；waive 只是多次自动重试仍失败后的真实人工决策出口）。
        return ToolResult.ok(
            f"审计未执行: {reason}. "
            "Next: retry request_code_audit once（审计对真实 diff 需 30-90s）。"
            "llm_failed 属上游暂态时平台会自动排队重试并回填收件箱通知，"
            "等待即可；只有多次自动重试仍失败才考虑请 coordinator 走 "
            "waive_attestation(taskId=..., reason=...)（真实人工决策）。"
            "审计凭证就绪前 submit_task 会被门禁拦下。"
        )
    return _format_verdict(result)
