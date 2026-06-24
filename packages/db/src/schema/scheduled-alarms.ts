import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const scheduledAlarms = sqliteTable("scheduled_alarms", {
  id: text("id").primaryKey(),
  projectId: text("project_id").notNull(),
  fromAgentId: text("from_agent_id").notNull(),
  toAgentId: text("to_agent_id").notNull(),
  purpose: text("purpose").notNull(),
  /** Absolute project time (seconds since day 0) when the alarm fires. */
  fireAtGameSeconds: integer("fire_at_game_seconds", { mode: "number" }).notNull(),
  status: text("status").notNull().default("pending"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  firedAt: integer("fired_at", { mode: "number" }),
});

export type ScheduledAlarm = typeof scheduledAlarms.$inferSelect;
export type NewScheduledAlarm = typeof scheduledAlarms.$inferInsert;
