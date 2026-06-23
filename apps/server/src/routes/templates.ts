import type { FastifyInstance } from "fastify";
import { TemplateService } from "@hiveweave/core";
import { db } from "@hiveweave/db";

const templateService = new TemplateService(db);

/**
 * Template catalog endpoints.
 * GET /api/templates            — list templates (filterable)
 * GET /api/templates/divisions  — division stats
 * GET /api/templates/:id        — single template with full prompt
 */
export async function templateRoutes(fastify: FastifyInstance) {
  // GET /api/templates?source=&division=&role=&search=
  fastify.get<{
    Querystring: { source?: string; division?: string; role?: string; search?: string };
  }>("/", async (request) => {
    const { source, division, role, search } = request.query;
    return templateService.listTemplates({ source, division, role, search });
  });

  // GET /api/templates/divisions
  fastify.get("/divisions", async () => {
    return templateService.getDivisions();
  });

  // GET /api/templates/:id
  fastify.get<{ Params: { id: string } }>("/:id", async (request, reply) => {
    const template = await templateService.getTemplate(request.params.id);
    if (!template) {
      return reply.status(404).send({ error: "Template not found" });
    }
    return template;
  });
}
