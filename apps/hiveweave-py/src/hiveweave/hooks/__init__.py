"""Public API for lifecycle hooks."""

from hiveweave.hooks.points import (
    AGENT_TURN_AFTER,
    AGENT_TURN_BEFORE,
    CATALOG_VERSION,
    CONVERSATION_COMPACT_BEFORE,
    INBOX_TRIAGE_ENRICH,
    TOOL_EXECUTE_AFTER,
    TOOL_EXECUTE_BEFORE,
    TRIGGER_CONTEXT_BUILD,
)
from hiveweave.hooks.registry import HookClosedError, HookRegistry, hooks

__all__ = [
    "AGENT_TURN_AFTER",
    "AGENT_TURN_BEFORE",
    "CATALOG_VERSION",
    "CONVERSATION_COMPACT_BEFORE",
    "INBOX_TRIAGE_ENRICH",
    "TOOL_EXECUTE_AFTER",
    "TOOL_EXECUTE_BEFORE",
    "TRIGGER_CONTEXT_BUILD",
    "HookClosedError",
    "HookRegistry",
    "hooks",
]
