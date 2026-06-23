import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { OrgService, MemoryService, RosterService, communicationService, statusEventBus } from "@hiveweave/core";
import { db, projects, getProjectDbForAgent, ensureProjectDb, lookupAgentWorkspace } from "@hiveweave/db";
import { eq } from "drizzle-orm";
import { PermissionType } from "@hiveweave/shared";

// ---------------------------------------------------------------------------
// Validation schemas
// ---------------------------------------------------------------------------

const CreateAgentBody = z.object({
  name: z.string().min(1),
  role: z.string().min(1),
  goal: z.string(),
  backstory: z.string().default(""),
  skills: z.array(z.string()).default([]),
  parentId: z.string().uuid().optional(),
  moduleId: z.string().uuid().optional(),
  projectId: z.string().uuid().optional(),
  permissionType: PermissionType,
  modelId: z.string().uuid().optional(),
  reasoningEffort: z.string().optional(),
});

const UpdateAgentBody = z.object({
  name: z.string().min(1).optional(),
  goal: z.string().optional(),
  status: z.string().optional(),
  backstory: z.string().optional(),
  modelId: z.string().uuid().nullable().optional(),
  reasoningEffort: z.string().nullable().optional(),
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function orgRoutes(fastify: FastifyInstance) {
  /** Get per-project services from an agentId (most endpoints use this). */
  function getProjectServicesForAgent(agentId: string) {
    const projectDb = getProjectDbForAgent(agentId);
    if (!projectDb) return null;
    const wsPath = lookupAgentWorkspace(agentId);
    return {
      db: projectDb,
      orgService: new OrgService(projectDb, wsPath),
      memoryService: new MemoryService(projectDb),
      rosterService: new RosterService(projectDb),
    };
  }

  /** Get per-project OrgService from a projectId (for org tree / list endpoints). */
  async function getOrgServicesForProject(projectId?: string) {
    if (!projectId) return null;
    const rows = await db.select().from(projects).where(eq(projects.id, projectId));
    if (rows.length === 0 || !rows[0].workspacePath) return null;
    const projectDb = ensureProjectDb(rows[0].workspacePath);
    return {
      orgService: new OrgService(projectDb, rows[0].workspacePath),
      memoryService: new MemoryService(projectDb),
      rosterService: new RosterService(projectDb),
    };
  }

  /** Recursively inject isProcessing into OrgNode tree */
  function injectProcessing(nodes: any[]): any[] {
    return nodes.map((node) => ({
      ...node,
      isProcessing: statusEventBus.isProcessing(node.id),
      children: node.children ? injectProcessing(node.children) : [],
    }));
  }

  /**
   * GET / — Get the full organization tree.
   * Returns an array of root OrgNode trees.
   */
  fastify.get<{ Querystring: { projectId?: string } }>("/", async (request, reply) => {
    try {
      const projectId = request.query.projectId;
      const services = await getOrgServicesForProject(projectId);
      if (!services) {
        return reply.status(400).send({ error: "projectId with valid workspacePath is required" });
      }
      const tree = await services.orgService.getOrgTree(projectId);
      return injectProcessing(tree);
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch org tree");
      return reply.status(500).send({ error: "Failed to fetch org tree", details: error.message });
    }
  });

  /**
   * POST /agents — Create a new agent.
   * Validates the request body and returns the new agent's ID.
   */
  fastify.post<{ Body: z.infer<typeof CreateAgentBody> }>("/agents", async (request, reply) => {
    const parsed = CreateAgentBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({
        error: "Validation failed",
        issues: parsed.error.issues,
      });
    }

    try {
      const services = await getOrgServicesForProject(parsed.data.projectId);
      if (!services) {
        return reply.status(400).send({ error: "projectId with valid workspacePath is required" });
      }
      const id = await services.orgService.createAgent(parsed.data);
      return reply.status(201).send({ id });
    } catch (error: any) {
      fastify.log.error(error, "Failed to create agent");
      return reply.status(500).send({ error: "Failed to create agent", details: error.message });
    }
  });

  /**
   * GET /agents/:id — Get a single agent by ID.
   */
  fastify.get<{ Params: { id: string } }>("/agents/:id", async (request, reply) => {
    const { id } = request.params;

    try {
      const services = getProjectServicesForAgent(id);
      if (!services) return reply.status(404).send({ error: "Agent not found in any project" });

      const agent = await services.orgService.getAgent(id);
      if (!agent) {
        return reply.status(404).send({ error: "Agent not found" });
      }
      return { ...agent, isProcessing: statusEventBus.isProcessing(id) };
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch agent");
      return reply.status(500).send({ error: "Failed to fetch agent", details: error.message });
    }
  });

  /**
   * PATCH /agents/:id — Update an agent's mutable fields (status, goal, name, backstory).
   */
  fastify.patch<{
    Params: { id: string };
    Body: z.infer<typeof UpdateAgentBody>;
  }>("/agents/:id", async (request, reply) => {
    const { id } = request.params;

    const parsed = UpdateAgentBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({
        error: "Validation failed",
        issues: parsed.error.issues,
      });
    }

    try {
      const services = getProjectServicesForAgent(id);
      if (!services) return reply.status(404).send({ error: "Agent not found in any project" });

      const existing = await services.orgService.getAgent(id);
      if (!existing) {
        return reply.status(404).send({ error: "Agent not found" });
      }

      const updates: Record<string, unknown> = { updatedAt: Date.now() };
      if (parsed.data.name !== undefined) updates.name = parsed.data.name;
      if (parsed.data.goal !== undefined) updates.goal = parsed.data.goal;
      if (parsed.data.status !== undefined) updates.status = parsed.data.status;
      if (parsed.data.backstory !== undefined) updates.backstory = parsed.data.backstory;
      if (parsed.data.modelId !== undefined) updates.modelId = parsed.data.modelId;
      if (parsed.data.reasoningEffort !== undefined) updates.reasoningEffort = parsed.data.reasoningEffort;

      await services.orgService.updateAgent(id, updates);

      const updated = await services.orgService.getAgent(id);
      return updated;
    } catch (error: any) {
      fastify.log.error(error, "Failed to update agent");
      return reply.status(500).send({ error: "Failed to update agent", details: error.message });
    }
  });

  /**
   * DELETE /agents/:id — Delete an agent.
   *
   * Archives the agent's private memories before removing the record.
   * Refuses if the agent has subordinate children.
   */
  fastify.delete<{ Params: { id: string } }>("/agents/:id", async (request, reply) => {
    const { id } = request.params;

    try {
      const services = getProjectServicesForAgent(id);
      if (!services) return reply.status(404).send({ error: "Agent not found in any project" });

      // Archive private memories so successors on the same module can access them
      const archivedCount = await services.memoryService.archiveAgentMemories(id);

      // Attempt deletion (will refuse if children exist)
      const result = await services.orgService.deleteAgent(id);
      if (!result.ok) {
        return reply.status(409).send({ error: result.reason });
      }

      return { deleted: true, archivedMemories: archivedCount };
    } catch (error: any) {
      fastify.log.error(error, "Failed to delete agent");
      return reply.status(500).send({ error: "Failed to delete agent", details: error.message });
    }
  });

  /**
   * GET /agents/:id/children — Get direct children of an agent.
   */
  fastify.get<{ Params: { id: string } }>("/agents/:id/children", async (request, reply) => {
    const { id } = request.params;

    try {
      const services = getProjectServicesForAgent(id);
      if (!services) return reply.status(404).send({ error: "Parent agent not found in any project" });

      const parent = await services.orgService.getAgent(id);
      if (!parent) {
        return reply.status(404).send({ error: "Parent agent not found" });
      }

      const children = await services.orgService.getChildren(id);
      return children;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch children");
      return reply.status(500).send({ error: "Failed to fetch children", details: error.message });
    }
  });

  /**
   * GET /modules — Get all registered modules.
   * Modules are per-project; this endpoint requires a projectId query param.
   */
  fastify.get<{ Querystring: { projectId?: string } }>("/modules", async (request, reply) => {
    try {
      const services = await getOrgServicesForProject(request.query.projectId);
      if (!services) return reply.status(400).send({ error: "projectId with valid workspacePath is required" });
      const allModules = await services.orgService.getModules();
      return allModules;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch modules");
      return reply.status(500).send({ error: "Failed to fetch modules", details: error.message });
    }
  });

  /**
   * GET /communications — Get active agent-to-agent communications.
   * Returns communications that are still within their TTL window.
   * The frontend uses this to render animated arrows between communicating agents.
   */
  fastify.get("/communications", async (_request, _reply) => {
    return communicationService.getActiveCommunications();
  });

  // ---------------------------------------------------------------------------
  // Roster (Personnel Records) endpoints
  // ---------------------------------------------------------------------------

  /**
   * GET /roster/:projectId — Get the full personnel roster for a project.
   */
  fastify.get<{ Params: { projectId: string } }>("/roster/:projectId", async (request, reply) => {
    const { projectId } = request.params;
    try {
      const services = await getOrgServicesForProject(projectId);
      if (!services) return reply.status(400).send({ error: "projectId with valid workspacePath is required" });
      const records = await services.rosterService.getProjectRoster(projectId);
      return records;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch roster");
      return reply.status(500).send({ error: "Failed to fetch roster", details: error.message });
    }
  });

  /**
   * GET /roster/agent/:agentId — Get the roster record for a single agent.
   */
  fastify.get<{ Params: { agentId: string } }>("/roster/agent/:agentId", async (request, reply) => {
    const { agentId } = request.params;
    try {
      const services = getProjectServicesForAgent(agentId);
      if (!services) return reply.status(404).send({ error: "Agent not found in any project" });
      const record = await services.rosterService.getAgentRecord(agentId);
      if (!record) {
        return reply.status(404).send({ error: "Roster record not found" });
      }
      return record;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch roster record");
      return reply.status(500).send({ error: "Failed to fetch roster record", details: error.message });
    }
  });
}
