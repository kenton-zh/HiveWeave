import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { SettingsService } from "@hiveweave/core";
import { db } from "@hiveweave/db";

// ---------------------------------------------------------------------------
// Validation schemas
// ---------------------------------------------------------------------------

const UpdateSettingsBody = z.record(z.string());

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function settingsRoutes(fastify: FastifyInstance) {
  const settingsService = new SettingsService(db);

  /** GET / — Get all global settings */
  fastify.get("/", async (_request, reply) => {
    const settings = await settingsService.getAll();
    return reply.send(settings);
  });

  /** GET /:key — Get a single setting */
  fastify.get<{ Params: { key: string } }>("/:key", async (request, reply) => {
    const { key } = request.params;
    const value = await settingsService.get(key);
    return reply.send({ key, value });
  });

  /** PUT / — Set one or more settings */
  fastify.put("/", async (request, reply) => {
    const body = UpdateSettingsBody.parse(request.body);
    await settingsService.setMany(body);
    return reply.send({ ok: true });
  });
}
