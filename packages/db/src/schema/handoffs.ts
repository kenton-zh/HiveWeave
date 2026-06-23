import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const handoffs = sqliteTable("handoffs", {
  id: text("id").primaryKey(),
  fromAgentId: text("from_agent_id").notNull(),
  toAgentId: text("to_agent_id"),
  moduleId: text("module_id"),
  summary: text("summary").notNull(),
  memorySnapshotId: text("memory_snapshot_id"),
  status: text("status", {
    enum: ["pending", "accepted", "rejected", "completed", "approved"],
  })
    .notNull()
    .default("pending"),
  expectReport: integer("expect_report", { mode: "boolean" }).notNull().default(false),
  reportedUp: integer("reported_up", { mode: "boolean" }).notNull().default(false),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }),
});

export type Handoff = typeof handoffs.$inferSelect;
export type NewHandoff = typeof handoffs.$inferInsert;
