/**
 * Provider Factory — create AI SDK provider instances from model registry config.
 *
 * Maps the `provider` field from llm_models to the correct Vercel AI SDK
 * provider package. Falls back to `@ai-sdk/openai-compatible` for any
 * unrecognized provider string (DeepSeek, Groq, Cerebras, TogetherAI, etc.).
 *
 * 防线 ①: All providers inject a custom fetch with a request-level timeout.
 * This catches half-open TCP connections that never respond. The stream-level
 * idle watchdog (防线 ②, in stream-timeout.ts) handles the more granular
 * "stream opened but no chunks" case.
 */

import { createOpenAI } from "@ai-sdk/openai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { createGoogleGenerativeAI } from "@ai-sdk/google";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

/** Supported provider identifiers. */
export type ProviderType = "openai" | "anthropic" | "google" | "openai-compatible";

/**
 * Request-level timeout for the underlying HTTP fetch (防线 ①).
 *
 * This is a hard upper bound on a single HTTP request. For streaming responses
 * the stream may stay open much longer — the idle watchdog (防线 ②) handles that.
 * Set high enough to avoid killing legitimate long first-token waits on
 * thinking models. Tunable via env.
 */
const REQUEST_TIMEOUT_MS = Number(process.env.HW_REQUEST_TIMEOUT_MS ?? 180_000); // 3 min

/**
 * Custom fetch wrapper that injects a request-level AbortSignal.timeout.
 *
 * Merges with any signal the AI SDK may already pass (e.g. for client-side
 * cancellation). Uses AbortSignal.any() (Node 18.17+/20+) to combine them.
 */
function timeoutFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const timeoutSignal = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  const signal = init?.signal
    ? AbortSignal.any([init.signal, timeoutSignal])
    : timeoutSignal;
  return fetch(input, { ...init, signal });
}

/**
 * Create an AI SDK provider instance from config.
 *
 * @param provider - Provider type string from the model registry.
 * @param baseUrl  - API base URL (e.g. "https://api.openai.com/v1").
 * @param apiKey   - API key for authentication.
 * @returns A provider factory function that accepts a model ID string.
 */
export function createProviderInstance(provider: string, baseUrl: string, apiKey: string) {
  const fetch = timeoutFetch;
  switch (provider) {
    case "openai":
      return createOpenAI({
        baseURL: baseUrl,
        apiKey,
        fetch,
      });
    case "anthropic":
      return createAnthropic({
        baseURL: baseUrl,
        apiKey,
        fetch,
      });
    case "google":
      return createGoogleGenerativeAI({
        baseURL: baseUrl,
        apiKey,
        fetch,
      });
    default:
      // openai-compatible: covers DeepSeek, Groq, Cerebras, TogetherAI, etc.
      return createOpenAICompatible({
        name: provider || "custom",
        baseURL: baseUrl,
        apiKey,
        fetch,
      });
  }
}
