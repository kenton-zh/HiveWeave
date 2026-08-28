"""Turn protocol tools: commit_turn, ask_agent, notify_agent."""

from __future__ import annotations

import time

from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveweave.tools.helpers import coerce_to_list
from hiveweave.tools.base import tool
from hiveweave.tools.result import ToolResult
from hiveweave.services.turn_result import (
    TURN_RESULT_SCHEMA_VERSION,
    parse_turn_result,
    validate_phase_fields,
)
from hiveweave.services.turn_session import (
    classify_commit_gate_soft_warn,
    get_pending_turn_result,
    set_pending_turn_result,
)

log = structlog.get_logger(__name__)


async def _archive_turn_lessons(agent_id: str, tr: Any, ctx: Any) -> None:
    """Co-learning: done_slice + extensions.lessons → archive experiential
    lessons (ChatDev Experiential Co-Learning). Quality gate inside
    LessonService rejects empty/fluff lessons. Fail-open — never blocks
    turn exit. Called on both soft-pass and normal commit paths.
    """
    if tr.phase != "done_slice" or not (tr.extensions or {}).get("lessons"):
        return
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.lessons import LessonService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id and ctx is not None:
            project_id = getattr(ctx, "project_id", None)
        if project_id:
            lessons = tr.extensions.get("lessons")
            if isinstance(lessons, list):
                svc = LessonService()
                for item in lessons:
                    if not isinstance(item, dict):
                        continue
                    lesson_text = item.get("lesson")
                    if not isinstance(lesson_text, str) or not lesson_text.strip():
                        continue
                    await svc.save_lesson(
                        project_id=project_id,
                        agent_id=agent_id,
                        lesson=lesson_text,
                        tags=item.get("tags") or item.get("keywords"),
                        root_cause=item.get("root_cause"),
                        fix=item.get("fix"),
                        source_summary=tr.summary,
                    )
    except Exception:
        pass  # fail-open: lesson archiving never blocks turn exit


# ── commit_turn ──────────────────────────────────────────


# TEST18 P0-4: in_progress 圈数止损 — commit_turn(in_progress) 不触发 exit
# gate，LLM 可在同一工具循环里无限"commit+工具"（柚子 20 次循环实锤）。
# ctx 无 turn id，用时间窗近似：90s 内第 6 次 in_progress → 强制提示收尾。
_IN_PROGRESS_LIMIT = 5
_IN_PROGRESS_WINDOW_MS = 90_000
_in_progress_counts: dict[str, list[float]] = {}


class CommitTurnParams(BaseModel):
    """Mandatory end-of-turn return value (TurnResult ABI)."""

    model_config = ConfigDict(populate_by_name=True)

    phase: Literal["in_progress", "waiting", "blocked", "done_slice"] = Field(
        description=(
            "Control plane. Pick one: in_progress = keep working (does NOT "
            "trigger the exit gate; allowed to 5 per 90s before forced to "
            "converge); waiting = legal wait on someone/something and requires "
            "waiting_on with {kind, ref}; blocked = stuck, cannot advance and "
            "requires waiting_on; done_slice = this slice's obligations are "
            "cleared, triggers the exit gate and hard-stops the tool loop."
        ),
    )
    summary: str = Field(
        description="1-2 sentences: what this turn accomplished",
        json_schema_extra={"aliases": ["content", "message", "text"]},
    )
    waiting_on: list[dict[str, Any]] | None = Field(
        default=None,
        alias="waitingOn",
        description=(
            "Required for waiting/blocked. "
            "Items: {kind: agent|task|user|timer|external, ref: str, note?: str}"
        ),
        json_schema_extra={"aliases": ["waitingOn", "waiting_on"]},
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Data plane payload (replies, tasks, artifacts, …). May be {}",
    )
    extensions: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Forward-compatible extensions. May be {}. "
            "Co-learning: on phase=done_slice, submit lessons learned as "
            "extensions.lessons=[{lesson, root_cause?, fix?, tags?}] — they are "
            "archived and recalled for future tasks."
        ),
    )


