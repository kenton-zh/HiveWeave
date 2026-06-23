import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { OrgService, DispatchService } from "@hiveweave/core";
import { getProjectDbForAgent } from "@hiveweave/db";

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const LimitQuery = z.object({
  limit: z.coerce.number().int().min(1).max(100).default(10),
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function logRoutes(fastify: FastifyInstance) {
  // Helper to get per-project services from an agentId
  async function getProjectServices(agentId: string) {
    const projectDb = getProjectDbForAgent(agentId);
    if (!projectDb) return null;
    return {
      orgService: new OrgService(projectDb),
      dispatchService: new DispatchService(projectDb),
    };
  }

  /**
   * GET /:agentId — Get work logs for a specific agent.
   *
   * Query params:
   *   - limit (default 10, max 100): max number of log entries to return.
   *
   * Returns logs ordered newest-first.
   */
  fastify.get<{
    Params: { agentId: string };
    Querystring: { limit?: string };
  }>("/:agentId", async (request, reply) => {
    const { agentId } = request.params;

    const parsedLimit = LimitQuery.safeParse(request.query);
    if (!parsedLimit.success) {
      return reply.status(400).send({
        error: "Validation failed",
        issues: parsedLimit.error.issues,
      });
    }

    try {
      const services = await getProjectServices(agentId);
      if (!services) {
        return reply.status(404).send({ error: "Agent not found in any project" });
      }

      const agent = await services.orgService.getAgent(agentId);
      if (!agent) {
        return reply.status(404).send({ error: "Agent not found" });
      }

      const logs = await services.dispatchService.getAgentLogs(agentId, parsedLimit.data.limit);
      return { agentId, logs, count: logs.length };
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch work logs");
      return reply.status(500).send({ error: "Failed to fetch work logs", details: error.message });
    }
  });

  /**
   * GET /:agentId/subordinates — Get work logs from all direct subordinates.
   *
   * This implements the coordinator's log-reading protocol (日志读取协议):
   * the coordinator can view recent activity from every direct report
   * without needing to query each subordinate individually.
   *
   * Query params:
   *   - limit (default 10, max 100): max log entries per subordinate.
   *
   * Returns a map of { subordinateId: { name, logs } }.
   */
  fastify.get<{
    Params: { agentId: string };
    Querystring: { limit?: string };
  }>("/:agentId/subordinates", async (request, reply) => {
    const { agentId } = request.params;

    const parsedLimit = LimitQuery.safeParse(request.query);
    if (!parsedLimit.success) {
      return reply.status(400).send({
        error: "Validation failed",
        issues: parsedLimit.error.issues,
      });
    }

    try {
      const services = await getProjectServices(agentId);
      if (!services) {
        return reply.status(404).send({ error: "Agent not found in any project" });
      }

      const agent = await services.orgService.getAgent(agentId);
      if (!agent) {
        return reply.status(404).send({ error: "Agent not found" });
      }

      const children = await services.orgService.getChildren(agentId);
      if (children.length === 0) {
        return { agentId, subordinates: {}, message: "No subordinates found" };
      }

      const result: Record<string, { name: string; role: string; logs: any[] }> = {};

      for (const child of children) {
        const logs = await services.dispatchService.getSubordinateLogs(child.id, parsedLimit.data.limit);
        result[child.id] = {
          name: child.name,
          role: child.role,
          logs,
        };
      }

      return { agentId, subordinates: result };
    } catch (error: any) {
      fastify.log.error(error, "Failed to fetch subordinate logs");
      return reply.status(500).send({
        error: "Failed to fetch subordinate logs",
        details: error.message,
      });
    }
  });
}
