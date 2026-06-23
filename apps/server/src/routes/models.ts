import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { ModelService } from "@hiveweave/core";
import { db } from "@hiveweave/db";

// ---------------------------------------------------------------------------
// Validation schemas
// ---------------------------------------------------------------------------

const CreateModelBody = z.object({
  name: z.string().min(1),
  modelId: z.string().min(1),
  baseUrl: z.string().url(),
  apiKey: z.string().min(1),
  provider: z.enum(["openai", "anthropic", "google", "openai-compatible"]).default("openai-compatible"),
  supportsImages: z.boolean().default(false),
  contextWindow: z.number().int().positive().default(128000),
  maxOutputTokens: z.number().int().positive().default(8192),
  supportsThinking: z.boolean().default(false),
  defaultReasoningEffort: z.string().nullable().default(null),
  temperature: z.string().nullable().default(null),
});

const UpdateModelBody = z.object({
  name: z.string().min(1).optional(),
  modelId: z.string().min(1).optional(),
  baseUrl: z.string().url().optional(),
  apiKey: z.string().min(1).optional(),
  provider: z.enum(["openai", "anthropic", "google", "openai-compatible"]).optional(),
  supportsImages: z.boolean().optional(),
  contextWindow: z.number().int().positive().optional(),
  maxOutputTokens: z.number().int().positive().optional(),
  supportsThinking: z.boolean().optional(),
  defaultReasoningEffort: z.string().nullable().optional(),
  temperature: z.string().nullable().optional(),
  isActive: z.boolean().optional(),
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function modelRoutes(fastify: FastifyInstance) {
  const modelService = new ModelService(db);

  /** GET / — List all active models */
  fastify.get("/", async (_request, reply) => {
    const models = await modelService.list();
    // Mask API keys in list response
    const safe = models.map((m) => ({
      ...m,
      apiKey: m.apiKey.slice(0, 8) + "..." + m.apiKey.slice(-4),
    }));
    return reply.send(safe);
  });

  /** GET /all — List all models including inactive */
  fastify.get("/all", async (_request, reply) => {
    const models = await modelService.getAll();
    const safe = models.map((m) => ({
      ...m,
      apiKey: m.apiKey.slice(0, 8) + "..." + m.apiKey.slice(-4),
    }));
    return reply.send(safe);
  });

  /** GET /:id — Get a single model */
  fastify.get<{ Params: { id: string } }>("/:id", async (request, reply) => {
    const model = await modelService.getById(request.params.id);
    if (!model) return reply.status(404).send({ error: "Model not found" });
    return reply.send({
      ...model,
      apiKey: model.apiKey.slice(0, 8) + "..." + model.apiKey.slice(-4),
    });
  });

  /** POST / — Create a new model */
  fastify.post("/", async (request, reply) => {
    const body = CreateModelBody.parse(request.body);
    const model = await modelService.create(body);
    return reply.status(201).send(model);
  });

  /** PATCH /:id — Update a model */
  fastify.patch<{ Params: { id: string } }>("/:id", async (request, reply) => {
    const body = UpdateModelBody.parse(request.body);
    const model = await modelService.update(request.params.id, body);
    if (!model) return reply.status(404).send({ error: "Model not found" });
    return reply.send(model);
  });

  /** DELETE /:id — Delete a model */
  fastify.delete<{ Params: { id: string } }>("/:id", async (request, reply) => {
    await modelService.delete(request.params.id);
    return reply.send({ ok: true });
  });

  /** POST /:id/test — Test model connection */
  fastify.post<{ Params: { id: string } }>("/:id/test", async (request, reply) => {
    const model = await modelService.getById(request.params.id);
    if (!model) return reply.status(404).send({ error: "Model not found" });

    const start = Date.now();
    try {
      const res = await fetch(`${model.baseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${model.apiKey}`,
        },
        body: JSON.stringify({
          model: model.modelId,
          messages: [{ role: "user", content: "Say 'OK' and nothing else." }],
          max_tokens: 10,
        }),
      });

      if (!res.ok) {
        const errBody = await res.text();
        return reply.send({ ok: false, latencyMs: Date.now() - start, error: `API error ${res.status}: ${errBody.slice(0, 200)}` });
      }

      const data = await res.json() as any;
      const content = data.choices?.[0]?.message?.content || "";
      return reply.send({ ok: true, latencyMs: Date.now() - start, response: content.slice(0, 100) });
    } catch (err: any) {
      return reply.send({ ok: false, latencyMs: Date.now() - start, error: err.message || "Connection failed" });
    }
  });
}
