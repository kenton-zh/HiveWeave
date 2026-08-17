"""LLM streamer package — public API + patch-compatible re-exports.

Behavior-preserving mechanical split of the former monolithic
``streamer.py``. External callers keep::

    from hiveweave.llm.streamer import Streamer, parse_sse, ...
    patch("hiveweave.llm.streamer._build_obligations_snapshot", ...)

Patch propagation: ``unittest.mock.patch`` on this package module also
updates consumer submodule globals for symbols that tests patch.
"""
from __future__ import annotations

import sys
from types import ModuleType

from .constants import (
    ACTIVITY_EXTEND_S,
    CONTEXT_TRIM_TRIGGER_RATIO,
    CONTINUE_SENTINEL,
    DEFAULT_PLACEHOLDER,
    EMPTY_RESPONSE_BACKOFF_MS,
    EMPTY_RESPONSE_MAX_RETRIES,
    FIRST_CHUNK_TIMEOUT_S,
    HARD_TOTAL_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    LLM_QUEUE_PING_S,
    STREAM_SOCKET_READ_TIMEOUT_S,
    MAX_TOOL_ROUNDS,
    MAX_TOOLS_PER_ROUND,
    MID_ROUND_REMINDER_RATIO,
    NO_TEXT_HINT_MAX,
    NO_TEXT_ROUNDS_THRESHOLD,
    OUTPUT_TOKEN_GLOBAL_CAP,
    SAFETY_BUFFER_TOKENS,
    WORKING_SET_CHECKPOINT_MARKER,
    WORKING_SET_PRESSURE_RATIO,
    WORKING_SET_RETAIN_RATIO,
    TOOL_EXECUTION_TIMEOUT_S,
    TOOL_LOOP_READONLY_STALL_LIMIT,
    TOOL_LOOP_STALL_LIMIT,
    TOTAL_TIMEOUT_S,
    session_wall_clock_enabled,
    stream_chunk_wait_s,
    _LLM_MAX_CONCURRENT,
    _LLM_SEMAPHORE,
    _QUESTION_TOOL_TIMEOUT_S,
    _get_llm_semaphore,
)
from .core import Streamer
from .doom_loop import (
    DOOM_LOOP_DEFAULT_LIMIT,
    DOOM_LOOP_READONLY_FUSE,
    DOOM_LOOP_READONLY_TOOLS,
    DOOM_LOOP_TOOL_LIMITS,
    doom_loop_limit,
    round_made_progress,
    round_was_readonly_only,
)
from .errors import CircuitBreakerOpenError
from .poll import (
    _POLL_CACHE_TOOLS,
    _POLL_CACHE_TTL_S,
    _POLL_HARD_REJECT_LIMIT,
    _POLL_HARD_REJECT_TOOLS,
    _WAITING_GATE_TOOLS,
    _build_obligations_snapshot,
    _poll_cache_get,
    _poll_cache_put,
    _poll_result_cache,
    _poll_waiting_gate_block_async,
)
from .sse import (
    _extract_data,
    _extract_reasoning,
    _extract_text_content,
    merge_tool_calls,
    parse_sse,
    sse_to_chunks,
)
from .types import DeltaCallback, ToolCallCallback

_PATCH_CONSUMERS = (
    "hiveweave.llm.streamer.poll",
    "hiveweave.llm.streamer.tool_exec",
    "hiveweave.llm.streamer.tool_loop",
    "hiveweave.llm.streamer.http_stream",
    "hiveweave.llm.streamer.context",
    "hiveweave.llm.streamer.core",
    "hiveweave.llm.streamer.doom_loop",
    "hiveweave.llm.streamer.sse",
    "hiveweave.llm.streamer.constants",
)

_PATCH_NAMES = frozenset({
    "_build_obligations_snapshot",
    "_poll_cache_get",
    "_poll_cache_put",
    "_poll_waiting_gate_block_async",
    "_poll_result_cache",
    "_get_llm_semaphore",
    "parse_sse",
    "sse_to_chunks",
    "merge_tool_calls",
    "doom_loop_limit",
    "round_made_progress",
    "round_was_readonly_only",
    "Streamer",
    "CircuitBreakerOpenError",
})


class _StreamerPackage(ModuleType):
    """Propagate setattr on patched symbols into consumer submodule globals."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _PATCH_NAMES:
            return
        for modname in _PATCH_CONSUMERS:
            mod = sys.modules.get(modname)
            if mod is not None and name in mod.__dict__:
                object.__setattr__(mod, name, value)


_mod = sys.modules[__name__]
_mod.__class__ = _StreamerPackage

__all__ = [
    "Streamer",
    "MAX_TOOL_ROUNDS",
    "parse_sse",
    "sse_to_chunks",
    "merge_tool_calls",
    "CircuitBreakerOpenError",
    "DeltaCallback",
    "ToolCallCallback",
    "DOOM_LOOP_DEFAULT_LIMIT",
    "DOOM_LOOP_READONLY_TOOLS",
    "DOOM_LOOP_READONLY_FUSE",
    "DOOM_LOOP_TOOL_LIMITS",
    "doom_loop_limit",
    "round_made_progress",
    "round_was_readonly_only",
    "TOOL_LOOP_STALL_LIMIT",
    "TOOL_LOOP_READONLY_STALL_LIMIT",
    "TOOL_EXECUTION_TIMEOUT_S",
    "_QUESTION_TOOL_TIMEOUT_S",
    "_build_obligations_snapshot",
    "_poll_cache_get",
    "_poll_cache_put",
    "_poll_waiting_gate_block_async",
    "_poll_result_cache",
    "_extract_data",
    "_extract_reasoning",
    "_extract_text_content",
]
