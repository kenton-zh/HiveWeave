import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const merges = sqliteTable("merges", {
  id: text("id").primaryKey(),
  sourceAgentIds: text("source_agent_ids").notNull().default("[]"),
  targetAgentId: text("target_agent_id").notNull(),
  summary: text("summary").notNull(),
  conflicts: text("conflicts").notNull().default("[]"),
  resolution: text("resolution").notNull().default("{}"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type Merge = typeof merges.$inferSelect;
export type NewMerge = typeof merges.$inferInsert;
