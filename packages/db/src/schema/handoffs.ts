import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const handoffs = sqliteTable("handoffs", {
  id: text("id").primaryKey(),
  fromAgentId: text("from_agent_id").notNull(),
  toAgentId: text("to_agent_id"),
  moduleId: text("module_id"),
  summary: text("summary").notNull(),
  memorySnapshotId: text("memory_snapshot_id"),
  status: text("status", {
    enum: ["pending", "accepted", "rejected", "completed"],
  })
    .notNull()
    .default("pending"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type Handoff = typeof handoffs.$inferSelect;
export type NewHandoff = typeof handoffs.$inferInsert;
