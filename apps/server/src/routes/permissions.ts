import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { PermissionService, ApprovalService } from "@hiveweave/core";
import { getProjectDbForAgent, ensureProjectDb, db, projects } from "@hiveweave/db";
import { eq } from "drizzle-orm";

// ---------------------------------------------------------------------------
// Validation schemas
// ---------------------------------------------------------------------------

const UpdateRulesBody = z.object({
  permissionMode: z.enum(["readonly", "readwrite", "full", "custom"]).optional(),
  allowedTools: z.array(z.string()).optional(),
  deniedTools: z.array(z.string()).optional(),
  askTools: z.array(z.string()).optional(),
  mcpServers: z.array(z.string()).optional(),
  boundSkills: z.array(z.string()).optional(),
});

const RespondBody = z.object({
  requestId: z.string().uuid(),
  approved: z.boolean(),
  remember: z.boolean().default(false),
  userNote: z.string().optional(),
  projectId: z.string().uuid().optional(), // needed to find per-project DB
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Get per-project PermissionService from agentId */
function getPermServiceForAgent(agentId: string) {
  const projectDb = getProjectDbForAgent(agentId);
  if (!projectDb) return null;
  return new PermissionService(projectDb);
}

/** Get per-project ApprovalService from agentId */
function getApprovalServiceForAgent(agentId: string) {
  const projectDb = getProjectDbForAgent(agentId);
  if (!projectDb) return null;
  return new ApprovalService(projectDb);
}

/** Get per-project ApprovalService from projectId */
async function getApprovalServiceForProject(projectId: string) {
  const rows = await db.select().from(projects).where(eq(projects.id, projectId));
  if (rows.length === 0 || !rows[0].workspacePath) return null;
  return new ApprovalService(ensureProjectDb(rows[0].workspacePath));
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function permissionRoutes(fastify: FastifyInstance) {

  /**
   * GET /rules/:agentId — Get effective permission rules for an agent.
   */
  fastify.get<{ Params: { agentId: string } }>("/rules/:agentId", async (request, reply) => {
    try {
      const permService = getPermServiceForAgent(request.params.agentId);
      if (!permService) return reply.status(404).send({ error: "Agent not found in any project" });

      const rules = await permService.getEffectiveRules(request.params.agentId);
      if (!rules) {
        return reply.status(404).send({ error: "Agent not found" });
      }
      return rules;
    } catch (error: any) {
      fastify.log.error(error, "Failed to get permission rules");
      return reply.status(500).send({ error: "Failed to get permission rules", details: error.message });
    }
  });

  /**
   * PATCH /rules/:agentId — Update permission rules for an agent.
   */
  fastify.patch<{
    Params: { agentId: string };
    Body: z.infer<typeof UpdateRulesBody>;
  }>("/rules/:agentId", async (request, reply) => {
    const parsed = UpdateRulesBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid body", issues: parsed.error.issues });
    }

    try {
      const permService = getPermServiceForAgent(request.params.agentId);
      if (!permService) return reply.status(404).send({ error: "Agent not found in any project" });

      await permService.updateRules(request.params.agentId, parsed.data);
      const updated = await permService.getEffectiveRules(request.params.agentId);
      return updated;
    } catch (error: any) {
      fastify.log.error(error, "Failed to update permission rules");
      return reply.status(500).send({ error: "Failed to update rules", details: error.message });
    }
  });

  /**
   * GET /pending/:agentId — Get pending approval requests for an agent.
   */
  fastify.get<{ Params: { agentId: string } }>("/pending/:agentId", async (request, reply) => {
    try {
      const approvalService = getApprovalServiceForAgent(request.params.agentId);
      if (!approvalService) return reply.status(404).send({ error: "Agent not found in any project" });

      const requests = await approvalService.getPendingRequests(request.params.agentId);
      return requests;
    } catch (error: any) {
      fastify.log.error(error, "Failed to get pending requests");
      return reply.status(500).send({ error: "Failed to get pending requests", details: error.message });
    }
  });

  /**
   * GET /pending/project/:projectId — Get all pending approval requests for a project.
   */
  fastify.get<{ Params: { projectId: string } }>("/pending/project/:projectId", async (request, reply) => {
    try {
      const approvalService = await getApprovalServiceForProject(request.params.projectId);
      if (!approvalService) return reply.status(400).send({ error: "Project not found or has no workspace" });

      const requests = await approvalService.getAllPendingForProject(request.params.projectId);
      return requests;
    } catch (error: any) {
      fastify.log.error(error, "Failed to get project pending requests");
      return reply.status(500).send({ error: "Failed to get project pending requests", details: error.message });
    }
  });

  /**
   * POST /respond — Approve or reject a pending approval request.
   * Requires projectId in body to locate the per-project DB.
   */
  fastify.post<{ Body: z.infer<typeof RespondBody> }>("/respond", async (request, reply) => {
    const parsed = RespondBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid body", issues: parsed.error.issues });
    }

    try {
      // Try to resolve approval service from projectId or scan all agents
      let approvalService: ApprovalService | null = null;
      if (parsed.data.projectId) {
        approvalService = await getApprovalServiceForProject(parsed.data.projectId);
      }
      if (!approvalService) {
        return reply.status(400).send({ error: "projectId is required to respond to approval requests" });
      }

      const result = await approvalService.respondToRequest(parsed.data);
      if (!result.ok) {
        return reply.status(400).send({ error: result.reason });
      }
      return result;
    } catch (error: any) {
      fastify.log.error(error, "Failed to respond to approval request");
      return reply.status(500).send({ error: "Failed to respond", details: error.message });
    }
  });
}
