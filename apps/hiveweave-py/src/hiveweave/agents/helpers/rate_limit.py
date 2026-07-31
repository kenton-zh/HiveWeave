"""Rate-limit error detection + project-wide account throttle.

Extracted from agent.py — behavior-preserving mechanical split (P1).
TEST18 P0-5: AccountRateLimitExceeded is an account ceiling, not a
per-agent quota. One hit must slow the whole project, otherwise peers
keep stampeding the same key and every resume immediately re-429s.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# project_id -> monotonic deadline for coordinated slowdown
_project_rate_limit_until: dict[str, float] = {}


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


def is_account_rate_limit(error: BaseException | None) -> bool:
    """True for account-wide ceilings (shared key), not per-model soft 429."""
    if error is None:
        return False
    msg = str(error).lower()
    return (
        "accountratelimit" in msg
        or "account rate limit" in msg
        or "account_rate_limit" in msg
    )


def project_rate_limit_remaining(project_id: str | None) -> float:
    """Seconds left on project-wide account throttle (0 if clear)."""
    if not project_id:
        return 0.0
    until = _project_rate_limit_until.get(project_id, 0.0)
    left = until - time.monotonic()
    if left <= 0:
        _project_rate_limit_until.pop(project_id, None)
        return 0.0
    return left


def arm_project_rate_limit(project_id: str, cooldown_s: float) -> float:
    """Arm/extend project-wide throttle. Returns effective remaining seconds."""
    if not project_id or cooldown_s <= 0:
        return project_rate_limit_remaining(project_id)
    until = time.monotonic() + float(cooldown_s)
    prev = _project_rate_limit_until.get(project_id, 0.0)
    if until > prev:
        _project_rate_limit_until[project_id] = until
    return project_rate_limit_remaining(project_id)


def circuit_open_for_agent(agent: Any) -> bool:
    """True when the agent's provider circuit is open (skip doomed resume)."""
    try:
        from hiveweave.llm.circuit_breaker import CircuitState, circuit_breaker

        provider = None
        model = getattr(agent, "model_config", None) or getattr(
            agent, "_model_config", None
        )
        if isinstance(model, dict):
            provider = model.get("provider") or model.get("provider_name")
        breakers = getattr(circuit_breaker, "_breakers", {}) or {}
        if provider:
            state = breakers.get(str(provider))
            return bool(state and state.state == CircuitState.OPEN)
        # No provider known — any open breaker blocks resume into hot account
        return any(
            getattr(s, "state", None) == CircuitState.OPEN
            for s in breakers.values()
        )
    except Exception:
        return False


def broadcast_project_rate_limit(
    project_id: str,
    cooldown_s: float,
    *,
    source_agent_id: str | None = None,
) -> int:
    """Slow all live agents in the project (TEST18 P0-5).

    Arms the project throttle and each agent's resume cooldown so watchers
    do not immediately re-fire into the same account ceiling.
    Returns number of peer agents cooled.
    """
    remaining = arm_project_rate_limit(project_id, cooldown_s)
    cooled = 0
    try:
        from hiveweave.agents.supervisor import agent_manager

        peers = list(agent_manager.list_all())
        for peer in peers:
            if getattr(peer, "project_id", None) != project_id:
                continue
            if source_agent_id and getattr(peer, "id", None) == source_agent_id:
                continue
            try:
                peer._arm_resume_cooldown(remaining or cooldown_s)
                cooled += 1
            except Exception:
                pass
    except Exception as e:
        log.debug("project_rate_limit_broadcast_failed", error=str(e))
    log.warning(
        "project_account_rate_limit",
        project_id=project_id,
        source_agent_id=source_agent_id,
        cooldown_s=remaining or cooldown_s,
        peers_cooled=cooled,
    )
    return cooled
