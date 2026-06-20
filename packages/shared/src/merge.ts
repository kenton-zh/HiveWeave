import { z } from "zod";

// --- Schema ---

export const MergeSchema = z.object({
  id: z.string().uuid(),
  sourceAgentIds: z.array(z.string().uuid()),
  targetAgentId: z.string().uuid(),
  summary: z.string(),
  conflicts: z.record(z.string(), z.unknown()),
  resolution: z.record(z.string(), z.unknown()),
});
export type Merge = z.infer<typeof MergeSchema>;
