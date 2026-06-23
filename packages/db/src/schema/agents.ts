import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { relations } from "drizzle-orm";
import { modules } from "./modules";
import { projects } from "./projects";

export const agents = sqliteTable("agents", {
  id: text("id").primaryKey(),
  shortId: text("short_id").unique(), // Human-friendly ID like "A001", "A002"
  projectId: text("project_id").references(() => projects.id),
  name: text("name").notNull(),
  role: text("role").notNull(),
  parentId: text("parent_id"),
  moduleId: text("module_id").references(() => modules.id),
  status: text("status", {
    enum: ["created", "active", "promoted", "receiving", "merging", "dissolving", "archived"],
  })
    .notNull()
    .default("created"),
  goal: text("goal").notNull().default(""),
  backstory: text("backstory").notNull().default(""),
  skills: text("skills").notNull().default("[]"),
  permissionType: text("permission_type", {
    enum: ["coordinator", "executor"],
  })
    .notNull()
    .default("executor"),
  // --- Permission system (Claude Code-inspired three-tier: Allow/Ask/Deny) ---
  // Preset mode: "readonly" | "readwrite" | "full" | "custom"
  // "custom" means the user configured allowedTools/deniedTools/askTools manually
  permissionMode: text("permission_mode").notNull().default("full"),
  // JSON arrays of tool rule strings, e.g. ["Read", "Bash(npm *)", "mcp__github__*"]
  allowedTools: text("allowed_tools").notNull().default("[]"),
  deniedTools: text("denied_tools").notNull().default("[]"),
  askTools: text("ask_tools").notNull().default("[]"),
  // JSON array of MCP server IDs this agent can use
  mcpServers: text("mcp_servers").notNull().default("[]"),
  // JSON array of skill IDs bound to this agent
  boundSkills: text("bound_skills").notNull().default("[]"),
  // --- Model configuration ---
  // References llm_models.id in meta DB (cross-DB, no FK constraint)
  modelId: text("model_id"),
  // Per-agent reasoning effort override: "off"|"low"|"medium"|"high"|"max"
  reasoningEffort: text("reasoning_effort"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
  // Cursor for subordinate log injection: only inject logs newer than this timestamp
  lastSeenLogAt: integer("last_seen_log_at", { mode: "number" }),
});

export const agentsRelations = relations(agents, ({ one, many }) => ({
  project: one(projects, {
    fields: [agents.projectId],
    references: [projects.id],
  }),
  parent: one(agents, {
    fields: [agents.parentId],
    references: [agents.id],
    relationName: "parentChild",
  }),
  children: many(agents, { relationName: "parentChild" }),
  module: one(modules, {
    fields: [agents.moduleId],
    references: [modules.id],
  }),
}));

export type Agent = typeof agents.$inferSelect;
export type NewAgent = typeof agents.$inferInsert;
