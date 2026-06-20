import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const workLogs = sqliteTable("work_logs", {
  id: text("id").primaryKey(),
  agentId: text("agent_id").notNull(),
  sessionId: text("session_id").notNull(),
  type: text("type").notNull(),
  summary: text("summary").notNull(),
  details: text("details").notNull().default("{}"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type WorkLog = typeof workLogs.$inferSelect;
export type NewWorkLog = typeof workLogs.$inferInsert;
