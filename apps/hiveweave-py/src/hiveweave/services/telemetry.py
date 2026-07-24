"""Telemetry — in-process event dispatch system for observability.

契约 16: 可观测性
- 9 event types: LLM streaming (4), Agent lifecycle (3), Circuit breaker (2)
- dispatch_handler logs via structlog (replaces Elixir :telemetry.execute)
- agent.crash auto-links to EventAudit.log(:crash) with reason
- Custom handlers can be registered via add_handler()
"""

import asyncio
import time
from typing import Any, Callable

import structlog

from hiveweave.services.event_audit import event_audit

logger = structlog.get_logger()

# Event name constants
LLM_STREAM_START = "llm.stream_start"
LLM_STREAM_CHUNK = "llm.stream_chunk"
LLM_STREAM_DONE = "llm.stream_done"
LLM_STREAM_FAIL = "llm.stream_fail"
AGENT_CHAT_START = "agent.chat_start"
AGENT_CHAT_DONE = "agent.chat_done"
AGENT_CRASH = "agent.crash"
CIRCUIT_OPEN = "circuit.open"
CIRCUIT_CLOSE = "circuit.close"
AGENT_WAKE = "agent.wake"
AGENT_NO_PROGRESS = "agent.no_progress_fault"
INBOX_DEDUPED = "inbox.deduped"
VERIFY_STALE_NUDGE = "verify.stale_nudge"
AGENT_TURN_EXIT = "agent.turn_exit"

ALL_EVENTS = frozenset({
    LLM_STREAM_START, LLM_STREAM_CHUNK, LLM_STREAM_DONE, LLM_STREAM_FAIL,
    AGENT_CHAT_START, AGENT_CHAT_DONE, AGENT_CRASH,
    CIRCUIT_OPEN, CIRCUIT_CLOSE,
    AGENT_WAKE, AGENT_NO_PROGRESS, INBOX_DEDUPED,
    VERIFY_STALE_NUDGE, AGENT_TURN_EXIT,
})


