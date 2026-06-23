/**
 * Provider Factory — create AI SDK provider instances from model registry config.
 *
 * Maps the `provider` field from llm_models to the correct Vercel AI SDK
 * provider package. Falls back to `@ai-sdk/openai-compatible` for any
 * unrecognized provider string (DeepSeek, Groq, Cerebras, TogetherAI, etc.).
 */

import { createOpenAI } from "@ai-sdk/openai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { createGoogleGenerativeAI } from "@ai-sdk/google";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

/** Supported provider identifiers. */
export type ProviderType = "openai" | "anthropic" | "google" | "openai-compatible";

/**
 * Create an AI SDK provider instance from config.
 *
 * @param provider - Provider type string from the model registry.
 * @param baseUrl  - API base URL (e.g. "https://api.openai.com/v1").
 * @param apiKey   - API key for authentication.
 * @returns A provider factory function that accepts a model ID string.
 */
export function createProviderInstance(provider: string, baseUrl: string, apiKey: string) {
  switch (provider) {
    case "openai":
      return createOpenAI({
        baseURL: baseUrl,
        apiKey,
      });
    case "anthropic":
      return createAnthropic({
        baseURL: baseUrl,
        apiKey,
      });
    case "google":
      return createGoogleGenerativeAI({
        baseURL: baseUrl,
        apiKey,
      });
    default:
      // openai-compatible: covers DeepSeek, Groq, Cerebras, TogetherAI, etc.
      return createOpenAICompatible({
        name: provider || "custom",
        baseURL: baseUrl,
        apiKey,
      });
  }
}
