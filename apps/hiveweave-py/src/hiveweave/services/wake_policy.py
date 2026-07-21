"""Wake policy — always wake; no message-category taxonomy.

Product rule: **any** inbox message may start an LLM turn.

Reply-need is **not** inferred here — use structured ``expect_report`` /
``message_type=ask`` (see ``reply_policy`` / turn exit). Do not invent
priority classes; a future per-agent assistant model will triage.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

# Kept for DB column / call-site compatibility. Always "message".
WakeCategory = Literal["message"]

# Well-known human sender ids (system aliases — not NL parsing)
_USER_IDS = frozenset({"user", "human", "operator"})


def is_user_sender(from_agent_id: str | None) -> bool:
    if not from_agent_id:
        return False
    return from_agent_id.lower() in _USER_IDS


def classify_message(
    *,
    message: str = "",
    message_type: str = "normal",
    expect_report: bool = False,
    from_agent_id: str | None = None,
    priority: str = "normal",
    task_id: str | None = None,
) -> WakeCategory:
    """No-op classifier — categories removed (always ``message``)."""
    return "message"


@dataclass(frozen=True)
class AdmitResult:
    """Result of ``admit_wake`` — single source for inbox + chat gates."""

    ok: bool
    reason: str = ""


def admit_wake(
    category: WakeCategory | str | None = None,
    *,
    disposition: str | None = None,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
    active_waits: list[dict] | None = None,
    recipient_parent_id: str | None = None,
    wake_category: str | None = None,
    from_user: bool = False,
) -> AdmitResult:
    """Always admit — any inbox message may wake (product rule)."""
    return AdmitResult(True, "always_wake")


def should_wake(
    category: WakeCategory | str | None = None,
    *,
    disposition: str | None = None,
    from_agent_id: str | None = None,
    from_agent_name: str | None = None,
    from_short_id: str | None = None,
    active_waits: list[dict] | None = None,
    recipient_parent_id: str | None = None,
) -> bool:
    """Whether delivering this message may start an LLM turn."""
    return admit_wake(
        category,
        disposition=disposition,
        from_agent_id=from_agent_id,
        from_agent_name=from_agent_name,
        from_short_id=from_short_id,
        active_waits=active_waits,
        recipient_parent_id=recipient_parent_id,
    ).ok


def make_idempotency_key(
    *,
    from_agent_id: str,
    to_agent_id: str,
    category: WakeCategory | str | None = None,
    message: str,
    task_id: str | None = None,
) -> str:
    """Stable key for upsert / dedupe (content hash; no category taxonomy)."""
    digest = hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:16]
    raw = f"msg|{from_agent_id}|{to_agent_id}|{task_id or ''}|{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
