"""Reply / expect_report hard rules shared by messaging + watchdog.

Scripts cannot cover every stall, but "asked for a reply then everyone went idle"
is a closed pattern: detect request language, mark expect_report, enforce reply,
and wake the waiter if silence persists.
"""

from __future__ import annotations

import re

# Strict enough to catch tool-verification / "reply with results" asks;
# avoid matching casual mentions of 回复 in FYI notes.
_REPLY_REQUEST_RE = re.compile(
    r"(?:"
    r"请(?:依次)?[^。\n]{0,30}回复"
    r"|回复(?:结果|格式|以下|内容)"
    r"|立即报告"
    r"|有任何失败立即报告"
    r"|等待(?:你的)?回复"
    r"|需要你(?:的)?回复"
    r"|reply[_\s-]?required"
    r"|please\s+reply"
    r"|reply\s+with"
    r"|report\s+back"
    r"|respond\s+with"
    r")",
    re.IGNORECASE,
)


def message_requests_reply(message: str | None) -> bool:
    """True if message body clearly asks the recipient to reply."""
    if not message or not str(message).strip():
        return False
    return bool(_REPLY_REQUEST_RE.search(str(message)))


def resolve_expect_report(explicit: bool | None, message: str | None) -> bool:
    """Merge explicit expect_report flag with reply-request heuristics.

    - explicit True → always True
    - explicit False/None + reply-request language → True (auto-upgrade)
    - otherwise → False
    """
    if explicit:
        return True
    return message_requests_reply(message)
