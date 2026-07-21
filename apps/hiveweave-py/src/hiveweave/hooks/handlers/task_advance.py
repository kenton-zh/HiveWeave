"""agent.turn.after — nudge when actionable tasks were not advanced.

Does **not** invent intent or auto-dispatch. Only observes ledger + tools,
then may set ``output["hint"]`` for the agent to re-enter chat.

Skip (no false positives):
- no actionable obligations
- legal wait disposition (waiting_*) or phase=waiting
  (phase/disposition ``blocked`` does NOT skip — agents must defer_task_advance
   or submit; fake-blocked with open claimed tasks was a TEST7 failure mode)
- higher-priority retrigger already scheduled (gate repair / continue slice)
- agent called ``defer_task_advance`` (不推进) or wake-cycle defer flag is set
- every remaining obligation was advanced this turn

Writing code without ledger movement (submit / update_progress / …) still
nudges — otherwise agents can talk-complete forever (assignee_worked removed).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from hiveweave.hooks import AGENT_TURN_AFTER, hooks

log = structlog.get_logger(__name__)

DEFER_TOOL = "defer_task_advance"

# Tools that count as advancing a specific task id (ledger movement).
_LEDGER_ADVANCE_TOOLS = frozenset({
    "submit_task",
    "review_task",
    "claim_task",
    "dispatch_task",
    "close_task",
    "update_task_status",
    "update_progress",
    "cancel_task",
    "unclaim_task",
    "git_worktree_merge",
})

# Assignee did real work this turn even without submit yet → not "idle talk".
_ASSIGNEE_WORK_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "bash",
    "run_command",
    "apply_patch",
    "run_tests",
    "git_worktree_checkpoint",
    "git_worktree_merge",
    "claim_task",
    "submit_task",
    "update_task_status",
    "update_progress",
})

# Legal idle — do not nag. ``blocked`` intentionally omitted: use
# defer_task_advance or commit_turn(waiting)+waiting_on, not silent blocked.
_LEGAL_WAIT_DISPOSITIONS = frozenset({
    "waiting_human",
    "waiting_agent",
    "waiting_timer",
})


def _tool_name(tc: dict) -> str:
    if not isinstance(tc, dict):
        return ""
    func = tc.get("function") or {}
    if isinstance(func, dict) and func.get("name"):
        return str(func["name"])
    return str(tc.get("name") or "")


def _tool_args(tc: dict) -> dict:
    import json

    if not isinstance(tc, dict):
        return {}
    func = tc.get("function") or {}
    raw = func.get("arguments") if isinstance(func, dict) else tc.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def called_defer_task_advance(tool_calls: list | None) -> bool:
    for tc in tool_calls or []:
        if _tool_name(tc) == DEFER_TOOL:
            return True
    return False


def task_ids_advanced(tool_calls: list | None) -> set[str]:
    """Task IDs that ledger tools touched this turn."""
    advanced: set[str] = set()
    for tc in tool_calls or []:
        name = _tool_name(tc)
        if name not in _LEDGER_ADVANCE_TOOLS:
            continue
        args = _tool_args(tc)
        tid = args.get("taskId") or args.get("task_id") or args.get("id")
        if tid:
            advanced.add(str(tid))
    return advanced


def _id_match(tid: str, advanced: set[str]) -> bool:
    if tid in advanced:
        return True
    return any(
        tid.startswith(a) or a.startswith(tid)
        for a in advanced
        if len(a) >= 8
    )


def remaining_obligations(
    obligations: list[dict],
    advanced: set[str],
) -> list[dict]:
    out: list[dict] = []
    for t in obligations or []:
        tid = str(t.get("id") or "")
        if not tid or _id_match(tid, advanced):
            continue
        out.append(t)
    return out


def had_assignee_work(tool_calls: list | None) -> bool:
    for tc in tool_calls or []:
        if _tool_name(tc) in _ASSIGNEE_WORK_TOOLS:
            return True
    return False


def build_task_advance_hint(obligations: list[dict]) -> str:
    lines = [
        "[TASK ADVANCE]",
        "本轮结束时你仍有可行动任务，但没有推动它们。"
        "请用工具推进账本，或显式 commit_turn(phase=waiting|blocked)+waiting_on。"
        "若此刻确实无法推进：调用 defer_task_advance(reason=…)（不推进）——"
        "之后不会再循环提醒，直到你被再次唤醒。",
        "可先 `read_skill(\"task-advance\")`。",
    ]
    for t in obligations[:8]:
        tid = str(t.get("id") or "")
        title = (t.get("title") or "(untitled)").split("\n")[0][:60]
        status = t.get("status") or "?"
        role = t.get("role_hint") or "assignee"
        progress = t.get("progress")
        prog = f" progress={progress}%" if progress is not None else ""
        if role == "creator":
            if status == "approved":
                next_step = (
                    "立即 git_worktree_merge(branchName=shortId 或 hw/...)；"
                    "禁止让 executor 在 main 上 merge"
                )
            else:
                next_step = "用 review_task(taskId, decision, feedback) 审批"
        elif status == "rework":
            next_step = "按反馈返工后重新 submit_task"
        elif status == "claimed":
            next_step = "update_task_status(running) 后继续执行 / submit_task"
        elif status == "verifying":
            next_step = "执行 VERIFY 后 submit_task / review"
        else:
            next_step = "继续执行或 submit_task / 合法 waiting / defer_task_advance"
        lines.append(
            f"- [{status}] {tid[:8]}… ({role}){prog} {title} → {next_step}"
        )
    if len(obligations) > 8:
        lines.append(f"- …还有 {len(obligations) - 8} 条未列出")
    return "\n".join(lines)


def decide_task_advance_nudge(
    *,
    open_obligations: list[dict],
    tool_calls: list | None,
    tasks_advanced: set[str] | None = None,
    phase: str | None,
    disposition: str | None,
    gate_repairing: bool,
    continue_slice: bool,
    deferred: bool = False,
    reminder_count: int = 0,
    reminder_max: int = 2,
) -> tuple[str | None, str]:
    """Return (hint_or_None, skip_reason). skip_reason is '' when nudging."""
    if gate_repairing:
        return None, "gate_repairing"
    if continue_slice:
        return None, "continue_slice"
    if deferred or called_defer_task_advance(tool_calls):
        return None, "deferred"
    if not open_obligations:
        return None, "no_obligations"
    # phase=blocked does not skip — must defer or advance ledger (TEST7 千寻)
    if phase == "waiting":
        return None, "declared_wait"
    if (disposition or "") in _LEGAL_WAIT_DISPOSITIONS:
        return None, f"disposition_{disposition}"
    # Safety only: same wake cycle should not nudge forever if model ignores
    # defer_task_advance. Cleared on next external wake (not a hard block).
    if reminder_count >= reminder_max:
        return None, "cap"

    advanced = set(tasks_advanced or ()) | task_ids_advanced(tool_calls)
    remaining = remaining_obligations(open_obligations, advanced)
    if not remaining:
        return None, "all_advanced"

    return build_task_advance_hint(remaining), ""


async def on_agent_turn_after(
    input: Mapping[str, Any],
    output: MutableMapping[str, Any],
) -> None:
    """Mutate output with optional ``hint`` / ``skip_reason``."""
    advanced_raw = input.get("tasks_advanced") or []
    advanced = {str(x) for x in advanced_raw} if advanced_raw else set()
    hint, skip = decide_task_advance_nudge(
        open_obligations=list(input.get("open_obligations") or []),
        tool_calls=input.get("tool_calls"),
        tasks_advanced=advanced,
        phase=input.get("phase"),
        disposition=input.get("disposition"),
        gate_repairing=bool(input.get("gate_repairing")),
        continue_slice=bool(input.get("continue_slice")),
        deferred=bool(input.get("deferred")),
        reminder_count=int(input.get("reminder_count") or 0),
        reminder_max=int(input.get("reminder_max") or 2),
    )
    output["skip_reason"] = skip
    if hint:
        output["hint"] = hint
        output["nudge_kind"] = "task_advance"
        log.info(
            "hook_task_advance_nudge",
            agent_id=input.get("agent_id"),
            skip_reason="",
        )
    else:
        output.setdefault("hint", None)
        log.debug(
            "hook_task_advance_skip",
            agent_id=input.get("agent_id"),
            skip_reason=skip,
        )


def register() -> None:
    hooks.register(
        AGENT_TURN_AFTER,
        on_agent_turn_after,
        priority=20,
        fail="open",
        name="task_advance_nudge",
    )
