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

export const PermissionMode = z.enum(["readonly", "readwrite", "full", "custom"]);
export type PermissionMode = z.infer<typeof PermissionMode>;

export const AgentRole = z.string().min(1);
export type AgentRole = z.infer<typeof AgentRole>;

/** Suggested role presets for the UI — the backend accepts any non-empty string. */
export const ROLE_PRESETS = [
  "ceo",
  "hr",
  "architect",
  "manager",
  "developer",
  "qa",
  "devops",
] as const;

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
  permissionMode: PermissionMode,
  allowedTools: z.array(z.string()),
  deniedTools: z.array(z.string()),
  askTools: z.array(z.string()),
  mcpServers: z.array(z.string()),
  boundSkills: z.array(z.string()),
});
export type Agent = z.infer<typeof AgentSchema>;
