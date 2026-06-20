import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const memories = sqliteTable("memories", {
  id: text("id").primaryKey(),
  agentId: text("agent_id"),
  scope: text("scope", {
    enum: ["project", "agent", "archive"],
  }).notNull(),
  moduleId: text("module_id"),
  type: text("type").notNull(),
  content: text("content").notNull(),
  sourceAgentId: text("source_agent_id"),
  metadata: text("metadata").notNull().default("{}"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export type Memory = typeof memories.$inferSelect;
export type NewMemory = typeof memories.$inferInsert;
