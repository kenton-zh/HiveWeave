"""Rate-limit error detection + project-wide account throttle.

Extracted from agent.py — behavior-preserving mechanical split (P1).
TEST18 P0-5: AccountRateLimitExceeded is an account ceiling, not a
per-agent quota. One hit must slow the whole project, otherwise peers
keep stampeding the same key and every resume immediately re-429s.
"""

from __future__ import annotations

import re
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# project_id -> monotonic deadline for coordinated slowdown
_project_rate_limit_until: dict[str, float] = {}

# 全局余额耗尽熔断（HTTP 402）。账号级：与 429 不同，402 重试必败
# （TEST19 教训：10 个 error run 全部 402，每个 agent 各自撞满 4 次
# give up，watchdog 还把 escalation 投给同样已死的收件人）。
# monotonic deadline，0 = 未触发。
_balance_exhausted_until: float = 0.0

# 402 熔断持续时间：余额耗尽需要人工充值，给足时间（默认 1 小时）。
BALANCE_EXHAUSTED_COOLDOWN_S = 3_600.0

# ── E7 项目级容量暂停（配额风暴组织级降速）────────────────────
# 与短时 account 上限（_project_rate_limit_until）不同：容量错误
# （daily_quota / GoUsageLimitError）的恢复钥匙是「配额窗口重置」，
# 暂停到确定的 reset_at 时刻，恢复后由既有 watcher/cooldown 批量唤醒。
# 无确定重置时刻时的兜底暂停时长（对齐 402 熔断粒度，1 小时，唤醒后
# 若仍失败会重新暂停）。
CAPACITY_PROJECT_COOLDOWN_S = 3_600.0

# project_id -> reset_at epoch（配额窗口重置时刻）
_project_capacity_until: dict[str, float] = {}


def is_balance_error(error: BaseException | None) -> bool:
    """True when the failure is account balance exhaustion (HTTP 402).

    Distinct from rate limits: 402 will NOT recover by retrying — only
    a human topping up the account fixes it. Triggers the global wake-stop.

    Prefer structured ``PermanentError(status=402)``. Message fallback uses
    phrase needles + word-bounded ``402`` compounds — bare ``"402"`` alone
    must not arm a 1h global kill (ports / codes like 1402 / HTML crumbs).
    """
    if error is None:
        return False
    try:
        from hiveweave.llm.retry import PermanentError

        if isinstance(error, PermanentError) and getattr(error, "status", None) == 402:
            return True
    except Exception:
        pass
    msg = str(error).lower()
    phrases = (
        "insufficient balance",
        "insufficient_balance",
        "balance exhausted",
        "payment required",
    )
    if any(p in msg for p in phrases):
        return True
    # Word-boundary compounds: "http 402" / "error 402" — not "error 4021"
    return bool(
        re.search(r"(?:http|status|error|code)\s*402\b", msg)
    )


def balance_exhausted_remaining() -> float:
    """Seconds left on the global 402 wake-stop (0 if clear)."""
    global _balance_exhausted_until
    left = _balance_exhausted_until - time.monotonic()
    if left <= 0:
        _balance_exhausted_until = 0.0
        return 0.0
    return left


def clear_balance_exhausted() -> int:
    """Clear the global 402 wake-stop and peer resume cooldowns (after top-up).

    ``broadcast_balance_exhausted`` arms both the process flag and each peer's
    ``_resume_cooldown_until``; clearing only the flag would leave peers parked.
    Returns number of peers whose resume cooldown was reset.
    """
    global _balance_exhausted_until
    _balance_exhausted_until = 0.0
    cleared = 0
    try:
        from hiveweave.agents.supervisor import agent_manager

        for peer in list(agent_manager.list_all()):
            try:
                until = float(getattr(peer, "_resume_cooldown_until", 0.0) or 0.0)
                if until > 0:
                    peer._resume_cooldown_until = 0.0
                    cleared += 1
            except Exception:
                pass
    except Exception as e:
        log.debug("balance_exhausted_clear_peers_failed", error=str(e))
    return cleared


