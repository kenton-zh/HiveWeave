import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const llmModels = sqliteTable("llm_models", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  modelId: text("model_id").notNull(),
  baseUrl: text("base_url").notNull(),
  apiKey: text("api_key").notNull(),
  /** Provider type: "openai" | "anthropic" | "google" | "openai-compatible" */
  provider: text("provider").notNull().default("openai-compatible"),
  /** Whether this model supports multimodal image input */
  supportsImages: integer("supports_images", { mode: "boolean" }).notNull().default(false),
  contextWindow: integer("context_window").notNull().default(128000),
  maxOutputTokens: integer("max_output_tokens").notNull().default(8192),
  supportsThinking: integer("supports_thinking", { mode: "boolean" }).notNull().default(false),
  defaultReasoningEffort: text("default_reasoning_effort"),
  temperature: text("temperature"),
  isActive: integer("is_active", { mode: "boolean" }).notNull().default(true),
  createdAt: integer("created_at", { mode: "number" }).notNull(),
  updatedAt: integer("updated_at", { mode: "number" }).notNull(),
});

export type LlmModel = typeof llmModels.$inferSelect;
export type NewLlmModel = typeof llmModels.$inferInsert;
