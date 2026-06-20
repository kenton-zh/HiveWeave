import { z } from "zod";
import { AgentRole, AgentStatus } from "./agent.js";

// --- Request Schemas ---

export const DispatchTaskRequest = z.object({
  agentId: z.string().uuid(),
  description: z.string(),
  priority: z.number().int().optional(),
});
export type DispatchTaskRequest = z.infer<typeof DispatchTaskRequest>;

export const ChatMessage = z.object({
  role: z.enum(["user", "assistant", "system"]),
  content: z.string(),
});
export type ChatMessage = z.infer<typeof ChatMessage>;

export const ChatRequest = z.object({
  agentId: z.string().uuid(),
  message: z.string(),
  sessionId: z.string().optional(),
});
export type ChatRequest = z.infer<typeof ChatRequest>;

export const AgentCreateRequest = z.object({
  name: z.string(),
  role: AgentRole,
  goal: z.string(),
  backstory: z.string(),
  skills: z.array(z.string()),
  parentId: z.string().uuid().optional(),
  moduleId: z.string().uuid().optional(),
});
export type AgentCreateRequest = z.infer<typeof AgentCreateRequest>;

export const AgentUpdateRequest = z.object({
  name: z.string().optional(),
  goal: z.string().optional(),
  status: AgentStatus.optional(),
});
export type AgentUpdateRequest = z.infer<typeof AgentUpdateRequest>;

// --- Response / Tree Schemas ---

export const OrgNode: z.ZodType<OrgNode> = z.object({
  id: z.string().uuid(),
  name: z.string(),
  role: AgentRole,
  status: AgentStatus,
  goal: z.string(),
  children: z.lazy(() => z.array(OrgNode)),
});

export interface OrgNode {
  id: string;
  name: string;
  role: z.infer<typeof AgentRole>;
  status: z.infer<typeof AgentStatus>;
  goal: string;
  children: OrgNode[];
}
