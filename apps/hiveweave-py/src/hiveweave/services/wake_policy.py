"""Wake policy — classify inbox events; progress must not start LLM turns.

P0: orthogonal to execution. Categories drive whether trigger/watcher may wake.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

WakeCategory = Literal["command", "ask", "approval", "task_transition", "progress"]

# Progress / ACK / heartbeat — deliver but do not wake LLM
_PROGRESS_PATTERNS = (
    re.compile(r"全部完成"),
    re.compile(r"\d+/\d+\s*测试通过"),
    re.compile(r"三人交付全部"),
    re.compile(r"^\[TASK WATCHDOG\]"),
    re.compile(r"^\[WATCHDOG\]"),
    # NOTE: [POST-MERGE VERIFY] is a real work order — not progress
    re.compile(r"done_slice", re.I),
    re.compile(r"已汇报"),
    re.compile(r"无需处理"),
)

_TASK_TRANSITION_PATTERNS = (
    re.compile(r"^\[TASK SUBMITTED\]"),
    re.compile(r"^\[TASK\s", re.I),
    re.compile(r"^\[POST-MERGE VERIFY\]"),
)

_USER_IDS = frozenset({"user", "用户", "human", "operator"})


def is_user_sender(from_agent_id: str | None) -> bool:
    if not from_agent_id:
        return False
    return from_agent_id.lower() in _USER_IDS or from_agent_id == "用户"


def classify_message(
    *,
    message: str,
    message_type: str = "normal",
    expect_report: bool = False,
    from_agent_id: str | None = None,
    priority: str = "normal",
    task_id: str | None = None,
) -> WakeCategory:
    """Return wake category for an inbox payload."""
    mt = (message_type or "normal").lower()
    text = message or ""

    if is_user_sender(from_agent_id) or mt in ("user", "human"):
        return "command"
    if expect_report or mt == "ask":
        return "ask"
    if mt in ("approval", "review", "task") and task_id:
        # Explicit task channel — treat as transition unless progress-shaped
        if any(p.search(text) for p in _PROGRESS_PATTERNS):
            return "progress"
        return "task_transition"
    if any(p.search(text) for p in _TASK_TRANSITION_PATTERNS):
        return "task_transition"
    if any(p.search(text) for p in _PROGRESS_PATTERNS):
        return "progress"
    if mt in ("system",) and any(p.search(text) for p in _PROGRESS_PATTERNS):
        return "progress"
    if priority == "urgent" and mt == "system":
        # system urgent without progress shape → still command-like (alarms)
        if any(p.search(text) for p in _PROGRESS_PATTERNS):
            return "progress"
        return "command"
    return "command"


def should_wake(
    category: WakeCategory,
    *,
    disposition: str | None = None,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
    active_waits: list[dict] | None = None,
) -> bool:
    """Whether delivering this message may start an LLM turn.

    When ``active_waits`` is provided (P1 Wait Contract), event must match
    a contract's wake_on. Otherwise fall back to disposition whitelist.
    """
    if category == "progress":
        return False

    if active_waits is not None:
        from hiveweave.services.wait_contract import (
            category_to_wake_event,
            event_matches_waits,
        )

        # message text not threaded here; WAIT_TIMEOUT sent after clear → empty waits
        event = category_to_wake_event(category, from_agent_id=from_agent_id)
        matched = event_matches_waits(
            active_waits,
            event=event,
            from_agent_id=from_agent_id,
            from_agent_name=from_agent_name,
            from_short_id=from_short_id,
        )
        if matched:
            # Wait Contract match is authoritative — including when disposition
            # is waiting_human. Otherwise peer replies that satisfy agent-waits
            # (模块方案/审批请求) land as wake=0 and the org deadlocks.
            return True
        # No match: timer/external/task waits must not swallow superior commands
        kinds = {(w.get("kind") or "") for w in (active_waits or [])}
        pierce_ok = bool(kinds) and kinds <= {"timer", "external", "task"}
        if not (
            pierce_ok
            and category in ("command", "ask", "approval", "task_transition")
        ):
            return False
        # pierced — fall through to waiting_human check

    if disposition == "waiting_human":
        # Only user replies or new task transitions wake a waiting_human agent
        if is_user_sender(from_agent_id):
            return True
        return category == "task_transition"
    return True


def make_idempotency_key(
    *,
    from_agent_id: str,
    to_agent_id: str,
    category: WakeCategory,
    message: str,
    task_id: str | None = None,
) -> str:
    """Stable key for upsert / dedupe."""
    if category == "progress":
        # Coarse: same sender + category (+ task) collapses duplicates
        raw = f"progress|{from_agent_id}|{to_agent_id}|{task_id or ''}"
    elif category == "task_transition" and task_id:
        # One wake per task status notification family
        preview = (message or "")[:40]
        raw = f"task|{from_agent_id}|{to_agent_id}|{task_id}|{preview}"
    else:
        digest = hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:16]
        raw = f"{category}|{from_agent_id}|{to_agent_id}|{task_id or ''}|{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
