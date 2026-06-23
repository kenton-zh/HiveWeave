/**
 * Retry & Error Classification Utilities
 *
 * Ported from opencode's executor.ts / provider-error.ts as plain TypeScript
 * (no Effect dependency). Provides:
 *   - HTTP status classification (retryable vs. permanent)
 *   - Retry-After header parsing (ms, seconds, HTTP-date)
 *   - Rate-limit header extraction (OpenAI + Anthropic)
 *   - Exponential backoff with random jitter
 *   - Context overflow detection (18 regex patterns across providers)
 *   - Error reason classification (auth / rate-limit / quota / overflow / server)
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maximum number of retries (aligned with opencode). */
export const MAX_RETRIES = 2;

/** Base delay for exponential backoff. */
const BASE_DELAY_MS = 500;

/** Maximum delay cap. */
const MAX_DELAY_MS = 10_000;

// ---------------------------------------------------------------------------
// Retryable status codes
// ---------------------------------------------------------------------------

/** HTTP statuses that should be retried. */
const RETRYABLE_STATUSES = new Set([429, 503, 504, 529]);

export function isRetryableStatus(status: number): boolean {
  return RETRYABLE_STATUSES.has(status);
}

// ---------------------------------------------------------------------------
// Retry-After header parsing
// ---------------------------------------------------------------------------

/**
 * Parse the `retry-after-ms` or `retry-after` header from an HTTP response.
 *
 * Supports three formats:
 *   1. `retry-after-ms: 5000` — milliseconds
 *   2. `retry-after: 5` — seconds
 *   3. `retry-after: Wed, 21 Oct 2025 07:28:00 GMT` — HTTP-date
 *
 * @returns Delay in milliseconds, or undefined if no header present.
 */
export function parseRetryAfterMs(headers: Record<string, string>): number | undefined {
  // retry-after-ms (milliseconds, non-standard but used by some providers)
  const millis = Number(headers["retry-after-ms"]);
  if (Number.isFinite(millis)) return Math.max(0, millis);

  const value = headers["retry-after"];
  if (!value) return undefined;

  // retry-after as seconds
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);

  // retry-after as HTTP-date
  const date = Date.parse(value);
  if (!Number.isNaN(date)) return Math.max(0, date - Date.now());

  return undefined;
}

// ---------------------------------------------------------------------------
// Rate-limit header extraction
// ---------------------------------------------------------------------------

export interface RateLimitInfo {
  retryAfterMs?: number;
  limit?: Record<string, string>;
  remaining?: Record<string, string>;
  reset?: Record<string, string>;
}

/**
 * Extract rate-limit details from response headers.
 * Supports both OpenAI (`x-ratelimit-*`) and Anthropic (`anthropic-ratelimit-*`) formats.
 */
export function extractRateLimitDetails(
  headers: Record<string, string>,
  retryAfter?: number,
): RateLimitInfo | undefined {
  const limit: Record<string, string> = {};
  const remaining: Record<string, string> = {};
  const reset: Record<string, string> = {};

  for (const [name, value] of Object.entries(headers)) {
    // OpenAI format: x-ratelimit-limit-requests, x-ratelimit-remaining-tokens, etc.
    const openaiLimit = /^x-ratelimit-limit-(.+)$/.exec(name)?.[1];
    if (openaiLimit) { limit[openaiLimit] = value; continue; }

    const openaiRemaining = /^x-ratelimit-remaining-(.+)$/.exec(name)?.[1];
    if (openaiRemaining) { remaining[openaiRemaining] = value; continue; }

    const openaiReset = /^x-ratelimit-reset-(.+)$/.exec(name)?.[1];
    if (openaiReset) { reset[openaiReset] = value; continue; }

    // Anthropic format: anthropic-ratelimit-requests-limit, anthropic-ratelimit-tokens-remaining, etc.
    const anthropic = /^anthropic-ratelimit-(.+)-(limit|remaining|reset)$/.exec(name);
    if (anthropic) {
      const [, resource, type] = anthropic;
      if (type === "limit") limit[resource] = value;
      else if (type === "remaining") remaining[resource] = value;
      else reset[resource] = value;
    }
  }

  if (
    retryAfter === undefined &&
    Object.keys(limit).length === 0 &&
    Object.keys(remaining).length === 0 &&
    Object.keys(reset).length === 0
  ) {
    return undefined;
  }

  return {
    retryAfterMs: retryAfter,
    limit: Object.keys(limit).length > 0 ? limit : undefined,
    remaining: Object.keys(remaining).length > 0 ? remaining : undefined,
    reset: Object.keys(reset).length > 0 ? reset : undefined,
  };
}

// ---------------------------------------------------------------------------
// Error classification
// ---------------------------------------------------------------------------

export type ErrorCategory =
  | "authentication"        // 401, 403 — invalid API key or insufficient permissions
  | "rate-limit"           // 429 — rate limited (retryable)
  | "quota-exceeded"       // 429 + quota keywords (NOT retryable)
  | "content-policy"       // content filter / safety violation
  | "context-overflow"     // 400/413 + context length patterns
  | "invalid-request"      // 400, 404, 409, 422 — bad request (not retryable)
  | "server-error"         // 500, 502, 503 — server issue (retryable)
  | "network-error"        // fetch threw — connection failed (retryable)
  | "unknown";             // anything else

