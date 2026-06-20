import { z } from "zod";

// --- Enums ---

export const WorkLogType = z.enum([
  "code_change",
  "bug_fix",
  "feature",
  "refactor",
  "discussion",
]);
export type WorkLogType = z.infer<typeof WorkLogType>;

// --- Schema ---

export const WorkLogSchema = z.object({
  id: z.string().uuid(),
  agentId: z.string().uuid(),
  sessionId: z.string(),
  type: WorkLogType,
  summary: z.string(),
  details: z.record(z.string(), z.unknown()),
  createdAt: z.string().datetime(),
});
export type WorkLog = z.infer<typeof WorkLogSchema>;
