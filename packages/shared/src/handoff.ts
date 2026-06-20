import { z } from "zod";

// --- Enums ---

export const HandoffStatus = z.enum(["pending", "completed", "failed"]);
export type HandoffStatus = z.infer<typeof HandoffStatus>;

// --- Schema ---

export const HandoffSchema = z.object({
  id: z.string().uuid(),
  fromAgentId: z.string().uuid(),
  toAgentId: z.string().uuid().nullable(),
  moduleId: z.string().uuid(),
  summary: z.string(),
  memorySnapshotId: z.string().uuid().nullable(),
  status: HandoffStatus,
});
export type Handoff = z.infer<typeof HandoffSchema>;
