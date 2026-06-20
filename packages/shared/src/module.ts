import { z } from "zod";

// --- Enums ---

export const ModuleStatus = z.enum(["active", "completed", "archived"]);
export type ModuleStatus = z.infer<typeof ModuleStatus>;

// --- Schema ---

export const ModuleSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  parentModuleId: z.string().uuid().nullable(),
  status: ModuleStatus,
  currentAgentId: z.string().uuid().nullable(),
});
export type Module = z.infer<typeof ModuleSchema>;
