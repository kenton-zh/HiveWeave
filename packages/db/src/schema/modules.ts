import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { relations } from "drizzle-orm";
import { agents } from "./agents";

export const modules = sqliteTable("modules", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  parentModuleId: text("parent_module_id").references(
    (): typeof modules.id => modules.id,
  ),
  status: text("status", {
    enum: ["active", "inactive", "archived"],
  })
    .notNull()
    .default("active"),
  currentAgentId: text("current_agent_id"),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export const modulesRelations = relations(modules, ({ one, many }) => ({
  parentModule: one(modules, {
    fields: [modules.parentModuleId],
    references: [modules.id],
    relationName: "parentChild",
  }),
  childModules: many(modules, { relationName: "parentChild" }),
  agents: many(agents),
}));

export type Module = typeof modules.$inferSelect;
export type NewModule = typeof modules.$inferInsert;
