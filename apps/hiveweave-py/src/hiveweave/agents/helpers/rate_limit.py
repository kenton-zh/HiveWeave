"""Rate-limit error detection helper.

Extracted from agent.py — behavior-preserving mechanical split (P1).
"""

from __future__ import annotations


def is_rate_limit_error(error: BaseException | None) -> bool:
    """True when the failure is provider rate-limit (429), not a hard fault.

    Rate limits must not increment consecutive-error give-up — they are
    temporary quota pressure, not agent failure.
    """
    if error is None:
        return False
    try:
        from hiveweave.llm.retry import RetryableError

        if isinstance(error, RetryableError) and getattr(error, "status", None) == 429:
            return True
    except Exception:
        pass
    msg = str(error).lower()
    needles = (
        "429",
        "accountratelimit",
        "rate limit",
        "ratelimitexceeded",
        "too many requests",
        "rate_limit",
    )
    return any(n in msg for n in needles)