def arm_balance_exhausted(
    duration_s: float = BALANCE_EXHAUSTED_COOLDOWN_S,
) -> float:
    """Arm the global 402 wake-stop. Returns effective remaining seconds."""
    global _balance_exhausted_until
    until = time.monotonic() + max(duration_s, 0.0)
    if until > _balance_exhausted_until:
        _balance_exhausted_until = until
    return balance_exhausted_remaining()


def broadcast_balance_exhausted(
    duration_s: float = BALANCE_EXHAUSTED_COOLDOWN_S,
    *,
    source_agent_id: str | None = None,
) -> int:
    """Stop ALL live agents in the process from waking (global 402 breaker).

    TEST19 教训: 402 是账号级余额问题，不是某个 agent 的配置错误。
    一个 agent 撞到 402 后，其他 agent 的唤醒请求同样必败 —— 立即停掉
    所有唤醒，避免连环 give up + escalation 投给已死收件人。
    Returns number of agents whose resume was suppressed.
    """
    remaining = arm_balance_exhausted(duration_s)
    cooled = 0
    try:
        from hiveweave.agents.supervisor import agent_manager

        for peer in list(agent_manager.list_all()):
            if source_agent_id and getattr(peer, "id", None) == source_agent_id:
                continue
            try:
                peer._arm_resume_cooldown(max(remaining, 60.0))
                cooled += 1
            except Exception:
                pass
    except Exception as e:
        log.debug("balance_exhausted_broadcast_failed", error=str(e))
    log.error(
        "account_balance_exhausted",
        source_agent_id=source_agent_id,
        cooldown_s=remaining,
        peers_cooled=cooled,
    )
    return cooled


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


# ── E7: 项目级容量暂停（配额风暴）────────────────────────────


def project_capacity_remaining(project_id: str | None) -> float:
    """距项目容量暂停解除的秒数（0 = 未暂停/已恢复）。"""
    if not project_id:
        return 0.0
    until = _project_capacity_until.get(project_id, 0.0)
    left = until - time.time()
    if left <= 0:
        _project_capacity_until.pop(project_id, None)
        return 0.0
    return left


def arm_project_capacity_pause(
    project_id: str, reset_at_epoch: float | None
) -> float:
    """把项目的容量暂停设立到配额窗口重置时刻。"""
    if not project_id:
        return 0.0
    if not reset_at_epoch or float(reset_at_epoch) <= time.time():
        return project_capacity_remaining(project_id)
    until = float(reset_at_epoch)
    prev = _project_capacity_until.get(project_id, 0.0)
    if until > prev:
        _project_capacity_until[project_id] = until
    return project_capacity_remaining(project_id)


def broadcast_project_capacity_pause(
    project_id: str,
    reset_at_epoch: float | None,
    *,
    source_agent_id: str | None = None,
) -> int:
    """配额风暴组织级降速：冷却本项目全部活体 agent 到配额窗口重置。

    容量错误是整个组织共享的墙（同 key 共享配额）——一个 agent 撞到
    daily_quota / GoUsageLimitError，其余 agent 的唤醒请求同样必败。
    参照 402 熔断模式，但粒度是项目级。恢复 = cooldown 到期 + 既有
    watcher/补偿机制批量唤醒（无需跳闸逻辑，自动生效）。
    Returns number of peer agents cooled.
    """
    if not reset_at_epoch:
        reset_at_epoch = time.time() + CAPACITY_PROJECT_COOLDOWN_S
    remaining = arm_project_capacity_pause(project_id, reset_at_epoch)
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
                peer._arm_resume_cooldown(max(remaining, 60.0))
                cooled += 1
            except Exception:
                pass
    except Exception as e:
        log.debug("project_capacity_broadcast_failed", error=str(e))
    log.warning(
        "project_capacity_paused",
        project_id=project_id,
        reset_in_s=round(remaining, 1),
        peers_cooled=cooled,
    )
    return cooled


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
