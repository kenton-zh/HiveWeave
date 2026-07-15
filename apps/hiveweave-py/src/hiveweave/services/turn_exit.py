"""Turn exit gates — violations block idle (function must return TurnResult).

Handlers are listed explicitly; add new rules here instead of scattering
if-branches across agent.py / game_time.py.
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
    # After a valid commit: schedule another turn instead of true idle
    continue_work: bool = False


def collect_unreplied_asks(
    pending_msgs: list[dict],
    tool_calls: list,
    name_by_id: dict[str, str] | None = None,
) -> list[dict]:
    """Messages that require a reply (ask / expect_report / reply language)
    and were not answered via ask_agent/notify_agent/send_message this turn.
    """
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


def evaluate_turn_exit(ctx: ExitContext) -> ExitDecision:
    """Run all exit gates. Pure-ish (uses ctx snapshot only)."""
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
        if remaining:
            violations.append("OPEN_TASKS_UNDECLARED")

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            uniq.append(v)

    if uniq:
        return ExitDecision(
            ok=False,
            violations=uniq,
            turn_result=turn_result,
            hint=_build_gate_hint(uniq, unreplied, turn_result),
            continue_work=False,
        )

    assert turn_result is not None
    return ExitDecision(
        ok=True,
        violations=[],
        turn_result=turn_result,
        hint="",
        continue_work=(turn_result.phase == "in_progress"),
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
            "或 phase='waiting' + waiting_on=[{kind:'agent', ref:'花名'}] "
            "或 phase='in_progress' 表示还要继续。"
        )
    lines.append("assistant 文字不是返回值。请立即用工具修正后再次 commit_turn。")
    return "\n".join(lines)
