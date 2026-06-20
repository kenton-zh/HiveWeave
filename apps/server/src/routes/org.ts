import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { OrgService } from "@hiveweave/core";
import { db } from "@hiveweave/db";
import { agents } from "@hiveweave/db";
import { eq } from "drizzle-orm";
import { AgentRole, PermissionType } from "@hiveweave/shared";

// ---------------------------------------------------------------------------
// Validation schemas
// ---------------------------------------------------------------------------

const CreateAgentBody = z.object({
  name: z.string().min(1),
  role: AgentRole,
  goal: z.string(),
  backstory: z.string().default(""),
  skills: z.array(z.string()).default([]),
  parentId: z.string().uuid().optional(),
  moduleId: z.string().uuid().optional(),
  permissionType: PermissionType,
});

const UpdateAgentBody = z.object({
  name: z.string().min(1).optional(),
  goal: z.string().optional(),
  status: z.string().optional(),
  backstory: z.string().optional(),
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function orgRoutes(fastify: FastifyInstance) {
  const orgService = new OrgService();

  /**
   * GET / — Get the full organization tree.
   * Returns an array of root OrgNode trees.
   */
  fastify.get("/", async (_request, reply) => {
    try {
      const tree = await orgService.getOrgTree();
      return tree;
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
      const id = await orgService.createAgent(parsed.data);
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
      const agent = await orgService.getAgent(id);
      if (!agent) {
        return reply.status(404).send({ error: "Agent not found" });
      }
      return agent;
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
      const existing = await orgService.getAgent(id);
      if (!existing) {
        return reply.status(404).send({ error: "Agent not found" });
      }

      const updates: Record<string, unknown> = { updatedAt: Date.now() };
      if (parsed.data.name !== undefined) updates.name = parsed.data.name;
      if (parsed.data.goal !== undefined) updates.goal = parsed.data.goal;
      if (parsed.data.status !== undefined) updates.status = parsed.data.status;
      if (parsed.data.backstory !== undefined) updates.backstory = parsed.data.backstory;

      await db.update(agents).set(updates).where(eq(agents.id, id));

      const updated = await orgService.getAgent(id);
      return updated;
    } catch (error: any) {
      fastify.log.error(error, "Failed to update agent");
      return reply.status(500).send({ error: "Failed to update agent", details: error.message });
    }
  });

  /**
   * GET /agents/:id/children — Get direct children of an agent.
   */
  fastify.get<{ Params: { id: string } }>("/agents/:id/children", async (request, reply) => {
    const { id } = request.params;

    try {
      const parent = await orgService.getAgent(id);
      if (!parent) {
        return reply.status(404).send({ error: "Parent agent not found" });
      }

      const children = await orgService.getChildren(id);
      return children;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch children");
      return reply.status(500).send({ error: "Failed to fetch children", details: error.message });
    }
  });

  /**
   * GET /modules — Get all registered modules.
   */
  fastify.get("/modules", async (_request, reply) => {
    try {
      const allModules = await orgService.getModules();
      return allModules;
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch modules");
      return reply.status(500).send({ error: "Failed to fetch modules", details: error.message });
    }
  });
}