@tool(
    "commit_turn",
    "MANDATORY end-of-turn return value. Every turn is a function call — "
    "you MUST commit_turn before stopping. phase=in_progress keeps you working; "
    "waiting/blocked require waiting_on; done_slice only when this slice's "
    "obligations are cleared. Assistant text is NOT a return value.",
    requires_workspace=False,
    security_level="standard",
)
async def commit_turn_tool(
    params: CommitTurnParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Validate and buffer TurnResult for exit gates + persist."""
    raw: dict[str, Any] = {
        "schema_version": TURN_RESULT_SCHEMA_VERSION,
        "phase": params.phase,
        "summary": params.summary,
        "waiting_on": params.waiting_on or [],
        "result": params.result if params.result is not None else {},
        "extensions": params.extensions if params.extensions is not None else {},
    }
    try:
        tr = parse_turn_result(raw)
    except Exception as e:
        return ToolResult.err(f"Invalid TurnResult: {e}")

    if tr.phase == "in_progress":
        now = time.time() * 1000
        stamps = _in_progress_counts.setdefault(agent_id, [])
        stamps = [t for t in stamps if now - t < _IN_PROGRESS_WINDOW_MS]
        stamps.append(now)
        _in_progress_counts[agent_id] = stamps
        if len(stamps) > _IN_PROGRESS_LIMIT:
            # 不重置计数 — 窗口内每次 in_progress 都拦，直到 LLM 转
            # waiting/blocked/done_slice（90s 窗口自然过期后才放行）。
            return ToolResult.err(
                "STOP: commit_turn(in_progress) called too many times in this "
                "tool loop — the turn is not making exit progress. Either "
                "commit_turn(phase='waiting'/'blocked', waiting_on=[...]) to "
                "park legally, or commit_turn(phase='done_slice') when this "
                "slice's obligations are cleared. Do NOT call commit_turn("
                "in_progress) again this turn."
            )

    field_violations = validate_phase_fields(tr)
    if field_violations:
        return ToolResult.err(
            "commit_turn rejected: "
            + ", ".join(field_violations)
            + ". waiting/blocked require waiting_on=[{kind, ref}]."
        )

    payload = tr.to_persist_dict()

    # P2 doom loop 缓解：同一 turn 内同参数 commit_turn 已被接受过时，
    # 返回差异化提示（而非逐字相同的 "TurnResult accepted"），让模型看到
    # 新信息，打破"相同工具结果 → 相同决策"的重复调用循环。
    prev = get_pending_turn_result(agent_id)
    if isinstance(prev, dict) and prev == payload:
        return ToolResult.ok(
            "STOP: TurnResult ALREADY committed "
            f"(phase={tr.phase}). Do NOT call any more tools this turn. "
            "gates: []. Platform will evaluate exit; if blocked you will be "
            "told what remains — do not re-commit_turn with the same args.",
            turn_result=payload,
            duplicate=True,
            end_turn=True,
            gates=[],
        )

    # Synchronous pre-check: if phase != in_progress, run exit gate pre-check
    # before accepting. This gives the LLM immediate feedback instead of
    # accepting and then blocking at _handle_completion.
    if tr.phase != "in_progress":
        try:
            from hiveweave.db import meta as meta_db
            from hiveweave.services.turn_exit import pre_check_exit_gates

            project_id = await meta_db.get_agent_project_id(agent_id)
            if not project_id and ctx is not None:
                project_id = getattr(ctx, "project_id", None)
            if project_id:
                violations = await pre_check_exit_gates(
                    agent_id,
                    project_id,
                    tr.phase,
                    waiting_on=[
                        {"kind": w.kind, "ref": w.ref}
                        for w in (tr.waiting_on or [])
                    ],
                )
                if violations:
                    labels = {
                        "UNREPLIED_ASKS": "有未回复的 ask 消息",
                        "WAIT_WITHOUT_ASK": "waiting 前须先向对方发消息",
                        "HIRE_UNREPORTED": "本轮 hire_agent 后未通知请求方",
                        "ASSIGNEE_MUST_SUBMIT": "有 claimed/running/rework 任务未提交（已派下属则 waiting 子任务，勿催交）",
                        "REVIEWER_MUST_START_REVIEW": "有 submitted 任务待开始审查",
                        "REVIEWER_MUST_FINISH_REVIEW": "有 reviewing 任务待完成审查",
                        "CREATOR_MUST_REVIEW": "有 submitted/reviewing 任务待审查",
                        "CREATOR_MUST_MERGE": "有 approved 任务待合并",
                        "UNCOMMITTED_WORKTREE": "worktree 有未提交改动",
                        "OPEN_TASKS_UNDECLARED": "仍有可行动任务却声明 done_slice",
                        "CEO_PROJECT_PENDING": "项目仍有未推进工作（CEO 项目级义务）",
                    }
                    # TEST19 ④: 每条 gate 附带可执行动作（单一来源
                    # GATE_ACTIONS，与 turn_exit backstop 共用防漂移），
                    # 拒绝消息变成编号步骤清单，而不是一行只报问题的提示。
                    from hiveweave.services.turn_exit import GATE_ACTIONS
                    gate_actions = GATE_ACTIONS
                    # Soft-warn (reminder-class only): first hit → warn+allow;
                    # second → hard. HARD_COMMIT_GATE_CODES (UNREPLIED_ASKS)
                    # always hard — soft-pass must not end the reply contract
                    # (TEST14 BUG-1). Soft-pass does not suppress backstop.
                    soft, hard = classify_commit_gate_soft_warn(
                        agent_id, violations
                    )
                    try:
                        from hiveweave.services.telemetry import telemetry

                        for code in hard:
                            telemetry.gate_hard_reject(code)
                        for code in soft:
                            telemetry.gate_soft_pass(code)
                    except Exception:
                        pass
                    if hard:
                        soft_note = ""
                        if soft:
                            soft_note = (
                                f" (first soft-pass already used for: "
                                f"{', '.join(soft)})"
                            )
                        steps = " ".join(
                            f"{i}) [{code}] {labels.get(code, code)} — "
                            f"动作: {gate_actions.get(code, '先处理该义务再重试')}."
                            for i, code in enumerate(hard, 1)
                        )
                        # P0-2: 附带项目级待办明细，让 CEO 直接看到什么 pending
                        if "CEO_PROJECT_PENDING" in hard:
                            from hiveweave.services.turn_exit import (
                                pop_ceo_project_pending_details,
                            )

                            details = pop_ceo_project_pending_details(agent_id)
                            if details:
                                steps += " 项目级待办: " + "；".join(
                                    details[:6]
                                )
                        if "UNREPLIED_ASKS" in hard:
                            from hiveweave.services.turn_exit import (
                                _unreplied_ask_contracts,
                                format_unreplied_ask_reject_suffix,
                            )

                            try:
                                ask_bits = await _unreplied_ask_contracts(
                                    agent_id
                                )
                            except Exception:
                                ask_bits = []
                            steps += format_unreplied_ask_reject_suffix(
                                ask_bits
                            )
                        return ToolResult.err(
                            f"commit_turn REJECTED (synchronous gate): "
                            + steps
                            + soft_note
                            + ". 请按上述步骤处理这些义务再 commit_turn，"
                            "或改用 phase=in_progress 继续工作。"
                            + f" gates: {hard}.",
                            gates=list(hard),
                            actions={
                                c: gate_actions.get(c, "")
                                for c in hard
                            },
                        )
                    if soft:
                        # Soft-pass: accept TurnResult but surface the warning.
                        # Still end_turn — backstop may still repair if the
                        # violation is real (name-mismatch false positives
                        # are fixed in pre_check enrichment).
                        set_pending_turn_result(agent_id, payload)
                        # N3: 预检未硬拒（soft-pass）→ 清理 CEO 项目级
                        # 待办明细残留（上次 done_slice 被拒留下的 advisory）。
                        from hiveweave.services.turn_exit import (
                            pop_ceo_project_pending_details,
                        )

                        pop_ceo_project_pending_details(agent_id)
                        hints = [labels.get(v, v) for v in soft]
                        # Persist observability (best-effort)
                        try:
                            from hiveweave.db import meta as meta_db
                            from hiveweave.services.work_log import WorkLogService

                            project_id = await meta_db.get_agent_project_id(
                                agent_id
                            )
                            if not project_id and ctx is not None:
                                project_id = getattr(ctx, "project_id", None)
                            if project_id:
                                await WorkLogService().write_work_log(
                                    project_id,
                                    agent_id,
                                    None,
                                    "turn_result",
                                    f"[{tr.phase}/SOFT] {tr.summary}"[:140],
                                    details={
                                        **payload,
                                        "soft_pass": soft,
                                    },
                                )
                        except Exception:
                            pass
                        await _archive_turn_lessons(agent_id, tr, ctx)
                        return ToolResult.ok(
                            f"STOP: TurnResult accepted WITH SOFT WARNING "
                            f"(first offense this turn): {'; '.join(hints)}. "
                            f"gates: {soft}. Do NOT call any more tools. "
                            f"Exit backstop may still require a fix if the "
                            f"obligation remains open. phase={tr.phase}.",
                            turn_result=payload,
                            soft_pass=soft,
                            end_turn=True,
                            gates=list(soft),
                        )
        except Exception:
            pass  # best-effort: don't block on pre-check failure

    # N3: 同步预检完成且未硬拒（无违规 / 预检未运行 / 预检异常）→ 统一
    # 清理 CEO 项目级待办明细残留。hard 拒绝分支已先 pop 拼消息（见上）。
    from hiveweave.services.turn_exit import pop_ceo_project_pending_details

    pop_ceo_project_pending_details(agent_id)

    set_pending_turn_result(agent_id, payload)

    # Persist for observability
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.work_log import WorkLogService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id and ctx is not None:
            project_id = getattr(ctx, "project_id", None)
        if project_id:
            await WorkLogService().write_work_log(
                project_id,
                agent_id,
                None,
                "turn_result",
                f"[{tr.phase}] {tr.summary}"[:140],
                details=payload,
            )
    except Exception:
        pass

    # Co-learning: done_slice + extensions.lessons → archive experiential lessons
    # (ChatDev Experiential Co-Learning). Quality gate inside LessonService:
    # rejects empty/fluff lessons. Fail-open — never block turn exit on this.
    await _archive_turn_lessons(agent_id, tr, ctx)

    # BUG-3 / DESIGN-1: non-in_progress commit hard-stops the tool loop.
    # Empty gates: [] means no outstanding synchronous gate failures —
    # do not invent gate names (e.g. HIRE_UNREPORTED) from memory.
    if tr.phase == "in_progress":
        return ToolResult.ok(
            f"TurnResult accepted: phase=in_progress. Will continue working. "
            f"gates: [].",
            turn_result=payload,
            end_turn=False,
            gates=[],
        )

    return ToolResult.ok(
        f"STOP: TurnResult committed (phase={tr.phase}). "
        f"Do NOT call any more tools this turn. gates: []. "
        f"Platform evaluates exit next; if blocked you will be told "
        f"exactly which gates remain — do not guess.",
        turn_result=payload,
        end_turn=True,
        gates=[],
    )


# ── defer_task_advance（不推进）───────────────────────────


class DeferTaskAdvanceParams(BaseModel):
    """Explicitly decline to advance actionable tasks this wake cycle."""

    model_config = ConfigDict(populate_by_name=True)

    reason: str = Field(
        description=(
            "Why you cannot advance now (blocked on whom/what, missing info, "
            "waiting for human, etc.). Be concrete — not empty filler."
        ),
        json_schema_extra={"aliases": ["reason", "why", "note", "summary"]},
    )


@tool(
    "defer_task_advance",
    "不推进：本轮无法推动可行动任务时必须调用。声明后平台停止 [TASK ADVANCE] "
    "循环提醒，直到你被再次唤醒（用户/inbox/任务）。不要用空话收工代替本工具。",
    requires_workspace=False,
    security_level="standard",
)
async def defer_task_advance_tool(
    params: DeferTaskAdvanceParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    """Mark this wake cycle as intentional no-advance — stops nudge loop."""
    from hiveweave.services.turn_session import (
        DEFER_REASON_STREAK_LIMIT,
        record_defer_reason,
        set_task_advance_deferred,
    )

    reason = (params.reason or "").strip()
    if not reason:
        return ToolResult.err(
            "defer_task_advance requires a non-empty reason "
            "(why you cannot advance now)."
        )

    streak = record_defer_reason(agent_id, reason)
    tripped = streak >= DEFER_REASON_STREAK_LIMIT
    # Tripped: also lift the standing suppression, otherwise a flag set by an
    # earlier defer keeps [TASK ADVANCE] muted through every platform
    # self-continue (no external wake ⇒ nothing clears it).
    set_task_advance_deferred(agent_id, not tripped)

    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.work_log import WorkLogService

        project_id = await meta_db.get_agent_project_id(agent_id)
        if not project_id and ctx is not None:
            project_id = getattr(ctx, "project_id", None)
        if project_id:
            prefix = "[不推进复读]" if tripped else "[不推进]"
            await WorkLogService().write_work_log(
                project_id,
                agent_id,
                None,
                (
                    "task_advance_defer_breaker"
                    if tripped
                    else "task_advance_deferred"
                ),
                f"{prefix} {reason}"[:140],
                details={
                    "reason": reason,
                    "same_reason_streak": streak,
                    "breaker_tripped": tripped,
                },
            )
    except Exception:
        pass

    if tripped:
        log.warning(
            "defer_task_advance_breaker_tripped",
            agent_id=agent_id,
            streak=streak,
            reason=reason[:120],
        )
        return ToolResult.err(
            f"defer_task_advance 已被断路器拦下：同一理由连续第 {streak} 次"
            f"（阈值 {DEFER_REASON_STREAK_LIMIT}）。复读同一句"
            f"「不推进」不是等待，是停滞——催办不再关闭。请择一：\n"
            f"1) 若任务其实已具备收口条件（已 approved 且 merge 已落库），"
            f"检查 get_platform_state 的 ledger，不必再 merge；approved 由"
            f"平台定时收口（约 2 分钟一轮，超 10 分钟宽限即关闭）。\n"
            f"2) 若确在等人：commit_turn(phase=waiting, "
            f"waiting_on={{kind:'agent'|'task', ref:'<id>'}})，让等待可被"
            f"超时唤醒与成环检测看见。\n"
            f"3) 若已无路可走：向上级/CEO 上报阻塞点（ask_agent 写清缺什么、"
            f"需要谁决策），不要再 defer。\n"
            f"换一个真实变化的理由重试才会被接受。 reason={reason[:200]}"
        )

    return ToolResult.ok(
        "已声明不推进。平台不会再因「未推动任务」循环提醒你，"
        "直到你被再次唤醒。请接着 commit_turn"
        "(通常 phase=waiting 或 blocked，并写清 waiting_on)。"
        f" 同一理由已连续 {streak}/{DEFER_REASON_STREAK_LIMIT} 次——"
        f"到阈值将不再接受，届时请上报上级或复核任务是否已可收口。"
        f" reason={reason[:200]}"
    )


# ── ask_agent / notify_agent ─────────────────────────────


class AskNotifyParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recipients: list[str] = Field(
        description="Recipient 花名, short_id, or UUID list",
        json_schema_extra={"aliases": ["recipient", "to", "targets", "target"]},
    )
    message: str = Field(
        description="Message body",
        json_schema_extra={"aliases": ["content", "body", "text"]},
    )
    priority: str = Field(
        default="normal",
        description="'normal' or 'urgent'",
        json_schema_extra={"aliases": ["level"]},
    )
    reply_to: str | None = Field(
        default=None,
        alias="replyTo",
        description=(
            "Reply contract ID from the original message's reply_contract_id. "
            "Include this when replying to a message that had reply_required=true "
            "to explicitly close the reply contract — otherwise a NEW reply "
            "obligation is created and the original asker stays blocked."
        ),
        json_schema_extra={"aliases": ["replyTo", "reply_to", "replyContractId"]},
    )

    @field_validator("recipients", mode="before")
    @classmethod
    def _coerce_recipients(cls, v: Any) -> Any:
        return coerce_to_list(v)


@tool(
    "ask_agent",
    "Ask one or more agents and REQUIRE a reply via send_message/ask_agent/notify_agent. "
    "Put the request and what they must return in this one message; do not also "
    "send a status-only follow-up. Prefer this over send_message(expectReport=true). "
    "When replying to an existing ask, pass replyTo=<the original message's "
    "reply_contract_id> to close the contract (not the message_id from a tool result).",
    requires_workspace=False,
    security_level="standard",
)
async def ask_agent_tool(
    params: AskNotifyParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    from hiveweave.tools.orchestration_tools import _send_message_core

    return await _send_message_core(
        agent_id=agent_id,
        recipients=params.recipients,
        message=params.message,
        priority=params.priority,
        expect_report=True,
        ctx=ctx,
        reply_to=params.reply_to,
        message_type="ask",
    )


@tool(
    "notify_agent",
    "Notify agents (FYI) — does NOT require a reply. "
    "Use for status broadcasts. Prefer this over send_message for one-way updates. "
    "When replying to an existing ask, pass replyTo=<the original message's "
    "reply_contract_id> to close the contract without creating a new one.",
    requires_workspace=False,
    security_level="standard",
)
async def notify_agent_tool(
    params: AskNotifyParams, agent_id: str, workspace: str, ctx=None
) -> ToolResult:
    from hiveweave.tools.orchestration_tools import _send_message_core

    return await _send_message_core(
        agent_id=agent_id,
        recipients=params.recipients,
        message=params.message,
        priority=params.priority,
        expect_report=False,
        ctx=ctx,
        reply_to=params.reply_to,
        message_type="notify",
    )
