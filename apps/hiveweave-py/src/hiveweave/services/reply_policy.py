"""Reply / expect_report — structured flags only (language-agnostic).

Do NOT scan free-text for "please reply" / 「请回复」 etc. Agents of any
language must set expect_report via ask_agent or explicit tool args.
See CLAUDE.md「语言无关：禁止用文案猜意图」。
"""

from __future__ import annotations


def message_requests_reply(message: str | None) -> bool:
    """Deprecated no-op — never infer reply-need from body text.

    Kept for import compatibility. Always False.
    """
    return False


def resolve_expect_report(explicit: bool | None, message: str | None = None) -> bool:
    """Whether the recipient must reply — explicit flag only.

    ``message`` is ignored (language-agnostic). Use ask_agent or
    expect_report=true on send_message.
    """
    return bool(explicit)
