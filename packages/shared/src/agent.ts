import { z } from "zod";

// --- Enums ---

export const AgentStatus = z.enum([
  "created",
  "active",
  "promoted",
  "receiving",
  "merging",
  "dissolving",
  "archived",
]);
export type AgentStatus = z.infer<typeof AgentStatus>;

export const PermissionType = z.enum(["coordinator", "executor"]);
export type PermissionType = z.infer<typeof PermissionType>;

export const AgentRole = z.enum(["architect", "manager", "module_dev"]);
export type AgentRole = z.infer<typeof AgentRole>;

// --- Schema ---

export const AgentSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  role: AgentRole,
  parentId: z.string().uuid().nullable(),
  moduleId: z.string().uuid().nullable(),
  status: AgentStatus,
  goal: z.string(),
  backstory: z.string(),
  skills: z.array(z.string()),
  permissionType: PermissionType,
});
export type Agent = z.infer<typeof AgentSchema>;
