"""Turn exit gates — validate TurnResult; do not schedule work.

P0: gate only validates. Scheduler (agent) decides continue/park.
phase=in_progress never implies unlimited continue_work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from hiveweave.services.reply_policy import message_requests_reply
from hiveweave.services.turn_result import (
    TurnResult,
    parse_turn_result,
    validate_phase_fields,
)
from hiveweave.services.turn_session import get_pending_turn_result

log = structlog.get_logger(__name__)

# Violations that warrant at most one repair retrigger
REPAIR_VIOLATIONS = frozenset({
    "MISSING_COMMIT_TURN",
    "INVALID_TURN_RESULT",
    "WAITING_ON_REQUIRED",
    "BLOCKED_WAITING_ON_REQUIRED",
    "UNREPLIED_ASKS",
})

# Ledger / obligation mismatches → park, do not immediately re-run LLM
PARK_VIOLATIONS = frozenset({
    "OPEN_TASKS_UNDECLARED",
})


@dataclass
class ExitContext:
    agent_id: str
    project_id: str
    tool_calls: list
    pending_inbox_msgs: list[dict] = field(default_factory=list)
    unreplied_asks: list[dict] = field(default_factory=list)
    open_task_obligations: list[dict] = field(default_factory=list)
    tasks_advanced: set[str] = field(default_factory=set)


@dataclass
class ExitDecision:
    ok: bool
    violations: list[str] = field(default_factory=list)
    turn_result: TurnResult | None = None
    hint: str = ""
    # Deprecated for auto-schedule: always False from evaluate; agent decides
    continue_work: bool = False
    # P0: repair once vs park on ledger mismatch
    should_repair: bool = False
    should_park: bool = False
    disposition: str = "runnable"  # runnable|waiting_human|waiting_agent|blocked|complete


def collect_unreplied_asks(
    pending_msgs: list[dict],
    tool_calls: list,
    name_by_id: dict[str, str] | None = None,
) -> list[dict]:
    """Messages that require a reply and were not answered this turn."""
    name_by_id = name_by_id or {}
    expects: list[dict] = []
    for m in pending_msgs:
        if (
            m.get("expect_report")
            or m.get("message_type") == "ask"
            or message_requests_reply(m.get("message"))
        ):
            expects.append(m)
    if not expects:
        return []

    replied_to: set[str] = set()
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        name = func.get("name") if isinstance(func, dict) else None
        if name not in ("send_message", "ask_agent", "notify_agent", "message_superior"):
            continue
        raw = func.get("arguments", {})
        if isinstance(raw, str):
            import json

            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        elif isinstance(raw, dict):
            args = raw
        else:
            continue
        recipients = args.get("recipients") or args.get("to") or args.get("target")
        if recipients is None:
            continue
        if isinstance(recipients, str):
            recipients = [recipients]
        if isinstance(recipients, list):
            replied_to.update(str(r) for r in recipients)

    unreplied: list[dict] = []
    for m in expects:
        fid = m.get("from_agent_id", "")
        fname = name_by_id.get(fid) or m.get("from_name") or fid[:8]
        m = dict(m)
        m["from_name"] = fname
        if fid in replied_to or fname in replied_to:
            continue
        unreplied.append(m)
    return unreplied


def _disposition_from_result(
    turn_result: TurnResult | None,
    obligations: list[dict],
) -> str:
    if turn_result is None:
        return "runnable"
    phase = turn_result.phase
    if phase == "waiting":
        kinds = {w.kind for w in turn_result.waiting_on}
        if "user" in kinds:
            return "waiting_human"
        if "timer" in kinds:
            return "waiting_timer"
        return "waiting_agent"
    if phase == "blocked":
        return "blocked"
    if phase == "done_slice" and not obligations:
        return "complete"
    return "runnable"


def evaluate_turn_exit(ctx: ExitContext) -> ExitDecision:
    """Validate turn exit. Never sets continue_work for unlimited re-entry."""
    violations: list[str] = []
    raw = get_pending_turn_result(ctx.agent_id)
    turn_result: TurnResult | None = None

    if raw is None:
        violations.append("MISSING_COMMIT_TURN")
    else:
        try:
            turn_result = parse_turn_result(raw)
        except Exception as e:
            violations.append("INVALID_TURN_RESULT")
            log.warning(
                "turn_result_parse_failed",
                agent_id=ctx.agent_id,
                error=str(e),
            )
            turn_result = None

    if turn_result is not None:
        violations.extend(validate_phase_fields(turn_result))

    unreplied = ctx.unreplied_asks
    if unreplied:
        violations.append("UNREPLIED_ASKS")

    remaining_obligations = list(ctx.open_task_obligations)
    if turn_result and turn_result.phase == "done_slice":
        remaining = []
        for t in ctx.open_task_obligations:
            tid = str(t.get("id") or "")
            if tid in ctx.tasks_advanced:
                continue
            if any(
                tid.startswith(a) or a.startswith(tid)
                for a in ctx.tasks_advanced
                if len(a) >= 8
            ):
                continue
            remaining.append(t)
        remaining_obligations = remaining
        if remaining:
            violations.append("OPEN_TASKS_UNDECLARED")

    seen: set[str] = set()
    uniq: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            uniq.append(v)

    disposition = _disposition_from_result(turn_result, remaining_obligations)

    if uniq:
        park = bool(PARK_VIOLATIONS.intersection(uniq)) and not bool(
            REPAIR_VIOLATIONS.intersection(uniq) - PARK_VIOLATIONS
        )
        # Mixed: prefer repair if unreplied/missing commit present
        repair_only = bool(REPAIR_VIOLATIONS.intersection(uniq)) and not park
        if PARK_VIOLATIONS.intersection(uniq) and REPAIR_VIOLATIONS.intersection(uniq):
            # Both → repair unreplied first if present, else park ledger
            repair_only = "UNREPLIED_ASKS" in uniq or "MISSING_COMMIT_TURN" in uniq
            park = not repair_only
        if park:
            disposition = "runnable" if remaining_obligations else disposition
        return ExitDecision(
            ok=False,
            violations=uniq,
            turn_result=turn_result,
            hint=_build_gate_hint(uniq, unreplied, turn_result),
            continue_work=False,
            should_repair=repair_only,
            should_park=park or (bool(PARK_VIOLATIONS.intersection(uniq)) and not repair_only),
            disposition=disposition if not park else (
                "runnable" if remaining_obligations else "complete"
            ),
        )

    assert turn_result is not None
    return ExitDecision(
        ok=True,
        violations=[],
        turn_result=turn_result,
        hint="",
        continue_work=False,  # agent scheduler may still continue one slice
        should_repair=False,
        should_park=False,
        disposition=disposition,
    )


def _build_gate_hint(
    violations: list[str],
    unreplied: list[dict],
    turn_result: TurnResult | None,
) -> str:
    lines = [
        "[TURN EXIT BLOCKED]",
        "每一轮必须像函数一样返回 TurnResult。当前不能结束回合：",
    ]
    labels = {
        "MISSING_COMMIT_TURN": "未调用 commit_turn — 请提交 phase/summary（及必要的 waiting_on/result）",
        "INVALID_TURN_RESULT": "commit_turn 参数无效 — 请按 schema 重试",
        "WAITING_ON_REQUIRED": "phase=waiting 必须提供 waiting_on",
        "BLOCKED_WAITING_ON_REQUIRED": "phase=blocked 必须提供 waiting_on",
        "UNREPLIED_ASKS": "有人 ask 了你，必须用 ask_agent/notify_agent/send_message 回复后才能收工",
        "OPEN_TASKS_UNDECLARED": "仍有可行动任务 — 请推进任务，或用 phase=in_progress/waiting/blocked 声明状态（禁止假装 done_slice）",
    }
    for v in violations:
        lines.append(f"- {labels.get(v, v)}")

    if "UNREPLIED_ASKS" in violations and unreplied:
        lines.append("未回复：")
        for m in unreplied[:8]:
            name = m.get("from_name") or (m.get("from_agent_id") or "?")[:8]
            preview = (m.get("message") or "")[:60]
            lines.append(f"  ❌ {name}：{preview}")

    if "MISSING_COMMIT_TURN" in violations:
        lines.append(
            "调用示例：commit_turn(phase='done_slice', summary='…') "
            "或 phase='waiting' + waiting_on=[{kind:'user', ref:'user'}] "
            "或 phase='in_progress' 表示本 slice 有进展。"
        )
    if "OPEN_TASKS_UNDECLARED" in violations:
        lines.append(
            "系统将按真实账本停泊，不会无限续跑。"
            "请在下一外部事件（新任务/用户消息）到来时再推进。"
        )
    lines.append("assistant 文字不是返回值。请立即用工具修正后再次 commit_turn。")
    return "\n".join(lines)
