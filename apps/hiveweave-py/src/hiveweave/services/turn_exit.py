"""Turn exit gates — validate TurnResult; do not schedule work.

P0: gate only validates. Scheduler (agent) decides continue/park.
phase=in_progress never implies unlimited continue_work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

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
    "ASSIGNEE_MUST_SUBMIT",
    "CREATOR_MUST_REVIEW",
    "CREATOR_MUST_MERGE",
    "HIRE_UNREPORTED",
})

# Ledger / obligation mismatches → park, do not immediately re-run LLM
PARK_VIOLATIONS = frozenset({
    "OPEN_TASKS_UNDECLARED",
})


def _task_ref_matches(ref: str, tid: str) -> bool:
    if not ref or not tid:
        return False
    if tid == ref:
        return True
    if len(ref) >= 8 and (tid.startswith(ref) or ref.startswith(tid)):
        return True
    return False


def _waiting_on_task(turn_result: TurnResult | None, tid: str) -> bool:
    if turn_result is None:
        return False
    for w in turn_result.waiting_on or []:
        if w.kind == "task" and _task_ref_matches(str(w.ref or ""), tid):
            return True
    return False


def _task_advanced(tid: str, advanced: set[str]) -> bool:
    if tid in advanced:
        return True
    return any(
        tid.startswith(a) or a.startswith(tid)
        for a in advanced
        if len(a) >= 8
    )


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
    extra_replied_to: set[str] | None = None,
    exempt_senders: set[str] | None = None,
) -> list[dict]:
    """Messages that require a reply and were not answered this turn.

    Structural only: expect_report or message_type=ask (language-agnostic).

    - extra_replied_to: 本 turn 内已成功送达的收件人（来自 inbox 落库记录，
      即"成功调用 send_message/message 工具"的 DB 证据），与工具调用
      参数提取的 replied_to 合并判定。
    - exempt_senders: 豁免的发送方（已归档/不存在/user/system）——
      对归档 agent 的回复义务随其归档消亡；user/system 的回复通道是
      assistant 输出本身，不适用本门。
    """
    name_by_id = name_by_id or {}
    exempt_senders = exempt_senders or set()
    expects: list[dict] = []
    for m in pending_msgs:
        fid = m.get("from_agent_id", "")
        if fid in exempt_senders:
            continue
        mt = (m.get("message_type") or "").lower()
        if m.get("expect_report") or mt == "ask":
            expects.append(m)
    if not expects:
        return []

    replied_to: set[str] = set(extra_replied_to or ())
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        name = func.get("name") if isinstance(func, dict) else None
        if name not in _MSG_TOOLS:
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


_MSG_TOOLS = frozenset({
    "send_message",
    "ask_agent",
    "notify_agent",
    "message_superior",
    "message_peer",
    "message_team",
    "message_subordinate",
    "message_user",
})


def _tool_name(tc: dict) -> str:
    if not isinstance(tc, dict):
        return ""
    func = tc.get("function") or {}
    if isinstance(func, dict) and func.get("name"):
        return str(func["name"])
    return str(tc.get("name") or "")


def hire_without_report(tool_calls: list) -> bool:
    """True if this turn hired someone but never messaged a peer.

    Does not guess intent — only checks whether the obvious next tool
    after hire_agent was used. The agent still chooses whom/what to say.
    """
    hired = False
    messaged = False
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        name = _tool_name(tc)
        if name == "hire_agent":
            hired = True
        if name in _MSG_TOOLS:
            messaged = True
    return hired and not messaged


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

    if hire_without_report(ctx.tool_calls):
        violations.append("HIRE_UNREPORTED")

    remaining_obligations = list(ctx.open_task_obligations)
    if turn_result and turn_result.phase in ("done_slice", "waiting"):
        remaining = []
        for t in ctx.open_task_obligations:
            tid = str(t.get("id") or "")
            if _task_advanced(tid, ctx.tasks_advanced):
                continue
            # Explicit wait on this task is a legal idle exit
            if turn_result.phase == "waiting" and _waiting_on_task(
                turn_result, tid
            ):
                remaining.append(t)
                continue
            role = t.get("role_hint")
            status = t.get("status")
            if role == "assignee" and status in (
                "running", "claimed", "rework",
            ):
                violations.append("ASSIGNEE_MUST_SUBMIT")
            elif role == "creator" and status in ("submitted", "reviewing"):
                violations.append("CREATOR_MUST_REVIEW")
            elif role == "creator" and status == "approved":
                violations.append("CREATOR_MUST_MERGE")
            remaining.append(t)
        remaining_obligations = remaining
        # Park leftover ledger mismatches not covered by repair (e.g. verifying)
        if turn_result.phase == "done_slice":
            leftover = [
                t for t in remaining
                if not (
                    (
                        t.get("role_hint") == "assignee"
                        and t.get("status") in ("running", "claimed", "rework")
                    )
                    or (
                        t.get("role_hint") == "creator"
                        and t.get("status") in (
                            "submitted", "reviewing", "approved",
                        )
                    )
                )
            ]
            if leftover:
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
        # Mixed: prefer repair if unreplied/missing commit / submit-review present
        repair_only = bool(REPAIR_VIOLATIONS.intersection(uniq)) and not park
        if PARK_VIOLATIONS.intersection(uniq) and REPAIR_VIOLATIONS.intersection(uniq):
            repair_only = (
                "UNREPLIED_ASKS" in uniq
                or "MISSING_COMMIT_TURN" in uniq
                or "ASSIGNEE_MUST_SUBMIT" in uniq
                or "CREATOR_MUST_REVIEW" in uniq
                or "CREATOR_MUST_MERGE" in uniq
                or "HIRE_UNREPORTED" in uniq
            )
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
        "HIRE_UNREPORTED": (
            "本轮调用了 hire_agent 但没有用 send_message/ask_agent/notify_agent 通知请求方 — "
            "招人完成≠协作完成；请向请求方汇报花名/shortId/role，再 commit_turn"
        ),
        "OPEN_TASKS_UNDECLARED": "仍有可行动任务 — 请推进任务，或用 phase=in_progress/waiting/blocked 声明状态（禁止假装 done_slice）",
        "ASSIGNEE_MUST_SUBMIT": (
            "有 running/claimed/rework 任务未 submit_task — "
            "请调用 submit_task(taskId, summary, testsPassed=true)，"
            "或 phase=waiting + waiting_on=[{kind:'task', ref:taskId}]"
        ),
        "CREATOR_MUST_REVIEW": (
            "有 submitted/reviewing 任务待你 review — "
            "请调用 review_task，或 phase=waiting + waiting_on=[{kind:'task', ref:taskId}]"
        ),
        "CREATOR_MUST_MERGE": (
            "有 approved 任务待你 merge 到 main — "
            "请立即调用 git_worktree_merge(branchName=assignee shortId 或 hw/...)。"
            "禁止口头让 executor 自己 merge；冲突则 review_task(rework) 让其在 worktree 对齐 main。"
        ),
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


# ── Synchronous pre-check for commit_turn ──────────────────


async def pre_check_exit_gates(
    agent_id: str, project_id: str, phase: str
) -> list[str]:
    """Lightweight synchronous pre-check of exit gates.

    Called from commit_turn when phase != in_progress. Returns a list of
    violation names — empty list means exit is likely safe.

    This is best-effort: if queries fail, returns empty (don't block).
    The full gate check in _handle_completion is the authoritative backstop.
    """
    violations: list[str] = []
    if phase == "in_progress":
        return violations  # No exit gates apply to in_progress

    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(project_id)

        # 1. Unreplied asks: inbox messages with expect_report=1, read=0
        cursor = await conn.execute(
            "SELECT from_agent_id, message FROM inbox "
            "WHERE to_agent_id = ? AND expect_report = 1 AND read = 0 "
            "AND from_agent_id NOT IN ('user', 'system') "
            "ORDER BY created_at DESC LIMIT 10",
            [agent_id],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if rows:
            violations.append("UNREPLIED_ASKS")

        # 2. Open task obligations: claimed/running/rework as assignee
        cursor = await conn.execute(
            "SELECT id, status FROM tasks "
            "WHERE assignee_id = ? AND is_archived = 0 "
            "AND status IN ('claimed', 'running', 'rework') "
            "LIMIT 20",
            [agent_id],
        )
        assignee_tasks = await cursor.fetchall()
        await cursor.close()
        if assignee_tasks and phase in ("done_slice", "waiting"):
            violations.append("ASSIGNEE_MUST_SUBMIT")

        # 3. Open task obligations: submitted/reviewing/approved as creator
        cursor = await conn.execute(
            "SELECT id, status FROM tasks "
            "WHERE creator_id = ? AND is_archived = 0 "
            "AND status IN ('submitted', 'reviewing', 'approved') "
            "LIMIT 20",
            [agent_id],
        )
        creator_tasks = await cursor.fetchall()
        await cursor.close()
        if creator_tasks and phase in ("done_slice", "waiting"):
            # Check if waiting_on covers them — we don't have the TurnResult
            # waiting_on here, so be conservative and flag
            statuses = {r["status"] for r in creator_tasks}
            if "submitted" in statuses or "reviewing" in statuses:
                violations.append("CREATOR_MUST_REVIEW")
            if "approved" in statuses:
                violations.append("CREATOR_MUST_MERGE")

    except Exception as e:
        log.debug("pre_check_exit_gates_failed", error=str(e))
        # Best-effort: don't block on query failure
        return []

    return violations

