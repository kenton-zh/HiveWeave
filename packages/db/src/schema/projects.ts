import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const projects = sqliteTable("projects", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  description: text("description"), // Project description (optional)
  workspacePath: text("workspace_path"), // Absolute path to the project's working directory
  orgParadigm: text("org_paradigm"), // Selected organizational paradigm ID (e.g. "flat_squad", "tech_lead")
  /** JSON ProjectCharter — roles, artifact kinds, staffing policy (CEO-authored) */
  charterJson: text("charter_json"),
  /** JSON EnterpriseGoals — objectives, key results, current focus (CEO/user-authored, visible to all agents) */
  goalsJson: text("goals_json"),
  /** Accumulated project-time seconds (persists across server restarts). */
  gameTimeAccumulatedSeconds: integer("game_time_accumulated_seconds", { mode: "number" }).notNull().default(0),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type Project = typeof projects.$inferSelect;
export type NewProject = typeof projects.$inferInsert;