export interface ClassifiedError {
  category: ErrorCategory;
  retryable: boolean;
  message: string;
  /** Suggested retry delay in ms (from Retry-After header), if available. */
  retryAfterMs?: number;
  /** Rate-limit details from response headers, if available. */
  rateLimit?: RateLimitInfo;
  /** HTTP status code, if available. */
  status?: number;
}

/**
 * Classify an HTTP error response into a structured error category.
 *
 * @param status  HTTP status code
 * @param body    Response body text (used for pattern matching)
 * @param headers Response headers as a plain object
 */
export function classifyHttpError(
  status: number,
  body: string,
  headers: Record<string, string> = {},
): ClassifiedError {
  const retryAfter = parseRetryAfterMs(headers);
  const rateLimit = extractRateLimitDetails(headers, retryAfter);
  const message = body.length <= 500
    ? `Provider request failed with HTTP ${status}: ${body}`
    : `Provider request failed with HTTP ${status}`;

  // Content policy violation
  if (/content[-_\s]?policy|content_filter|safety/i.test(body)) {
    return { category: "content-policy", retryable: false, message, status };
  }

  // Authentication
  if (status === 401) {
    return { category: "authentication", retryable: false, message: `${message} (invalid API key)`, status };
  }
  if (status === 403) {
    return { category: "authentication", retryable: false, message: `${message} (insufficient permissions)`, status };
  }

  // Rate limit (429) — distinguish rate limit from quota exceeded
  if (status === 429) {
    if (/insufficient[-_\s]?quota|quota[-_\s]?exceeded/i.test(body)) {
      return { category: "quota-exceeded", retryable: false, message, status, rateLimit };
    }
    return { category: "rate-limit", retryable: true, message, status, retryAfterMs: retryAfter, rateLimit };
  }

  // Client errors (400, 404, 409, 413, 422)
  if (status === 400 || status === 404 || status === 409 || status === 413 || status === 422) {
    const overflow = isContextOverflow(body);
    return {
      category: overflow ? "context-overflow" : "invalid-request",
      retryable: false,
      message: overflow ? `${message} (context overflow detected)` : message,
      status,
    };
  }

  // Server errors (5xx) — retryable
  if (status >= 500 || isRetryableStatus(status)) {
    return { category: "server-error", retryable: true, message, status, retryAfterMs: retryAfter };
  }

  return { category: "unknown", retryable: false, message, status };
}

/**
 * Classify a network error (fetch threw an exception).
 */
export function classifyNetworkError(error: Error): ClassifiedError {
  return {
    category: "network-error",
    retryable: true,
    message: `Network error: ${error.message || "unknown"}`,
  };
}

// ---------------------------------------------------------------------------
// Backoff computation
// ---------------------------------------------------------------------------

/**
 * Compute the retry delay for a given attempt using exponential backoff with
 * random jitter. If a Retry-After value is provided, it takes precedence
 * (capped at MAX_DELAY_MS).
 *
 * Formula: BASE * 2^attempt * jitter, where jitter is uniform in [0.8, 1.2].
 *
 * @param attempt     Current attempt number (0-based).
 * @param retryAfter  Optional Retry-After delay in ms (from header).
 * @returns Delay in milliseconds before the next retry.
 */
export function computeBackoff(attempt: number, retryAfter?: number): number {
  if (retryAfter !== undefined) {
    return Math.min(retryAfter, MAX_DELAY_MS);
  }
  const jitter = 0.8 + Math.random() * 0.4; // [0.8, 1.2]
  return Math.min(BASE_DELAY_MS * Math.pow(2, attempt) * jitter, MAX_DELAY_MS);
}

// ---------------------------------------------------------------------------
// Context overflow detection (ported from opencode provider-error.ts)
// ---------------------------------------------------------------------------

/** Regex patterns that detect context-length-exceeded errors across providers. */
const CONTEXT_OVERFLOW_PATTERNS: RegExp[] = [
  /prompt is too long/i,
  /input is too long for requested model/i,
  /exceeds the context window/i,
  /input token count.*exceeds the maximum/i,
  /maximum prompt length is \d+/i,
  /reduce the length of the messages/i,
  /maximum context length is \d+ tokens/i,
  /exceeds the limit of \d+/i,
  /exceeds the available context size/i,
  /greater than the context length/i,
  /context window exceeds limit/i,
  /exceeded model token limit/i,
  /context[_ ]length[_ ]exceeded/i,
  /request entity too large/i,
  /context length is only \d+ tokens/i,
  /input length.*exceeds.*context length/i,
  /prompt too long; exceeded (?:max )?context length/i,
  /too large for model with \d+ maximum context length/i,
  /model_context_window_exceeded/i,
];

/**
 * Check whether an error message indicates a context overflow.
 * Matches 18+ patterns covering OpenAI, Anthropic, Google, DeepSeek, etc.
 */
export function isContextOverflow(message: string): boolean {
  return (
    CONTEXT_OVERFLOW_PATTERNS.some((p) => p.test(message)) ||
    /^4(00|13)\s*(status code)?\s*\(no body\)/i.test(message)
  );
}
