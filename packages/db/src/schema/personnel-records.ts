import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { relations } from "drizzle-orm";
import { projects } from "./projects";
import { agents } from "./agents";

export const personnelRecords = sqliteTable("personnel_records", {
  id: text("id").primaryKey(),
  projectId: text("project_id")
    .notNull()
    .references(() => projects.id),
  agentId: text("agent_id")
    .notNull()
    .references(() => agents.id, { onDelete: "cascade" }),
  position: text("position").notNull().default(""),
  department: text("department").notNull().default(""),
  responsibilities: text("responsibilities").notNull().default(""),
  notes: text("notes").notNull().default(""),
  status: text("status").notNull().default("active"), // active | inactive | probation | terminated
  createdBy: text("created_by")
    .notNull()
    .references(() => agents.id),
  updatedBy: text("updated_by")
    .notNull()
    .references(() => agents.id),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export const personnelRecordsRelations = relations(personnelRecords, ({ one }) => ({
  project: one(projects, {
    fields: [personnelRecords.projectId],
    references: [projects.id],
  }),
  agent: one(agents, {
    fields: [personnelRecords.agentId],
    references: [agents.id],
    relationName: "personnelAgent",
  }),
  creator: one(agents, {
    fields: [personnelRecords.createdBy],
    references: [agents.id],
    relationName: "personnelCreator",
  }),
  updater: one(agents, {
    fields: [personnelRecords.updatedBy],
    references: [agents.id],
    relationName: "personnelUpdater",
  }),
}));

export type PersonnelRecord = typeof personnelRecords.$inferSelect;
export type NewPersonnelRecord = typeof personnelRecords.$inferInsert;
