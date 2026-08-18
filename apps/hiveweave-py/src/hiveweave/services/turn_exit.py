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
from hiveweave.services.turn_session import (
    filter_soft_passed_violations,
    get_pending_turn_result,
)

log = structlog.get_logger(__name__)

# Violations that warrant at most one repair retrigger
REPAIR_VIOLATIONS = frozenset({
    "MISSING_COMMIT_TURN",
    "INVALID_TURN_RESULT",
    "WAITING_ON_REQUIRED",
    "BLOCKED_WAITING_ON_REQUIRED",
    "UNREPLIED_ASKS",
    "WAIT_WITHOUT_ASK",
    "ASSIGNEE_MUST_SUBMIT",
    "REVIEWER_MUST_START_REVIEW",
    "REVIEWER_MUST_FINISH_REVIEW",
    "CREATOR_MUST_REVIEW",
    "CREATOR_MUST_MERGE",
    "HIRE_UNREPORTED",
    "UNCOMMITTED_WORKTREE",
    "CEO_PROJECT_PENDING",
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
    return waiting_covers_task(turn_result.waiting_on, tid)


def _iter_waiting_refs(waiting_on: list | None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for w in waiting_on or []:
        if isinstance(w, dict):
            kind = str(w.get("kind") or "").lower()
            ref = str(w.get("ref") or "")
        else:
            kind = str(getattr(w, "kind", "") or "").lower()
            ref = str(getattr(w, "ref", "") or "")
        if kind and ref:
            items.append((kind, ref))
    return items


def waiting_covers_task(waiting_on: list | None, tid: str) -> bool:
    for kind, ref in _iter_waiting_refs(waiting_on):
        if kind == "task" and _task_ref_matches(ref, tid):
            return True
    return False


def waiting_covers_assignee_task(
    waiting_on: list | None,
    tid: str,
    *,
    delegated_in_flight: list[dict] | None = None,
    assignee_status: str | None = None,
    claimed_assignee_ids: list[str] | None = None,
) -> bool:
    """True when waiting_on parks this agent's assignee obligation *tid*.

    Legal parks:
    - waiting on *tid* itself
    - waiting on a child whose ``parent_task_id`` matches *tid*
    - unlinked child (no parent): claimed *tid* only when it is this
      agent's **unique** claimed assignee obligation

    Linked children that point at another parent never use the fallback.
    ``running`` / ``rework`` never use the unlinked fallback.
    """
    if waiting_covers_task(waiting_on, tid):
        return True
    status = (assignee_status or "").strip().lower()
    claimed_ids = [
        str(x) for x in (claimed_assignee_ids or []) if x
    ]
    unique_claimed = (
        len(claimed_ids) == 1 and _task_ref_matches(claimed_ids[0], tid)
    )
    for child in delegated_in_flight or []:
        cid = str(child.get("id") or "")
        if not cid or not waiting_covers_task(waiting_on, cid):
            continue
        parent = str(child.get("parent_task_id") or "")
        if parent:
            if _task_ref_matches(parent, tid):
                return True
            continue
        if status == "claimed" and unique_claimed:
            return True
    return False


def waiting_on_live_external(
    waiting_on: list | None, *, agent_id: str | None = None
) -> bool:
    """True when waiting_on names a live off-turn job.

    Fake ``kind=external`` refs must not park forever — only in-flight
    native bg jobs (``is_live_job``) owned by *agent_id* when given.
    """
    refs = [ref for kind, ref in _iter_waiting_refs(waiting_on) if kind == "external"]
    if not refs:
        return False
    try:
        from hiveweave.services.offturn import is_live_job

        if any(is_live_job(ref, agent_id=agent_id) for ref in refs):
            return True
    except Exception:
        pass
    return False


def _live_offturn_covers_task(
    tid: str,
    waiting_on: list | None,
    agent_id: str | None,
) -> bool:
    """True when a live off-turn job bound to *tid* parks that obligation.

    Unbound jobs (empty task_id) never cover. Fake ``kind=external`` never
    covers. With *agent_id*, the registry is authoritative (the model may
    omit waiting_on). Without *agent_id*, only live external refs whose
    bound task_id equals *tid*.
    """
    if not tid:
        return False
    try:
        from hiveweave.services.offturn import (
            agent_has_live_job_for_task,
            is_live_job,
            job_bound_task_id,
        )

        if agent_id:
            return bool(agent_has_live_job_for_task(agent_id, tid))
        for kind, ref in _iter_waiting_refs(waiting_on):
            if kind != "external":
                continue
            if not is_live_job(ref):
                continue
            if job_bound_task_id(ref) == tid:
                return True
        return False
    except Exception:
        return False


def assignee_must_submit(
    phase: str,
    assignee_task_ids: list[str],
    waiting_on: list | None,
    *,
    agent_id: str | None = None,
    delegated_in_flight: list[dict] | None = None,
    assignee_status_by_id: dict[str, str] | None = None,
) -> bool:
    """ASSIGNEE_MUST_SUBMIT should fire for this exit.

    Legal park: waiting on each assignee task, a dispatched child of that
    task, or a live off-turn job bound to that task. Unbound / fake
    external waits do not park.
    """
    ids = [str(t) for t in assignee_task_ids if t]
    if not ids:
        return False
    if phase == "done_slice":
        return True
    if phase != "waiting":
        return False
    status_by_id = assignee_status_by_id or {}
    claimed_ids = [
        tid for tid in ids
        if (status_by_id.get(tid) or "").strip().lower() == "claimed"
    ]
    for tid in ids:
        if waiting_covers_assignee_task(
            waiting_on,
            tid,
            delegated_in_flight=delegated_in_flight,
            assignee_status=status_by_id.get(tid),
            claimed_assignee_ids=claimed_ids,
        ):
            continue
        if _live_offturn_covers_task(tid, waiting_on, agent_id):
            continue
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
    # Tasks this agent created and assigned to someone else (still open).
    # Waiting on one of these parks a claimed parent assignee obligation.
    delegated_in_flight: list[dict] = field(default_factory=list)
    tasks_advanced: set[str] = field(default_factory=set)
    # TEST11 #1a: recipients successfully messaged this turn (id/name/short_id)
    messaged_refs: set[str] = field(default_factory=set)
    # Outstanding outbound ask/expect_report recipients (unanswered contracts)
    outbound_ask_refs: set[str] = field(default_factory=set)
    # Optional name/short_id → id map for ref matching
    name_by_id: dict[str, str] = field(default_factory=dict)
    # Builder/executor worktree has uncommitted porcelain at done_slice
    worktree_uncommitted: bool = False
    # P0-2: CEO done_slice 项目级未推进工作（人可读条目；空 = 项目层面可收工）。
    # 仅 CEO 需要；由 completion 层在 done_slice 时计算注入。
    ceo_project_pending: list[str] = field(default_factory=list)


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
    replied_contracts: set[str] | None = None,
) -> list[dict]:
    """Messages that require a reply and were not answered this turn.

    Structural only: ``expect_report`` truthy (language-agnostic).
    ``message_type=ask`` alone is NOT sufficient — ask-chain downgrade
    writes notify + expect_report=0 so peers don't inherit a new obligation.

    - extra_replied_to: 本 turn 内已成功送达的收件人（来自 inbox 落库记录，
      即"成功调用 send_message/message 工具"的 DB 证据），与工具调用
      参数提取的 replied_to 合并判定。
    - exempt_senders: 豁免的发送方（已归档/不存在/user/system）——
      对归档 agent 的回复义务随其归档消亡；user/system 的回复通道是
      assistant 输出本身，不适用本门。
    - replied_contracts: 本 turn 内通过 reply_to 关闭的合约 ID 集合。
      如果消息的 reply_contract_id 在此集合中，视为已回复（确定性判定）。
    """
    name_by_id = name_by_id or {}
    exempt_senders = exempt_senders or set()
    replied_contracts = replied_contracts or set()
    expects: list[dict] = []
    for m in pending_msgs:
        fid = m.get("from_agent_id", "")
        if fid in exempt_senders:
            continue
        # Structural reply-required = expect_report only.
        # message_type=ask alone is insufficient: ask-chain downgrade writes
        # notify + expect_report=0; treating mt==ask would re-open the loop.
        if m.get("expect_report"):
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
        # Deterministic check: if reply_contract_id exists and has been closed.
        # 前缀容忍（TEST18 P0-3）：gate 提示展示 contract=<前 12 位>，LLM 用
        # 前缀传 replyTo 也能闭合 — 与 inbox.py 的软警告同款匹配语义。
        contract = m.get("reply_contract_id")
        if contract:
            cid = str(contract)
            if cid in replied_contracts or any(
                cid.startswith(rc) or rc.startswith(cid[:12])
                for rc in replied_contracts
                if rc
            ):
                continue  # Reply contract fulfilled — skip
        # Fallback heuristic: check if agent sent any message to the sender
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


def _ref_in_set(ref: str, candidates: set[str], name_by_id: dict[str, str] | None = None) -> bool:
    """True if wait.ref matches any candidate (id / short prefix / display name)."""
    if not ref or not candidates:
        return False
    r = ref.strip().lower()
    if not r:
        return False
    cand_l = {str(c).strip().lower() for c in candidates if c}
    if r in cand_l:
        return True
    for c in cand_l:
        if len(r) >= 4 and (c.startswith(r) or r.startswith(c)):
            return True
    # Resolve names: if ref is a name mapped to an id in candidates
    name_by_id = name_by_id or {}
    for aid, name in name_by_id.items():
        if str(name).strip().lower() == r and (
            aid.lower() in cand_l or aid in candidates
        ):
            return True
    return False


def _agent_wait_has_ask_evidence(ref: str, ctx: ExitContext) -> bool:
    """WAITING on an agent requires prior ask/message evidence (TEST11 #1a).

    Only DB-backed delivery evidence counts (messaged_refs / outbound asks).
    Tool-call argument fallback removed (audit M6) — intended recipients of a
    failed send must not satisfy the gate.

    Evidence windows are NOT this-turn-only (审计 H2 更正):
    - messaged_refs: 近 30 分钟内的送达（completion 预检窗口已并入，
      completion.py）——跨 turn 的消息同样算证据。
    - outbound_ask_refs: DB 全量未完结 ask。
    与异步预检口径一致；build_exit_contract_hint 会把 30 分钟窗口规则
    提前写进出口提示。
    """
    if _ref_in_set(ref, ctx.messaged_refs, ctx.name_by_id):
        return True
    if _ref_in_set(ref, ctx.outbound_ask_refs, ctx.name_by_id):
        return True
    return False


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

    if (
        turn_result is not None
        and turn_result.phase == "done_slice"
        and ctx.worktree_uncommitted
    ):
        violations.append("UNCOMMITTED_WORKTREE")

    # P0-2: CEO done_slice 还要求项目级无待推进工作（submitted 待审 /
    # verifying 待收口 / 待命叶子未派活）——只看 CEO 自己名下任务会漏判，
    # CEO 收工后这些工作无人推进，项目卡死。
    if (
        turn_result is not None
        and turn_result.phase == "done_slice"
        and ctx.ceo_project_pending
    ):
        violations.append("CEO_PROJECT_PENDING")

    # TEST11 #1a: waiting on an agent requires having asked/messaged them first
    wait_without_ask_refs: list[str] = []
    if turn_result is not None and turn_result.phase == "waiting":
        for w in turn_result.waiting_on or []:
            if (getattr(w, "kind", None) or "").lower() != "agent":
                continue
            ref = str(getattr(w, "ref", None) or "")
            if not ref:
                continue
            if not _agent_wait_has_ask_evidence(ref, ctx):
                if "WAIT_WITHOUT_ASK" not in violations:
                    violations.append("WAIT_WITHOUT_ASK")
                wait_without_ask_refs.append(ref)

    remaining_obligations = list(ctx.open_task_obligations)
    claimed_assignee_ids = [
        str(x.get("id") or "")
        for x in ctx.open_task_obligations
        if x.get("role_hint") == "assignee"
        and (x.get("status") or "") == "claimed"
        and x.get("id")
    ]
    if turn_result and turn_result.phase in ("done_slice", "waiting"):
        remaining = []
        for t in ctx.open_task_obligations:
            tid = str(t.get("id") or "")
            if _task_advanced(tid, ctx.tasks_advanced):
                continue
            # Explicit wait on this task is a legal idle exit for that task.
            if turn_result.phase == "waiting" and _waiting_on_task(
                turn_result, tid
            ):
                remaining.append(t)
                continue
            role = t.get("role_hint")
            status = t.get("status")
            # Live off-turn job bound to this task parks assignee
            # coding work only — not review/merge. Unbound jobs
            # and jobs bound to another task do not park this one.
            if (
                turn_result.phase == "waiting"
                and role == "assignee"
                and status in ("running", "claimed", "rework")
                and _live_offturn_covers_task(
                    tid, turn_result.waiting_on, ctx.agent_id
                )
            ):
                remaining.append(t)
                continue
            # Player-coach: waiting on a dispatched child parks the
            # coordinator's still-claimed umbrella (ASSIGNEE_MUST_SUBMIT).
            if (
                turn_result.phase == "waiting"
                and role == "assignee"
                and status in ("running", "claimed", "rework")
                and waiting_covers_assignee_task(
                    turn_result.waiting_on,
                    tid,
                    delegated_in_flight=ctx.delegated_in_flight,
                    assignee_status=str(status or ""),
                    claimed_assignee_ids=claimed_assignee_ids,
                )
            ):
                remaining.append(t)
                continue
            if role == "assignee" and status in (
                "running", "claimed", "rework",
            ):
                violations.append("ASSIGNEE_MUST_SUBMIT")
            elif role == "reviewer" and status == "submitted":
                violations.append("REVIEWER_MUST_START_REVIEW")
            elif role == "reviewer" and status == "reviewing":
                violations.append("REVIEWER_MUST_FINISH_REVIEW")
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
                        t.get("role_hint") == "reviewer"
                        and t.get("status") in ("submitted", "reviewing")
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
    # Soft-pass codes accepted earlier this turn must not re-block exit
    uniq = filter_soft_passed_violations(ctx.agent_id, uniq)

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
                or "WAIT_WITHOUT_ASK" in uniq
                or "MISSING_COMMIT_TURN" in uniq
                or "ASSIGNEE_MUST_SUBMIT" in uniq
                or "REVIEWER_MUST_START_REVIEW" in uniq
                or "REVIEWER_MUST_FINISH_REVIEW" in uniq
                or "CREATOR_MUST_REVIEW" in uniq
                or "CREATOR_MUST_MERGE" in uniq
                or "HIRE_UNREPORTED" in uniq
                or "UNCOMMITTED_WORKTREE" in uniq
                or "CEO_PROJECT_PENDING" in uniq
            )
            park = not repair_only
        if park:
            disposition = "runnable" if remaining_obligations else disposition
        try:
            from hiveweave.services.telemetry import telemetry

            for ref in wait_without_ask_refs:
                telemetry.gate_hard_reject(f"WAIT_WITHOUT_ASK:{ref}")
            if "UNREPLIED_ASKS" in uniq and unreplied:
                for m in unreplied[:8]:
                    sender = (
                        m.get("from_name")
                        or m.get("from_agent_id")
                        or "?"
                    )
                    telemetry.gate_hard_reject(f"UNREPLIED_ASKS:{sender}")
            for gate in uniq:
                if gate not in ("WAIT_WITHOUT_ASK", "UNREPLIED_ASKS"):
                    telemetry.gate_hard_reject(gate)
        except Exception as e:
            log.debug("gate_telemetry_failed", error=str(e))
        return ExitDecision(
            ok=False,
            violations=uniq,
            turn_result=turn_result,
            hint=_build_gate_hint(
                uniq,
                unreplied,
                turn_result,
                wait_without_ask_refs=wait_without_ask_refs,
                ceo_project_pending=ctx.ceo_project_pending,
                agent_id=ctx.agent_id,
            ),
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


# TEST19 ④: 单一来源的 gate→action 映射。commit_turn 同步 pre-check
# （tools/turn_tools.py）与 turn-exit backstop 共用，防漂移。
GATE_ACTIONS: dict[str, str] = {
    "MISSING_COMMIT_TURN": (
        "commit_turn(phase, summary, waiting_on/result as needed)"
    ),
    "INVALID_TURN_RESULT": "commit_turn with valid schema",
    "WAITING_ON_REQUIRED": "commit_turn(waiting, waiting_on=[...])",
    "BLOCKED_WAITING_ON_REQUIRED": "commit_turn(blocked, waiting_on=[...])",
    "UNREPLIED_ASKS": "ask_agent or send_message to sender REF",
    "WAIT_WITHOUT_ASK": "ask_agent or send_message to REF",
    "HIRE_UNREPORTED": "send_message/ask_agent to hiring requester",
    "OPEN_TASKS_UNDECLARED": "advance tasks or declare waiting/blocked",
    "ASSIGNEE_MUST_SUBMIT": (
        "submit_task(taskId) if YOU are executing; if already dispatched, "
        "commit_turn(waiting, waiting_on=[{kind:task, ref:childTaskId}]) "
        "— do not ask_agent the assignee to submit"
    ),
    "REVIEWER_MUST_START_REVIEW": "review_task(taskId, decision=...)",
    "REVIEWER_MUST_FINISH_REVIEW": "review_task(taskId, decision=...)",
    "CREATOR_MUST_REVIEW": "review_task(taskId, decision=...)",
    "CREATOR_MUST_MERGE": "git_worktree_merge(branchName=...)",
    "UNCOMMITTED_WORKTREE": "git_worktree_checkpoint then commit_turn",
    "CEO_PROJECT_PENDING": (
        "review_task 审查 submitted / 推进 VERIFY 收口 / dispatch_task 派活待命叶子"
    ),
}


def _build_gate_hint(
    violations: list[str],
    unreplied: list[dict],
    turn_result: TurnResult | None,
    *,
    wait_without_ask_refs: list[str] | None = None,
    ceo_project_pending: list[str] | None = None,
    agent_id: str | None = None,
) -> str:
    lines = [
        "[TURN EXIT BLOCKED]",
        "每一轮必须像函数一样返回 TurnResult。当前不能结束回合：",
    ]
    gate_actions = GATE_ACTIONS
    labels = {
        "MISSING_COMMIT_TURN": "未调用 commit_turn — 请提交 phase/summary（及必要的 waiting_on/result）",
        "INVALID_TURN_RESULT": "commit_turn 参数无效 — 请按 schema 重试",
        "WAITING_ON_REQUIRED": "phase=waiting 必须提供 waiting_on",
        "BLOCKED_WAITING_ON_REQUIRED": "phase=blocked 必须提供 waiting_on",
        "UNREPLIED_ASKS": "有人 ask 了你，必须用 ask_agent/notify_agent/send_message 回复后才能收工",
        "WAIT_WITHOUT_ASK": (
            "phase=waiting 且 waiting_on 含 agent，但未向对方发过消息"
            "（本轮送达或未完结 ask）— "
            "请先 ask_agent（带回复契约）或 send_message，再 commit_turn(waiting)"
        ),
        "HIRE_UNREPORTED": (
            "本轮调用了 hire_agent 但没有用 send_message/ask_agent/notify_agent 通知请求方 — "
            "招人完成≠协作完成；请向请求方汇报花名/shortId/role，再 commit_turn"
        ),
        "OPEN_TASKS_UNDECLARED": "仍有可行动任务 — 请推进任务，或用 phase=in_progress/waiting/blocked 声明状态（禁止假装 done_slice）",
        "ASSIGNEE_MUST_SUBMIT": (
            "有 running/claimed/rework 任务未 submit_task — "
            "若这是你自己在执行：submit_task(taskId, summary, testsPassed=true)。"
            "若已 dispatch 给下属：commit_turn(waiting) 挂 "
            "waiting_on=[{kind:'task', ref:子任务id}] "
            "（等子任务即可停泊你名下仍 claimed 的总包）。"
            "禁止 ask_agent/notify 催下属提交。"
            "off-turn 进行中：waiting_on 含 "
            "[{kind:'external', ref:'bg-sub-|bg-bash-…'}]"
        ),
        "REVIEWER_MUST_START_REVIEW": (
            "有 submitted 任务待你开始审查 — "
            "请调用 review_task(taskId, decision='approve'/'rework')，"
            "或 phase=waiting + waiting_on=[{kind:'task', ref:taskId}]"
        ),
        "REVIEWER_MUST_FINISH_REVIEW": (
            "有 reviewing 任务待你完成审查 — "
            "请调用 review_task(taskId, decision='approve'/'rework')，"
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
        "UNCOMMITTED_WORKTREE": _format_uncommitted_worktree_label(agent_id),
        "CEO_PROJECT_PENDING": (
            "项目仍有未推进工作，CEO 不能收工 — 请逐项推进："
            "审查 submitted 任务 / 推进 VERIFY 收口 / 给待命叶子派活；"
            "如确需等待他人推进，改用 phase=waiting + waiting_on 声明等待对象"
        ),
    }

    emitted_unreplied = False
    for v in violations:
        if v == "UNREPLIED_ASKS" and unreplied:
            for m in unreplied[:8]:
                name = m.get("from_name") or (m.get("from_agent_id") or "?")[:8]
                lines.append(
                    f"GATE=UNREPLIED_ASKS REF={name} "
                    f"MISSING={gate_actions[v]}"
                )
                preview = (m.get("message") or "").replace("\n", " ").strip()[:60]
                cid = (m.get("reply_contract_id") or "")[:12]
                # TEST18 P0-3: 附合同 ID — 用 send_message/ask_agent 的
                # replyTo 参数原样传回即闭合；已回执过则不再重复回执。
                if not preview:
                    preview = (
                        f"(body not in this turn — replyTo={cid or '?'})"
                    )
                    cid_hint = ""
                else:
                    cid_hint = f" contract={cid}" if cid else ""
                lines.append(f"  ❌ {name}：{preview}{cid_hint}")
            lines.append(
                "  回复方式：send_message/ask_agent/notify_agent 传 "
                "replyTo=<上方 contract 值>（原消息的 reply_contract_id，"
                "不是工具返回的 message_id）。已回执过则直接 commit_turn。"
            )
            emitted_unreplied = True
            continue
        if v == "WAIT_WITHOUT_ASK" and wait_without_ask_refs:
            for ref in wait_without_ask_refs:
                lines.append(
                    f"GATE=WAIT_WITHOUT_ASK REF={ref} "
                    f"MISSING={gate_actions[v]}"
                )
            if v in labels:
                lines.append(f"  {labels[v]}")
            continue
        if v == "CEO_PROJECT_PENDING" and ceo_project_pending:
            lines.append(
                f"GATE=CEO_PROJECT_PENDING REF=- MISSING={gate_actions[v]}"
            )
            for item in ceo_project_pending[:6]:
                lines.append(f"  ❌ {item}")
            if v in labels:
                lines.append(f"  {labels[v]}")
            continue
        ref = "-"
        lines.append(
            f"GATE={v} REF={ref} MISSING={gate_actions.get(v, 'fix and commit_turn')}"
        )
        if v in labels:
            lines.append(f"  {labels[v]}")

    if "UNREPLIED_ASKS" in violations and unreplied and not emitted_unreplied:
        lines.append("未回复：")
        for m in unreplied[:8]:
            name = m.get("from_name") or (m.get("from_agent_id") or "?")[:8]
            preview = (m.get("message") or "").replace("\n", " ").strip()[:60]
            cid = (m.get("reply_contract_id") or "")[:12]
            if not preview:
                preview = f"(body not in this turn — replyTo={cid or '?'})"
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


async def agent_worktree_has_uncommitted(
    agent_id: str, project_id: str
) -> bool:
    """True when agent with write worktree has porcelain-dirty tree."""
    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.git_worktree import (
            GitWorktreeService,
            agent_gets_write_worktree,
        )
        from hiveweave.services.org import OrgService

        agent = await OrgService().resolve_agent(agent_id)
        if not agent or not agent_gets_write_worktree(agent):
            return False
        short_id = (agent.get("short_id") or "").strip()
        if not short_id:
            return False
        ws = await meta_db.get_project_workspace(project_id)
        if not ws:
            return False
        info = await GitWorktreeService().info(ws, short_id)
        fallback = ""
        try:
            from pathlib import Path

            from hiveweave.services.git_worktree.constants import WORKTREE_DIR

            fallback = str(Path(ws) / WORKTREE_DIR / short_id)
        except Exception:
            fallback = ""
        hinted = _hint_from_worktree_info(info, fallback)
        _worktree_hint_details[agent_id] = hinted
        if hinted.get("git_error"):
            return False
        return bool(hinted.get("dirty"))
    except Exception as e:
        log.debug("worktree_uncommitted_check_failed", error=str(e))
        return False


# ── P0-2: CEO 项目级义务检查 ─────────────────────────────

# 最近一次预检算出的项目级待办明细（agent_id → 人可读条目），供
# commit_turn 同步拒绝时拼进提示。覆写式更新，仅 advisory。
_ceo_project_pending_details: dict[str, list[str]] = {}

# UNCOMMITTED_WORKTREE hint extras (files / path / git_status_error).
_worktree_hint_details: dict[str, dict] = {}

# 叶子「待命未派活」宽限期：招聘后 10 分钟内零任务不算待派活（onboarding 抖动）
_CEO_IDLE_LEAF_GRACE_MS = 10 * 60 * 1000


def _hint_from_worktree_info(info: object, fallback_path: str = "") -> dict:
    """Parse GitWorktreeService.info() via .get() — fail-open on old shape."""
    details: dict = {
        "dirty": False,
        "files": [],
        "path": fallback_path,
        "git_error": None,
    }
    status = info.get("status") if isinstance(info, dict) else None
    if not isinstance(status, dict):
        return details
    path = status.get("path") or status.get("worktree_path") or fallback_path
    details["path"] = str(path or fallback_path)
    git_err = status.get("git_status_error")
    if git_err:
        details["git_error"] = (
            "git status failed"
            if git_err is True
            else str(git_err)
        )
        details["dirty"] = False
        return details
    files = status.get("uncommitted_files")
    if isinstance(files, list):
        details["files"] = [str(f) for f in files if f][:5]
    details["dirty"] = bool(status.get("has_uncommitted"))
    return details


def _format_uncommitted_worktree_label(agent_id: str | None) -> str:
    base = (
        "你的 worktree 有未提交改动 — 请先 git_worktree_checkpoint，"
        "再 commit_turn(done_slice)"
    )
    details = _worktree_hint_details.get(agent_id or "") if agent_id else None
    if not details:
        return base
    git_err = details.get("git_error")
    path = details.get("path") or ""
    files = details.get("files") or []
    if git_err:
        loc = f" at {path}" if path else ""
        return f"git status failed{loc} (not necessarily dirty): {git_err}"
    extra: list[str] = []
    if path:
        extra.append(f"path={path}")
    if files:
        extra.append("files=" + ", ".join(str(f) for f in files[:4]))
    if extra:
        return f"{base} ({'; '.join(extra)})"
    return base


def pop_ceo_project_pending_details(agent_id: str) -> list[str]:
    """Pop the most recent CEO project-level pending details (advisory)."""
    return _ceo_project_pending_details.pop(agent_id, [])


async def ceo_project_pending_obligations(
    project_id: str, agent_id: str
) -> list[str]:
    """P0-2: 项目级义务（仅 CEO 在 done_slice 收工前检查）。

    CEO 的「无待办」不能只看自己名下任务 —— 以下项目级状态只有 CEO
    层级能推进，CEO 收工会把项目卡死：
    1. submitted 任务待审查（审查链终点在 CEO）
    2. verifying 任务待收口（VERIFY 串行泵需要管理层推进）
    3. 待命叶子未派活（active 叶子超过宽限期仍零未归档任务）

    非 CEO 一律返回 []（仅一次角色查询，零任务 SQL）。任一子查询失败
    fail-open（跳过该项），绝不阻塞收工。返回人可读条目列表；
    空列表 = 项目层面可以收工。
    """
    try:
        from hiveweave.services.org import OrgService
        from hiveweave.services.policy import infer_role_family

        agent = await OrgService().get_agent(agent_id)
        if not agent or infer_role_family(agent) != "ceo":
            return []
    except Exception as e:
        log.debug("ceo_project_pending_role_check_failed", error=str(e))
        return []

    pending: list[str] = []
    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(project_id)
    except Exception as e:
        log.debug("ceo_project_pending_db_failed", error=str(e))
        return pending

    # 1. submitted 任务（待审查 — 审查链终点在 CEO）
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS c FROM tasks "
            "WHERE is_archived = 0 AND status = 'submitted'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        count = int(row["c"] or 0) if row else 0
        if count > 0:
            pending.append(f"项目有 {count} 个 submitted 任务待审查")
    except Exception as e:
        log.debug("ceo_project_pending_submitted_failed", error=str(e))

    # 2. verifying 任务（待收口）
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS c FROM tasks "
            "WHERE is_archived = 0 AND status = 'verifying'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        count = int(row["c"] or 0) if row else 0
        if count > 0:
            pending.append(f"项目有 {count} 个 verifying 任务待收口")
    except Exception as e:
        log.debug("ceo_project_pending_verifying_failed", error=str(e))

    # 3. 待命叶子未派活（active + 超过宽限期 + 名下零未归档任务）。
    #    叶子 = executor/qa family（role 是中文职称，不能靠 SQL 字符串
    #    排除 CEO/HR/中层，统一走 infer_role_family 判定）。
    try:
        import time as _time

        grace_cutoff = int(_time.time() * 1000) - _CEO_IDLE_LEAF_GRACE_MS
        cursor = await conn.execute(
            "SELECT id, name, short_id, role, permission_type FROM agents a "
            "WHERE a.status = 'active' "
            "AND a.created_at IS NOT NULL AND a.created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM tasks t "
            "  WHERE t.assignee_id = a.id AND t.is_archived = 0) "
            "AND NOT EXISTS (SELECT 1 FROM tasks t "
            "  WHERE t.creator_id = a.id AND t.is_archived = 0) "
            "LIMIT 50",
            [grace_cutoff],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        idle_leaves = [
            r
            for r in rows
            if infer_role_family(
                {
                    "role": r["role"],
                    "permission_type": r["permission_type"],
                }
            )
            in ("executor", "qa")
        ][:3]
        if idle_leaves:
            names = ", ".join(
                f"{r['name']}({r['short_id'] or '?'})" for r in idle_leaves
            )
            pending.append(f"项目有待命叶子未派活: {names}")
    except Exception as e:
        log.debug("ceo_project_pending_idle_leaves_failed", error=str(e))

    return pending


async def pre_check_exit_gates(
    agent_id: str,
    project_id: str,
    phase: str,
    waiting_on: list | None = None,
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
        from hiveweave.services import task as task_module

        # Ensure task migrations (reviewer_id etc.) before obligation queries
        try:
            await task_module._ensure_schema(project_id)
        except Exception as e:
            log.debug("pre_check_task_schema_failed", error=str(e))

        conn = await project_db.get_project_db_by_project_id(project_id)

        # Resolve agents once for name/short_id enrichment (TEST14 P0b)
        name_by_id: dict[str, str] = {}
        short_by_id: dict[str, str] = {}
        try:
            cursor = await conn.execute(
                "SELECT id, name, short_id FROM agents "
                "WHERE status IS NULL OR status != 'archived'"
            )
            agent_rows = await cursor.fetchall()
            await cursor.close()
            for a in agent_rows:
                aid = a["id"] if "id" in a.keys() else None
                if not aid:
                    continue
                if a["name"] if "name" in a.keys() else None:
                    name_by_id[aid] = str(a["name"])
                if a["short_id"] if "short_id" in a.keys() else None:
                    short_by_id[aid] = str(a["short_id"])
        except Exception as e:
            log.debug("pre_check_agent_map_failed", error=str(e))

        def enrich_id_set(ids: set[str]) -> set[str]:
            """UUID set → UUID + flower-name + short_id (for _ref_in_set)."""
            out = set(ids)
            for aid in list(ids):
                n = name_by_id.get(aid)
                if n:
                    out.add(n)
                s = short_by_id.get(aid)
                if s:
                    out.add(s)
            return out

        # 0. WAIT_WITHOUT_ASK: waiting on agent requires prior message/ask
        if phase == "waiting" and waiting_on:
            agent_refs: list[str] = []
            for w in waiting_on:
                if isinstance(w, dict):
                    kind = str(w.get("kind") or "").lower()
                    ref = str(w.get("ref") or "")
                else:
                    kind = str(getattr(w, "kind", "") or "").lower()
                    ref = str(getattr(w, "ref", "") or "")
                if kind == "agent" and ref:
                    agent_refs.append(ref)
            if agent_refs:
                try:
                    from hiveweave.services.inbox import InboxService

                    inbox = InboxService()
                    # Recent deliveries (30 min) + outstanding outbound asks
                    import time as _time

                    since = int(_time.time() * 1000) - 30 * 60 * 1000
                    sent = await inbox.get_sent_recipients_since(agent_id, since)
                    outstanding = await inbox.get_outstanding_ask_recipients(
                        agent_id
                    )
                    evidence = enrich_id_set(set(sent) | set(outstanding))
                    for ref in agent_refs:
                        if not _ref_in_set(ref, evidence, name_by_id):
                            violations.append("WAIT_WITHOUT_ASK")
                            break
                except Exception as e:
                    log.debug("pre_check_wait_without_ask_failed", error=str(e))

        # 1. Unreplied asks — contract-based (TEST14 P1a), not read=0.
        #    read≠replied: marking read must not clear the reply obligation.
        #    口径需与 backstop (collect_unreplied_asks) 对齐：backstop 允许
        #    "已向 sender 发过消息" 即放行（:196 fallback）。预检若纯合约口径
        #    会误拒走普通 send_message 回复的 agent → 撞门循环（二审 R1）。
        #    这里补充近 30 分钟已送达收件人豁免，与 backstop fallback 同款。
        try:
            from hiveweave.services.inbox import InboxService

            inbox = InboxService()
            senders = await inbox.get_outstanding_ask_senders(agent_id)
            if senders:
                import time as _ts

                _sent = await inbox.get_sent_recipients_since(
                    agent_id, int(_ts.time() * 1000) - 30 * 60 * 1000
                )
                # 已向 outstanding sender 发过消息即视为已回复（与 backstop
                # 的 replied_to fallback 语义一致），从违规集合中剔除。
                outstanding = senders - _sent
                if outstanding:
                    violations.append("UNREPLIED_ASKS")
        except Exception as e:
            log.debug("pre_check_unreplied_asks_failed", error=str(e))

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
        assignee_ids: list[str] = []
        assignee_status_by_id: dict[str, str] = {}
        for row in assignee_tasks:
            if hasattr(row, "keys") and "id" in row.keys():
                tid = str(row["id"])
                st = str(row["status"]) if "status" in row.keys() else ""
            else:
                tid = str(row[0])
                st = str(row[1]) if len(row) > 1 else ""
            assignee_ids.append(tid)
            if st:
                assignee_status_by_id[tid] = st
        delegated_in_flight: list[dict] = []
        try:
            delegated_in_flight = (
                await task_module.TaskService().list_delegated_in_flight(
                    project_id, agent_id
                )
            )
        except Exception as e:
            log.debug("pre_check_delegated_in_flight_failed", error=str(e))
        if assignee_must_submit(
            phase,
            assignee_ids,
            waiting_on,
            agent_id=agent_id,
            delegated_in_flight=delegated_in_flight,
            assignee_status_by_id=assignee_status_by_id,
        ):
            violations.append("ASSIGNEE_MUST_SUBMIT")

        if phase == "done_slice":
            if await agent_worktree_has_uncommitted(agent_id, project_id):
                violations.append("UNCOMMITTED_WORKTREE")

        # 3. Open task obligations: submitted|reviewing as reviewer (TEST11 #3)
        cursor = await conn.execute(
            "SELECT id, status FROM tasks "
            "WHERE reviewer_id = ? AND is_archived = 0 "
            "AND status IN ('submitted', 'reviewing') "
            "LIMIT 20",
            [agent_id],
        )
        reviewer_tasks = await cursor.fetchall()
        await cursor.close()
        if reviewer_tasks and phase in ("done_slice", "waiting"):
            statuses = {r["status"] for r in reviewer_tasks}
            if "submitted" in statuses:
                violations.append("REVIEWER_MUST_START_REVIEW")
            if "reviewing" in statuses:
                violations.append("REVIEWER_MUST_FINISH_REVIEW")

        # 4. Open task obligations: submitted/reviewing/approved as creator
        #    (skip review when designated reviewer ≠ this creator)
        cursor = await conn.execute(
            "SELECT id, status, reviewer_id FROM tasks "
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
            need_review = False
            need_merge = False
            for r in creator_tasks:
                st = r["status"]
                rid = r["reviewer_id"] if "reviewer_id" in r.keys() else None
                if st in ("submitted", "reviewing"):
                    if not rid or rid == agent_id:
                        need_review = True
                if st == "approved":
                    need_merge = True
            if need_review:
                violations.append("CREATOR_MUST_REVIEW")
            if need_merge:
                violations.append("CREATOR_MUST_MERGE")

        # 5. P0-2: CEO done_slice 项目级义务 —— 只看 CEO 名下任务会漏掉
        #    项目整体待推进工作（submitted 待审 / verifying 待收口 /
        #    待命叶子未派活），导致 CEO 收工后项目卡死。非 CEO 零开销。
        if phase == "done_slice":
            try:
                project_pending = await ceo_project_pending_obligations(
                    project_id, agent_id
                )
                if project_pending:
                    violations.append("CEO_PROJECT_PENDING")
                    _ceo_project_pending_details[agent_id] = project_pending
            except Exception as e:
                log.debug("pre_check_ceo_project_pending_failed", error=str(e))

    except Exception as e:
        log.debug("pre_check_exit_gates_failed", error=str(e))
        # Keep violations found before the failure (e.g. WAIT_WITHOUT_ASK)
        return violations

    return violations


# ── 回合开始前置注入（F1）─────────────────────────────────
# 数据源与 gate 同口径；best-effort，任何失败静默降级，
# 绝不阻塞回合开始。


def _fmt_ids(ids: list[str], limit: int = 4) -> str:
    """ID 列表 → "#a、#b、#c 等N个"（超限截断，防上下文膨胀）。"""
    shown = [f"#{i}" for i in ids[:limit]]
    if len(ids) > limit:
        return f"{'、'.join(shown)} 等{len(ids)}个"
    return "、".join(shown)


async def _unreplied_ask_contracts(agent_id: str) -> list[dict]:
    """Outstanding asks with sender + body snippet (read=1 still open).

    Same口径 as get_outstanding_ask_messages: expect_report=1 and
    reply_contract_id not closed via reply_to (legacy unread if no
    contract). Does not change read flags.
    """
    from hiveweave.services.inbox import InboxService

    msgs = await InboxService().get_outstanding_ask_messages(agent_id, limit=20)
    out: list[dict] = []
    for m in msgs:
        cid = m.get("reply_contract_id")
        snippet = m.get("snippet") or m.get("message") or ""
        snippet = str(snippet).replace("\n", " ").strip()[:80]
        fid = str(m.get("from_agent_id") or "")
        out.append({
            "contract": (str(cid)[:12] if cid else (fid[:8] or "?")),
            "from_agent_id": fid,
            "from_name": str(m.get("from_name") or ""),
            "snippet": snippet,
        })
    return out


def format_unreplied_ask_reject_suffix(asks: list[dict]) -> str:
    """Sender + body + contract for commit_turn REJECTED (UNREPLIED_ASKS)."""
    if not asks:
        return ""
    bits: list[str] = []
    for a in asks[:4]:
        name = a.get("from_name") or ""
        fid = (a.get("from_agent_id") or "?")[:8]
        who = f"{name} ({fid})" if name else fid
        cid = a.get("contract") or ""
        snippet = (a.get("snippet") or "").strip()
        if not snippet:
            snippet = f"(body not in this turn — replyTo={cid or '?'})"
        cid_bit = f" contract={cid}" if cid else ""
        bits.append(f"{who}{cid_bit} 「{snippet}」")
    return " 未回复详情: " + "；".join(bits)


async def _worktree_dirty_flag(agent_id: str, project_id: str) -> dict:
    """Worktree dirty details for the turn-start hint. Fail-open.

    Prefers GitWorktreeService.info() fields via .get() (uncommitted_files /
    git_status_error from a parallel agent). Old info() shape → generic
    dirty from has_uncommitted. git_status_error means status failed, not
    necessarily dirty.
    """
    details: dict = {
        "dirty": False,
        "files": [],
        "path": "",
        "git_error": None,
    }
    from pathlib import Path

    from hiveweave.services.agent_router import agent_router
    from hiveweave.services.git_worktree.constants import WORKTREE_DIR

    try:
        route = agent_router.get_route(agent_id)
        if not route or not route.workspace_path or not route.short_id:
            return details
        wt = str(Path(route.workspace_path) / WORKTREE_DIR / route.short_id)
        details["path"] = wt
        try:
            from hiveweave.services.git_worktree import GitWorktreeService

            # info() resolves A010-b relocation; do not require canonical .git.
            info = await GitWorktreeService().info(
                route.workspace_path, route.short_id
            )
            if isinstance(info, dict) and isinstance(info.get("status"), dict):
                hinted = _hint_from_worktree_info(info, wt)
                _worktree_hint_details[agent_id] = hinted
                return hinted
        except Exception as e:
            log.debug(
                "exit_contract_worktree_info_failed",
                agent_id=agent_id,
                error=str(e),
            )
        from hiveweave.services.git_worktree.git_cmd import _git
        from hiveweave.services.git_worktree.porcelain import (
            _porcelain_tracked_dirty_paths,
        )

        ok_st, st_out = await _git(
            ["-c", "core.quotepath=false", "status", "--porcelain", "-z"], wt
        )
        if not ok_st:
            details["git_error"] = "git status failed"
            details["dirty"] = False
            _worktree_hint_details[agent_id] = details
            return details
        files = _porcelain_tracked_dirty_paths(st_out or "")
        details["files"] = files[:5]
        details["dirty"] = bool(files)
        _worktree_hint_details[agent_id] = details
        return details
    except Exception as e:
        log.debug(
            "exit_contract_worktree_flag_failed",
            agent_id=agent_id,
            error=str(e),
        )
        return details


async def build_exit_contract_hint(agent_id: str, project_id: str) -> str:
    """回合开始时的出口条件提示（F1）— agent 收工决策前可见本轮 gate 要求。

    数据源与 gate 一致（ask 合约 / actionable obligations / worktree
    dirty flag）。任一源失败则忽略该项；全部失败返回空串（不注入）。
    无待办时返回单行「仅需提交 commit_turn」，不膨胀上下文。
    """
    asks: list[dict] = []
    obligations: list[dict] = []
    checked = 0
    try:
        asks = await _unreplied_ask_contracts(agent_id)
        checked += 1
    except Exception as e:
        log.debug("exit_contract_asks_failed", agent_id=agent_id, error=str(e))
    try:
        from hiveweave.services.task import TaskService

        obligations = await TaskService().get_actionable_obligations(
            project_id, agent_id
        )
        checked += 1
    except Exception as e:
        log.debug(
            "exit_contract_obligations_failed",
            agent_id=agent_id,
            error=str(e),
        )
    if not checked:
        return ""
    wt: dict = {"dirty": False, "files": [], "path": "", "git_error": None}
    try:
        wt_raw = await _worktree_dirty_flag(agent_id, project_id)
        if isinstance(wt_raw, dict):
            wt = wt_raw
        else:
            wt = {
                "dirty": bool(wt_raw),
                "files": [],
                "path": "",
                "git_error": None,
            }
    except Exception as e:
        log.debug(
            "exit_contract_worktree_flag_failed",
            agent_id=agent_id,
            error=str(e),
        )
    wt_dirty = bool(wt.get("dirty"))
    wt_git_err = wt.get("git_error")
    # P0-2 疏通层：CEO 在收工决策前就可见项目级待办（与 done_slice 门禁
    # 同口径），避免撞门后才知道项目还有未推进工作。非 CEO 返回 []。
    ceo_pending: list[str] = []
    try:
        ceo_pending = await ceo_project_pending_obligations(
            project_id, agent_id
        )
    except Exception as e:
        log.debug(
            "exit_contract_ceo_pending_failed", agent_id=agent_id, error=str(e)
        )
    if (
        not asks
        and not obligations
        and not wt_dirty
        and not wt_git_err
        and not ceo_pending
    ):
        return (
            "【本轮出口条件】无未回复 ask / 未完成义务 / 未提交 worktree："
            "仅需提交 commit_turn 收尾。\n"
            "commit_turn 用法：commit_turn(phase='done_slice'|'waiting'|'blocked', "
            "summary='…')——assistant 纯文本不是返回值，遗漏会触发 TURN EXIT BLOCKED。\n"
            "⚠️ 若要用 phase='waiting' 等待某个 agent（waiting_on 声明 agent），"
            "必须先在本轮或近 30 分钟内向该 agent 发过消息/ask，否则被 "
            "WAIT_WITHOUT_ASK 拒绝；等待任务完成请用 commit_turn(waiting, waiting_on=[task 引用])"
        )
    items: list[str] = ["必须提交 commit_turn"]
    if asks:
        bits: list[str] = []
        for a in asks[:4]:
            name = a.get("from_name") or ""
            fid = (a.get("from_agent_id") or "?")[:8]
            who = f"{name} ({fid})" if name else fid
            cid = a.get("contract") or ""
            snippet = (a.get("snippet") or "").strip()
            if not snippet:
                snippet = f"(body not in this turn — replyTo={cid or '?'})"
            cid_bit = f" contract={cid}" if cid else ""
            bits.append(f"{who}{cid_bit} 「{snippet}」")
        items.append(f"未回复 ask: {len(asks)} 个（{'；'.join(bits)}）")
    if obligations:
        tids = [str(o.get("id") or "")[:8] for o in obligations if o.get("id")]
        items.append(f"未完成义务: {len(obligations)} 个（{_fmt_ids(tids)}）")
    if wt_git_err:
        loc = f" at {wt.get('path')}" if wt.get("path") else ""
        items.append(f"git status failed{loc} (not necessarily dirty)")
    if wt_dirty:
        extra_bits: list[str] = []
        if wt.get("path"):
            extra_bits.append(str(wt["path"]))
        files = wt.get("files") or []
        if files:
            extra_bits.append(", ".join(str(f) for f in files[:4]))
        extra = f"（{'；'.join(extra_bits)}）" if extra_bits else ""
        items.append(f"worktree 有未提交改动{extra}（done_slice 前需 checkpoint）")
    if ceo_pending:
        items.append(
            f"项目级待办（done_slice 前须推进）: {len(ceo_pending)} 项"
            f"（{'；'.join(ceo_pending[:3])}）"
        )
    # 与空分支共用同一条 waiting 规则（审计 P2：此前有 ask/义务的 agent
    # 拿不到 WAIT_WITHOUT_ASK 预告）
    items.append(
        "若用 phase='waiting' 等待 agent：须先在本轮或近 30 分钟向该 agent "
        "发过消息/ask，否则 WAIT_WITHOUT_ASK 拒绝；等待任务用 "
        "waiting_on=[task 引用]"
    )
    body = "；".join(f"{i}) {it}" for i, it in enumerate(items, 1))
    return f"【本轮出口条件】{body}"
