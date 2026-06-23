import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const agentTemplates = sqliteTable("agent_templates", {
  id: text("id").primaryKey(),
  /** Template source: "agency-agents" | "hiveweave" */
  source: text("source").notNull().default("agency-agents"),
  /** Division/category: "engineering", "design", "marketing", etc. */
  division: text("division").notNull().default(""),
  /** Display name: "Frontend Developer" */
  name: text("name").notNull(),
  /** HiveWeave role mapping: "developer", "qa", "manager", etc. */
  role: text("role").notNull().default("specialist"),
  /** Theme color from YAML frontmatter */
  color: text("color").notNull().default(""),
  /** Emoji from YAML frontmatter */
  emoji: text("emoji").notNull().default(""),
  /** Short tagline from YAML frontmatter "vibe" field */
  vibe: text("vibe").notNull().default(""),
  /** Description from YAML frontmatter */
  description: text("description").notNull().default(""),
  /** Full markdown body (the agent persona definition) */
  promptBody: text("prompt_body").notNull().default(""),
  /** Original filename for provenance tracking */
  originalFile: text("original_file").notNull().default(""),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
});

export type AgentTemplate = typeof agentTemplates.$inferSelect;
export type NewAgentTemplate = typeof agentTemplates.$inferInsert;
