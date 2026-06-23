import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

/**
 * Persistent conversation history per agent.
 *
 * Each row stores one "turn" — a complete exchange of user message +
 * assistant response + any tool calls/results. The raw messages are
 * serialized as JSON (StoredMessage[] format).
 *
 * On server restart, the ConversationStore lazily loads turns from this
 * table, trims to the token budget, and caches in memory.
 */
export const conversationTurns = sqliteTable("conversation_turns", {
  id: text("id").primaryKey(),
  agentId: text("agent_id").notNull(),
  turnIndex: integer("turn_index").notNull(),
  /** JSON-serialized StoredMessage[] for this turn. */
  rawMessages: text("raw_messages").notNull(),
  /** Approximate token count for this turn's messages. */
  approxTokens: integer("approx_tokens").notNull().default(0),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type ConversationTurn = typeof conversationTurns.$inferSelect;
export type NewConversationTurn = typeof conversationTurns.$inferInsert;
