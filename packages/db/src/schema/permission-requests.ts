import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { relations } from "drizzle-orm";
import { agents } from "./agents";

/**
 * Permission approval requests — tracks when an agent wants to perform
 * an action that requires user approval (Ask-tier rule triggered).
 *
 * Lifecycle: pending → approved | rejected
 *
 * Inspired by AG-UI protocol's APPROVAL_REQUEST / APPROVAL_RESPONSE pattern.
 */
export const permissionRequests = sqliteTable("permission_requests", {
  id: text("id").primaryKey(),
  agentId: text("agent_id")
    .notNull()
    .references(() => agents.id),
  // The tool the agent wants to call, e.g. "Bash(rm -rf /tmp/test)"
  toolName: text("tool_name").notNull(),
  // JSON: the arguments the agent wants to pass to the tool
  toolArguments: text("tool_arguments").notNull().default("{}"),
  // Human-readable description of what the agent wants to do
  description: text("description").notNull().default(""),
  // Status: "pending" | "approved" | "rejected"
  status: text("status").notNull().default("pending"),
  // If user chose "remember this choice", the rule is saved as a permanent Allow
  remember: integer("remember", { mode: "boolean" }).notNull().default(false),
  // Optional user note when approving/rejecting
  userNote: text("user_note"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export const permissionRequestsRelations = relations(permissionRequests, ({ one }) => ({
  agent: one(agents, {
    fields: [permissionRequests.agentId],
    references: [agents.id],
  }),
}));

export type PermissionRequest = typeof permissionRequests.$inferSelect;
export type NewPermissionRequest = typeof permissionRequests.$inferInsert;
