import { z } from "zod";

// --- Enums ---

export const MemoryScope = z.enum(["project", "agent", "archive"]);
export type MemoryScope = z.infer<typeof MemoryScope>;

export const MemoryType = z.enum([
  "knowledge",
  "decision",
  "lesson",
  "error",
  "log",
  "handoff_summary",
  "merge_summary",
]);
export type MemoryType = z.infer<typeof MemoryType>;

// --- Schema ---

export const MemorySchema = z.object({
  id: z.string().uuid(),
  agentId: z.string().uuid().nullable(),
  scope: MemoryScope,
  moduleId: z.string().uuid().nullable(),
  type: MemoryType,
  content: z.string(),
  sourceAgentId: z.string().uuid().nullable(),
  metadata: z.record(z.string(), z.unknown()),
  createdAt: z.string().datetime(),
});
export type Memory = z.infer<typeof MemorySchema>;
