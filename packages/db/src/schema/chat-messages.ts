import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const chatMessages = sqliteTable("chat_messages", {
  id: text("id").primaryKey(),
  agentId: text("agent_id").notNull(),
  role: text("role").notNull(), // "user" | "assistant"
  content: text("content").notNull(),
  toolCalls: text("tool_calls").notNull().default("[]"), // JSON string
  isBackground: integer("is_background", { mode: "boolean" }).notNull().default(false),
  isRead: integer("is_read", { mode: "boolean" }).notNull().default(true),
  isStreaming: integer("is_streaming", { mode: "boolean" }).notNull().default(false),
  teamFromAgentId: text("team_from_agent_id"),
  teamToAgentId: text("team_to_agent_id"),
  images: text("images"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type ChatMessageRow = typeof chatMessages.$inferSelect;
export type NewChatMessageRow = typeof chatMessages.$inferInsert;
