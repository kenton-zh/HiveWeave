"""request_code_audit tool — one-shot second-pass LLM audit of the worktree diff.

出口合同前置：累计代码变更超过 CODE_AUDIT_LINE_THRESHOLD(20) 行时必须先
request_code_audit 再 submit_task。审计实现/台账在 services/code_audit.py
（并行拆分，独立模块）——此处只做工具壳：参数解析、agent 身份/任务解析、
结果短契约格式化。审计 LLM 走 ctx.oneshot_llm_callback（与 review 套件同一条
一次性 HTTP 路径），模型从本项目在职队友当前解析到的模型里选一个
vendor model_id 与作者不同的；团队只有一种模型时退回作者自己的。
审计是只读分析 + 有成本 LLM 调用；本工具**不 raise**（仅「agent 无项目」
回 err）。未审计的出口按**三态**映射（TEST_DSH_55 P0，契约见
``services/code_audit.py`` 顶部「审计三态」）：

  ready（有凭证）        ⇒ ``ToolResult.ok``，agent 可 submit。
  accepted_pending       ⇒ **已受理·等待结果**（llm_failed 已入队后台重试）：
  （等通知，不要重发）      ``success=False`` + ``fact=outcome_unknown`` +
                          ``blocked=True``，并把 ``audit_state`` /
                          ``wait_for_notice`` 写进结果契约。
  failed                 ⇒ ``success=False`` + ``fact``，``action_required=True``。

三者**都不靠回执文案**让 agent 判断该等还是该改 —— 文案只是信号的可读补充。
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
    # 三态契约的 ready 态标记（TEST_DSH_55 P0）：与 accepted_pending /
    # failed 同轴可读。lazy import 保持本模块「工具壳不牵 services 导入」的
    # 既有拓扑（见模块 docstring）。
    from hiveweave.services.code_audit import AUDIT_STATE_READY

    return ToolResult.ok("\n".join(lines), audit_state=AUDIT_STATE_READY)


def _soft_fail_receipt(result: dict) -> str:
    """未审计出口的可读回执（原文案**逐字保留**）。

    文案不是信号 —— 三态由 :func:`_soft_fail_result` 的结构化字段表达；
    这里只是给模型看的可读补充，故不做语义判断（判据在 services 层）。
    """
    reason = result.get("reason") or "unknown"
    if reason == "llm_failed" and result.get("retry_queued"):
        # 审计 epic P1-4：失败已入队后台自动重试——回执改为重试队列
        # 口径，绝不再教 agent「反复失败就 waive」（42 轮实测 6/6
        # waive 级联全由此文案引发）。
        attempts = result.get("retry_attempts")
        if result.get("retry_exhausted"):
            return (
                f"审计未执行: {reason}. 审计上游失败，自动重试已达上限"
                f"（{attempts} 次）。平台不再自动重试——如确认上游长时间"
                "不可用，可请 coordinator 走 waive_attestation（真实"
                "人工决策）；否则稍后再重试 request_code_audit。"
            )
        if result.get("retry_resequence"):
            # 审计 P2 修复：耗尽后再人工重试会新插 attempts=1 行，
            # 措辞点明这是新一轮重试序列，与「平台不再自动重试」不打架。
            return (
                f"审计未执行: {reason}. 此前的自动重试序列已耗尽；你本次"
                f"人工重试已作为新一轮重试序列（第 {attempts} 次）排队，"
                "成功后会通过收件箱通知你，无需申请豁免。"
            )
        return (
            f"审计未执行: {reason}. 审计上游失败，平台已排队自动重试"
            f"（第 {attempts} 次）；成功后会通过收件箱通知你，无需申请"
            "豁免，也无需循环重试 request_code_audit。等待重试通知即可，"
            "此期间不要提交（submit 门禁在审计凭证就绪前会拒绝）。"
        )
    # s3-clone_06 P0-1/P0-3：fail-loud 之后"直接 submit"会被门禁拒——
    # 旧文案（soft gate — does not block）误导 Agent 走一条必然失败的路。
    # 审计 epic P1-4：llm_failed 文案统一为重试队列口径（等待平台自动
    # 重试通知；waive 只是多次自动重试仍失败后的真实人工决策出口）。
    return (
        f"审计未执行: {reason}. "
        "Next: retry request_code_audit once（审计对真实 diff 需 30-90s）。"
        "llm_failed 属上游暂态时平台会自动排队重试并回填收件箱通知，"
        "等待即可；只有多次自动重试仍失败才考虑请 coordinator 走 "
        "waive_attestation(taskId=..., reason=...)（真实人工决策）。"
        "审计凭证就绪前 submit_task 会被门禁拦下。"
    )


def _soft_fail_result(result: dict) -> ToolResult:
    """未审计出口 → 三态映射的**单一漏斗**（TEST_DSH_55 P0）。

    这是本缺陷（假成功）的唯一修法：``audited=False`` 一律**不报成功**——
    34 次调用曾全报 success ⇒ ``run_steps.status`` 全 ``'completed'``，
    其中 11 次文本写着「审计未执行: llm_failed」，agent 读到「成功」却拿不到
    凭证，于是重发 11 次。

    三态映射（判据在 ``services.code_audit.audit_outcome_state``，此处只做
    「状态 → 结果契约」的翻译）：

    ``accepted_pending``（已受理·等待结果）—— 既不是成功，也不是失败：
      * ``success=False``：拿不到凭证就不是成功（判据①）；
      * ``fact="outcome_unknown"``：本仓库词表里该格的语义**正是**
        「结果未就绪 · 平台已记录 · **不许盲目重试**」（``result.py:35``），
        且被 ``_BLOCKED_FACT_KINDS`` 显式接纳 —— 不新增词表、不臆造枚举；
      * ``blocked=True``：平台已接管这次调用，不是 agent 该修的 ⇒
        ``blocked_ids`` 把它摘出「模型空转 / 工具失败」归因。**这是信号**；
      * ``wait_for_notice=True`` / ``action_required=False``：结果契约层的
        可读信号 —— 门禁 / UI / 后续消费者直接读，无需解析回执文本。
    与 ``failed`` 的差别是**结构化的**（blocked / fact / audit_state 三者
    同时不同），不是文字差别。

    ``failed``：``success=False`` + 显式 ``fact``（本漏斗**总是**给 fact，
    避免 ``fact_positions.finalize_tool_result`` 在收口处记
    ``fact_position_missing_at_finalize`` 日志并兜底覆盖）。
    """
    from hiveweave.services import code_audit as _code_audit

    state = _code_audit.audit_outcome_state(result)
    text = _soft_fail_receipt(result)
    shared: dict = {
        "audit_state": state,
        "retry_queued": bool(result.get("retry_queued")),
        "retry_attempts": result.get("retry_attempts"),
        "retry_exhausted": bool(result.get("retry_exhausted")),
    }
    if state == _code_audit.AUDIT_STATE_ACCEPTED_PENDING:
        # ⚠ 必须走**显式构造**而不是 ``ToolResult.err(..., blocked=True)``：
        # ``err()`` 的 ``**extra`` 会吞掉 ``blocked``，而 ``to_dict()`` 的
        # ``d["blocked"] = self.blocked`` 字段恒胜会把它抹成 False
        # （潜伏陷阱，见 ``tools/fact_positions.py`` 的 blocked 透传注释）。
        # 用 dataclass 构造可让 ``__post_init__`` 的不变式（blocked 必须携带
        # 平台侧事实位）真正生效 —— 测试已钉住（blocked is True）。
        return ToolResult(
            success=False,
            output="",
            error=text,
            blocked=True,
            fact="outcome_unknown",
            extra={
                "wait_for_notice": _code_audit.AUDIT_WAIT_FOR_NOTICE,
                "action_required": False,
                **shared,
            },
        )
    return ToolResult.err(
        text,
        fact=_code_audit.audit_failure_fact(result),
        wait_for_notice=False,
        action_required=True,
        **shared,
    )


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
        # 平台前提缺失（agent 无项目树）⇒ 从未执行 ⇒ runner_failed。
        # 显式声明 fact：否则收口处 finalize_tool_result 会记
        # fact_position_missing_at_finalize 并兜底覆盖（见 fact_positions.py）。
        return ToolResult.err(
            f"Agent {agent_id} has no project", fact="runner_failed"
        )

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
        # 触发点在 run_code_audit 调用之后 ⇒ 审计可能已部分跑过/已有副作用
        # ⇒ outcome_unknown（「结果未知，不许盲目重试」），不得标
        # runner_failed（=「从未执行」⇒ 下游读成「无副作用可重试」）。
        return ToolResult.err(
            f"code audit failed: {e}",
            fact="outcome_unknown",
            audit_state=_code_audit.AUDIT_STATE_FAILED,
            action_required=True,
            wait_for_notice=False,
        )

    if not result.get("audited"):
        # 未审计出口的**唯一漏斗**：三态（ready / accepted_pending / failed）
        # 映射集中在此，新增未审计出口时不可能再写出「假成功」。
        return _soft_fail_result(result)
    return _format_verdict(result)
