import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { relations } from "drizzle-orm";
import { modules } from "./modules";

export const agents = sqliteTable("agents", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  role: text("role", {
    enum: ["architect", "manager", "module_dev"],
  }).notNull(),
  parentId: text("parent_id").references((): typeof agents.id => agents.id),
  moduleId: text("module_id").references(() => modules.id),
  status: text("status", {
    enum: ["created", "active", "idle", "completed", "terminated"],
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
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export const agentsRelations = relations(agents, ({ one, many }) => ({
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
