"""Stable hook point names (catalog v1). See docs/spec/lifecycle-hooks.md."""

# Inbox / wake
INBOX_TRIAGE_ENRICH = "inbox.triage.enrich"
TRIGGER_CONTEXT_BUILD = "trigger.context.build"

# Agent turn
AGENT_TURN_BEFORE = "agent.turn.before"
AGENT_TURN_AFTER = "agent.turn.after"

# Tools / LLM
TOOL_EXECUTE_BEFORE = "tool.execute.before"
TOOL_EXECUTE_AFTER = "tool.execute.after"
CONVERSATION_COMPACT_BEFORE = "conversation.compact.before"

CATALOG_VERSION = 1
