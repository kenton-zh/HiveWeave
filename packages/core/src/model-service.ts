import { llmModels } from "@hiveweave/db";
import type { MetaDatabase } from "@hiveweave/db";
import type { LlmModel } from "@hiveweave/db";
import { eq, asc } from "drizzle-orm";
import { randomUUID } from "crypto";

export type { LlmModel };

export interface CreateModelInput {
  name: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  /** Provider type: "openai" | "anthropic" | "google" | "openai-compatible" */
  provider?: string;
  /** Whether this model supports multimodal image input */
  supportsImages?: boolean;
  contextWindow?: number;
  maxOutputTokens?: number;
  supportsThinking?: boolean;
  defaultReasoningEffort?: string | null;
  temperature?: string | null;
}

/**
 * CRUD service for the LLM model registry (meta DB).
 *
 * Each model entry defines an API endpoint, credentials, context window,
 * and capability flags (thinking support). Agents reference models by ID.
 */
export class ModelService {
  constructor(private readonly metaDb: MetaDatabase) {}

  /** List all active models (default for UI dropdowns). */
  async list(): Promise<LlmModel[]> {
    return this.metaDb
      .select()
      .from(llmModels)
      .where(eq(llmModels.isActive, true))
      .orderBy(asc(llmModels.createdAt));
  }

  /** List all models including inactive ones. */
  async getAll(): Promise<LlmModel[]> {
    return this.metaDb
      .select()
      .from(llmModels)
      .orderBy(asc(llmModels.createdAt));
  }

  /** Get a single model by ID. Returns null if not found. */
  async getById(id: string): Promise<LlmModel | null> {
    const rows = await this.metaDb
      .select()
      .from(llmModels)
      .where(eq(llmModels.id, id));
    return rows[0] || null;
  }

  /** Get the default model (first active model). */
  async getDefault(): Promise<LlmModel | null> {
    const rows = await this.metaDb
      .select()
      .from(llmModels)
      .where(eq(llmModels.isActive, true))
      .orderBy(asc(llmModels.createdAt))
      .limit(1);
    return rows[0] || null;
  }

  /** Create a new model entry. */
  async create(input: CreateModelInput): Promise<LlmModel> {
    const id = randomUUID();
    const now = Date.now();
    const record: LlmModel = {
      id,
      name: input.name,
      modelId: input.modelId,
      baseUrl: input.baseUrl,
      apiKey: input.apiKey,
      provider: input.provider ?? "openai-compatible",
      supportsImages: input.supportsImages ?? false,
      contextWindow: input.contextWindow ?? 128_000,
      maxOutputTokens: input.maxOutputTokens ?? 8_192,
      supportsThinking: input.supportsThinking ?? false,
      defaultReasoningEffort: input.defaultReasoningEffort ?? null,
      temperature: input.temperature ?? null,
      isActive: true,
      createdAt: now,
      updatedAt: now,
    };

    await this.metaDb.insert(llmModels).values(record);
    return record;
  }

  /** Update an existing model entry. */
  async update(id: string, patch: Partial<CreateModelInput> & { isActive?: boolean }): Promise<LlmModel | null> {
    const existing = await this.getById(id);
    if (!existing) return null;

    const updated = { ...existing, ...patch, updatedAt: Date.now() };
    await this.metaDb
      .update(llmModels)
      .set({
        name: updated.name,
        modelId: updated.modelId,
        baseUrl: updated.baseUrl,
        apiKey: updated.apiKey,
        provider: updated.provider ?? "openai-compatible",
        supportsImages: updated.supportsImages ?? false,
        contextWindow: updated.contextWindow,
        maxOutputTokens: updated.maxOutputTokens,
        supportsThinking: updated.supportsThinking,
        defaultReasoningEffort: updated.defaultReasoningEffort ?? null,
        temperature: updated.temperature ?? null,
        isActive: updated.isActive,
        updatedAt: updated.updatedAt,
      })
      .where(eq(llmModels.id, id));

    return updated;
  }

  /** Delete a model entry. */
  async delete(id: string): Promise<boolean> {
    const result = await this.metaDb.delete(llmModels).where(eq(llmModels.id, id));
    return true;
  }
}
