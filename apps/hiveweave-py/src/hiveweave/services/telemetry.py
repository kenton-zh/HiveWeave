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

ALL_EVENTS = frozenset({
    LLM_STREAM_START, LLM_STREAM_CHUNK, LLM_STREAM_DONE, LLM_STREAM_FAIL,
    AGENT_CHAT_START, AGENT_CHAT_DONE, AGENT_CRASH,
    CIRCUIT_OPEN, CIRCUIT_CLOSE,
})


class Telemetry:
    """In-process telemetry event dispatcher.

    Replaces Elixir's :telemetry.execute + attach_many pattern.
    Events are dispatched synchronously to handlers; crash events
    additionally trigger async EventAudit logging.
    """

    def __init__(self) -> None:
        self._handlers: list[Callable[[str, dict], None]] = []

    def add_handler(self, handler: Callable[[str, dict], None]) -> None:
        """Register a custom event handler."""
        self._handlers.append(handler)

    def emit(self, event_name: str, payload: dict | None = None) -> None:
        """Dispatch a telemetry event to all handlers + structlog."""
        data = payload or {}

        # Default dispatch: structured logging via structlog
        if event_name.startswith("llm"):
            logger.debug("telemetry.llm", event=event_name, **data)
        elif event_name == AGENT_CRASH:
            logger.warning("telemetry.agent_crash", **data)
        elif event_name.startswith("agent"):
            logger.debug("telemetry.agent", event=event_name, **data)
        elif event_name.startswith("circuit"):
            logger.info("telemetry.circuit", event=event_name, **data)
        else:
            logger.info("telemetry.event", event=event_name, **data)

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


telemetry = Telemetry()
