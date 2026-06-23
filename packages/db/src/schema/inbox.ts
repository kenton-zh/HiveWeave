import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const inbox = sqliteTable("inbox", {
  id: text("id").primaryKey(),
  fromAgentId: text("from_agent_id").notNull(),
  toAgentId: text("to_agent_id").notNull(),
  message: text("message").notNull(),
  messageType: text("message_type").notNull().default("superior"),
  expectReport: integer("expect_report", { mode: "boolean" }).notNull().default(false),
  read: integer("read", { mode: "boolean" }).notNull().default(false),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type InboxMessage = typeof inbox.$inferSelect;
export type NewInboxMessage = typeof inbox.$inferInsert;