class Telemetry:
    """In-process telemetry event dispatcher.

    Replaces Elixir's :telemetry.execute + attach_many pattern.
    Events are dispatched synchronously to handlers; crash events
    additionally trigger async EventAudit logging.
    """

    def __init__(self) -> None:
        self._handlers: list[Callable[[str, dict], None]] = []
        # P2: in-process counters for GET /api/debug/metrics
        self._counters: dict[str, int] = {
            "wake_total": 0,
            "wake_by_reason": 0,  # placeholder; use _wake_reasons
            "no_progress_faults": 0,
            "inbox_deduped": 0,
            "verify_stale_nudge": 0,
            "stream_total_timeout": 0,
            "doom_loop_detected": 0,
            "doom_loop_warned": 0,
            "poll_hard_reject": 0,
        }
        self._wake_reasons: dict[str, int] = {}
        self._turn_exit_violations: dict[str, int] = {}
        self._turn_exit_actions: dict[str, int] = {}
        self._gate_soft_pass: dict[str, int] = {}
        self._gate_hard_reject: dict[str, int] = {}

    def add_handler(self, handler: Callable[[str, dict], None]) -> None:
        """Register a custom event handler."""
        self._handlers.append(handler)

    def snapshot_counters(self) -> dict[str, Any]:
        """Return a copy of observability counters (P2)."""
        return {
            "wake_total": self._counters.get("wake_total", 0),
            "wake_by_reason": dict(self._wake_reasons),
            "no_progress_faults": self._counters.get("no_progress_faults", 0),
            "inbox_deduped": self._counters.get("inbox_deduped", 0),
            "verify_stale_nudge": self._counters.get("verify_stale_nudge", 0),
            "stream_total_timeout": self._counters.get("stream_total_timeout", 0),
            "doom_loop_detected": self._counters.get("doom_loop_detected", 0),
            "doom_loop_warned": self._counters.get("doom_loop_warned", 0),
            "poll_hard_reject": self._counters.get("poll_hard_reject", 0),
            "turn_exit_by_violation": dict(self._turn_exit_violations),
            "turn_exit_by_action": dict(self._turn_exit_actions),
            "gate_soft_pass_total": dict(self._gate_soft_pass),
            "gate_hard_reject_total": dict(self._gate_hard_reject),
        }

    def reset_counters_for_tests(self) -> None:
        self._counters = {
            "wake_total": 0,
            "wake_by_reason": 0,
            "no_progress_faults": 0,
            "inbox_deduped": 0,
            "verify_stale_nudge": 0,
            "stream_total_timeout": 0,
            "doom_loop_detected": 0,
            "doom_loop_warned": 0,
            "poll_hard_reject": 0,
        }
        self._wake_reasons.clear()
        self._turn_exit_violations.clear()
        self._turn_exit_actions.clear()
        self._gate_soft_pass.clear()
        self._gate_hard_reject.clear()

    def gate_soft_pass(self, code: str) -> None:
        """TEST14: observe commit_turn soft-pass by gate code."""
        key = str(code or "unknown")
        self._gate_soft_pass[key] = self._gate_soft_pass.get(key, 0) + 1

    def gate_hard_reject(self, code: str) -> None:
        """TEST14: observe commit_turn hard-reject by gate code."""
        key = str(code or "unknown")
        self._gate_hard_reject[key] = self._gate_hard_reject.get(key, 0) + 1

    def emit(self, event_name: str, payload: dict | None = None) -> None:
        """Dispatch a telemetry event to all handlers + structlog."""
        data = payload or {}

        # P2 counters
        if event_name == AGENT_WAKE:
            self._counters["wake_total"] = self._counters.get("wake_total", 0) + 1
            reason = str(data.get("reason") or "unknown")
            self._wake_reasons[reason] = self._wake_reasons.get(reason, 0) + 1
        elif event_name == AGENT_NO_PROGRESS:
            self._counters["no_progress_faults"] = (
                self._counters.get("no_progress_faults", 0) + 1
            )
        elif event_name == INBOX_DEDUPED:
            self._counters["inbox_deduped"] = (
                self._counters.get("inbox_deduped", 0) + 1
            )
        elif event_name == VERIFY_STALE_NUDGE:
            self._counters["verify_stale_nudge"] = (
                self._counters.get("verify_stale_nudge", 0) + 1
            )
        elif event_name == AGENT_TURN_EXIT:
            action = str(data.get("action") or "unknown")
            self._turn_exit_actions[action] = (
                self._turn_exit_actions.get(action, 0) + 1
            )
            for v in data.get("violations") or []:
                key = str(v)
                self._turn_exit_violations[key] = (
                    self._turn_exit_violations.get(key, 0) + 1
                )

        # Default dispatch: structured logging via structlog
        if event_name.startswith("llm"):
            logger.debug("telemetry.llm", telem_event=event_name, **data)
        elif event_name == AGENT_CRASH:
            logger.warning("telemetry.agent_crash", **data)
        elif event_name.startswith("agent"):
            logger.debug("telemetry.agent", telem_event=event_name, **data)
        elif event_name.startswith("circuit"):
            logger.info("telemetry.circuit", telem_event=event_name, **data)
        elif event_name.startswith("inbox"):
            logger.info("telemetry.inbox", telem_event=event_name, **data)
        else:
            logger.info("telemetry.event", telem_event=event_name, **data)

        # Custom handlers
        for handler in self._handlers:
            try:
                handler(event_name, data)
            except Exception as e:
                logger.warning("telemetry.handler_error", error=str(e))

        # Crash auto-link to EventAudit (契约 16)
        if "crash" in event_name:
            self._schedule_crash_audit(data)

    def _schedule_crash_audit(self, data: dict) -> None:
        """Fire-and-forget: write crash event to EventAudit."""
        agent_id = data.get("agent_id", "unknown")
        project_id = data.get("project_id", "")
        reason = str(data.get("reason", "unknown"))
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    event_audit.log(
                        agent_id, project_id, "crash",
                        {"reason": reason},
                    )
                )
            else:
                logger.warning("telemetry.no_loop_for_crash_audit",
                               agent_id=agent_id)
        except RuntimeError:
            logger.warning("telemetry.no_loop_for_crash_audit",
                           agent_id=agent_id)

    # ── 9 emit functions ──────────────────────────────────────

    def llm_stream_start(self, provider: str, model: str) -> None:
        self.emit(LLM_STREAM_START, {
            "provider": provider, "model": model,
            "system_time": time.time(),
        })

    def llm_stream_chunk(self, provider: str, latency_ms: float) -> None:
        self.emit(LLM_STREAM_CHUNK, {
            "provider": provider, "latency_ms": latency_ms,
        })

    def llm_stream_done(
        self, provider: str, model: str,
        duration_ms: float, status: str,
    ) -> None:
        self.emit(LLM_STREAM_DONE, {
            "provider": provider, "model": model,
            "duration_ms": duration_ms, "status": status,
        })

    def llm_stream_fail(self, provider: str, reason: str) -> None:
        self.emit(LLM_STREAM_FAIL, {
            "provider": provider, "reason": reason,
            "system_time": time.time(),
        })

    def agent_chat_start(self, agent_id: str, from_: str = "") -> None:
        self.emit(AGENT_CHAT_START, {
            "agent_id": agent_id, "from": from_,
            "system_time": time.time(),
        })

    def agent_chat_done(
        self, agent_id: str, duration_ms: float, tokens: int = 0,
    ) -> None:
        self.emit(AGENT_CHAT_DONE, {
            "agent_id": agent_id, "duration_ms": duration_ms,
            "tokens": tokens,
        })

    def agent_crash(
        self, agent_id: str, reason: Any, project_id: str = "",
    ) -> None:
        self.emit(AGENT_CRASH, {
            "agent_id": agent_id, "reason": str(reason),
            "project_id": project_id, "system_time": time.time(),
        })

    def circuit_open(self, provider: str) -> None:
        self.emit(CIRCUIT_OPEN, {
            "provider": provider, "system_time": time.time(),
        })

    def circuit_close(self, provider: str) -> None:
        self.emit(CIRCUIT_CLOSE, {
            "provider": provider, "system_time": time.time(),
        })

    def agent_wake(self, agent_id: str, reason: str, **extra: Any) -> None:
        self.emit(AGENT_WAKE, {
            "agent_id": agent_id, "reason": reason, **extra,
            "system_time": time.time(),
        })

    def agent_no_progress(self, agent_id: str, streak: int = 0) -> None:
        self.emit(AGENT_NO_PROGRESS, {
            "agent_id": agent_id, "streak": streak,
            "system_time": time.time(),
        })

    def inbox_deduped(self, to_agent_id: str, category: str = "") -> None:
        self.emit(INBOX_DEDUPED, {
            "to_agent_id": to_agent_id, "category": category,
            "system_time": time.time(),
        })

    def turn_exit_gate(
        self,
        agent_id: str,
        violations: list[str] | None,
        action: str,
        *,
        gate_round: int = 0,
    ) -> None:
        """Record a turn-exit evaluation (repair|park|exhausted|ok)."""
        viols = list(violations or [])
        self.emit(AGENT_TURN_EXIT, {
            "agent_id": agent_id,
            "violations": viols,
            "action": action,
            "gate_round": gate_round,
            "primary_violation": viols[0] if viols else None,
            "system_time": time.time(),
        })

    def stream_total_timeout(
        self, agent_id: str, *, agent_streak: int | None = None
    ) -> None:
        self._counters["stream_total_timeout"] = (
            self._counters.get("stream_total_timeout", 0) + 1
        )
        # BUG-8: log per-agent streak separately from the process-wide counter
        # (global count alone looked like park should have fired when it was
        # actually two different agents timing out once each).
        logger.warning(
            "telemetry_stream_total_timeout",
            agent_id=agent_id,
            count=self._counters["stream_total_timeout"],
            agent_streak=agent_streak,
        )

    def doom_loop(
        self, agent_id: str, tool: str, *, stage: str = "detected"
    ) -> None:
        key = (
            "doom_loop_detected"
            if stage == "detected"
            else "doom_loop_warned"
        )
        self._counters[key] = self._counters.get(key, 0) + 1
        logger.warning(
            "telemetry_doom_loop",
            agent_id=agent_id,
            tool=tool,
            stage=stage,
            count=self._counters[key],
        )

    def tool_loop_stall(self, agent_id: str, *, stall_count: int = 0) -> None:
        self._counters["tool_loop_stall"] = (
            self._counters.get("tool_loop_stall", 0) + 1
        )
        logger.warning(
            "telemetry_tool_loop_stall",
            agent_id=agent_id,
            stall_count=stall_count,
            count=self._counters["tool_loop_stall"],
        )

    def poll_hard_reject(self, agent_id: str, tool: str) -> None:
        self._counters["poll_hard_reject"] = (
            self._counters.get("poll_hard_reject", 0) + 1
        )
        logger.warning(
            "telemetry_poll_hard_reject",
            agent_id=agent_id,
            tool=tool,
            count=self._counters["poll_hard_reject"],
        )


telemetry = Telemetry()
