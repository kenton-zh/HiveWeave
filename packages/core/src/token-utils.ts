/**
 * Token estimation and model configuration.
 *
 * Instead of hardcoding message limits, we derive the conversation history
 * budget from the model's context window size. This module provides:
 *
 *   1. `estimateTokens(text)` — cheap char-ratio approximation (~4 chars/token).
 *   2. `calculateHistoryBudget()` — subtracts static portions from the total
 *      window to yield the token budget available for conversation history.
 *   3. `calculateUsableContext()` — determines usable context after reserving
 *      space for model output (aligned with OpenCode's overflow detection).
 *
 * The estimation is intentionally conservative (over-estimates by ~10-15%)
 * so we never accidentally exceed the model's hard limit.
 *
 * Aligned with OpenCode's overflow.ts and compaction.ts patterns.
 */

// ---------------------------------------------------------------------------
// Token estimation
// ---------------------------------------------------------------------------

/**
 * Approximate token count for a text string.
 *
 * Uses a simple char-ratio heuristic: ~4 characters per token for English,
 * ~2 for CJK. We blend at ~3.5 for mixed content (HiveWeave prompts are
 * mostly English with some Chinese).
 *
 * This is NOT a replacement for tiktoken — it's a budget guard, not an
 * exact counter. Accuracy within ±15% is sufficient.
 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  // Count CJK characters (they use ~2 chars/token)
  let cjkCount = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (
      (code >= 0x4e00 && code <= 0x9fff) || // CJK Unified
      (code >= 0x3000 && code <= 0x303f) || // CJK punctuation
      (code >= 0xff00 && code <= 0xffef)    // Fullwidth forms
    ) {
      cjkCount++;
    }
  }
  const nonCjk = text.length - cjkCount;
  return Math.ceil(nonCjk / 4 + cjkCount / 1.5);
}

// ---------------------------------------------------------------------------
// Budget & overflow calculation (aligned with OpenCode)
// ---------------------------------------------------------------------------

/** Hard cap on max output tokens (aligned with OpenCode's ProviderTransform.OUTPUT_TOKEN_MAX). */
export const OUTPUT_TOKEN_MAX = 32_000;
/** Buffer reserved for model output + safety margin (aligned with OpenCode). */
export const COMPACTION_BUFFER = 20_000;

/** Min/max tokens to preserve for recent messages during compaction. */
export const PRESERVE_RECENT_MIN = 2_000;
export const PRESERVE_RECENT_MAX = 8_000;

/** Default tail turns to keep during compaction. */
export const DEFAULT_TAIL_TURNS = 2;

/**
 * Calculate the usable context window for determining when compaction is needed.
 *
 * Aligned with OpenCode's `usable()` in overflow.ts:
 *   - If model has `limitInput`, use that minus reserved
 *   - Otherwise, use context window minus maxOutput
 *   - Reserved = min(COMPACTION_BUFFER, maxOutput)
 *
 * @param contextWindow  - Total context window in tokens.
 * @param maxOutput      - Max output tokens for the model.
 * @param limitInput     - Optional explicit input token limit from the model.
 * @returns Usable token budget before compaction is needed.
 */
export function calculateUsableContext(
  contextWindow: number,
  modelLimitOutput: number,
  limitInput?: number,
): number {
  if (contextWindow === 0) return 0;
  // Cap max output to OUTPUT_TOKEN_MAX (aligned with OpenCode's ProviderTransform.maxOutputTokens)
  const maxOut = Math.min(modelLimitOutput, OUTPUT_TOKEN_MAX) || OUTPUT_TOKEN_MAX;
  const reserved = Math.min(COMPACTION_BUFFER, maxOut);
  if (limitInput !== undefined) {
    return Math.max(0, limitInput - reserved);
  }
  return Math.max(0, contextWindow - maxOut);
}

/**
 * Calculate the token budget to preserve for recent messages during compaction.
 *
 * Aligned with OpenCode's `preserveRecentBudget()`:
 *   - Uses explicit value if provided
 *   - Falls back to 25% of usable context, clamped to [2K, 8K]
 *
 * @param usableContext  - Usable context from calculateUsableContext().
 * @param explicitValue  - Optional explicit preserve value.
 * @returns Token budget for recent messages.
 */
export function calculatePreserveRecentBudget(
  usableContext: number,
  explicitValue?: number,
): number {
  if (explicitValue !== undefined) return explicitValue;
  return Math.max(
    PRESERVE_RECENT_MIN,
    Math.min(PRESERVE_RECENT_MAX, Math.floor(usableContext * 0.25)),
  );
}

/**
 * Calculate the token budget available for conversation history.
 *
 * The total context window is partitioned as:
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ identityPrompt (static, cacheable)                       │ ← always included
 *   │ contextPrompt  (dynamic: memories, handoffs, inbox, …)   │ ← always included
 *   │ currentMessage (the new user message)                    │ ← always included
 *   ├──────────────────────────────────────────────────────────┤
 *   │ conversation history  ← THIS is what we budget for       │
 *   ├──────────────────────────────────────────────────────────┤
 *   │ reserved for model output                                │
 *   └──────────────────────────────────────────────────────────┘
 *
 * Uses the OpenCode-aligned usable context calculation internally.
 *
 * @param contextWindow  - Total context window in tokens (from model registry).
 * @param reservedOutput - Tokens reserved for model output (from model registry).
 * @param identityPrompt - The static identity system prompt.
 * @param contextPrompt  - The dynamic context system prompt.
 * @param currentMessage - The new user message for this turn.
 * @returns Token budget available for history messages.
 */
export function calculateHistoryBudget(
  contextWindow: number,
  reservedOutput: number,
  identityPrompt: string,
  contextPrompt: string,
  currentMessage: string,
): number {
  const staticTokens =
    estimateTokens(identityPrompt) +
    estimateTokens(contextPrompt) +
    estimateTokens(currentMessage);

  // Use the OpenCode-aligned usable context
  const usable = calculateUsableContext(contextWindow, reservedOutput);
  const budget = usable - staticTokens;
  // Floor at 4096 — even a tiny budget should allow a few recent turns
  return Math.max(budget, 4096);
}

// ---------------------------------------------------------------------------
// Prefix hash — detect cache-invalidating changes
// ---------------------------------------------------------------------------

/**
 * Compute a SHA-256 hash of the static prefix (identity prompt + tool catalog).
 *
 * DeepSeek's prefix caching works at the byte level — if even one character
 * in the prefix changes, the cache misses. This hash lets us detect drift:
 *
 *   - Log it each turn → grep for changes across a session.
 *   - Compare consecutive turns → warn when the hash shifts unexpectedly.
 *
 * Uses a simple string hash (FNV-1a 32-bit) rather than crypto SHA-256 to
 * avoid Node.js crypto dependency in core (this package may run in edge
 * runtimes). Sufficient for drift detection.
 */
export function computePrefixHash(
  identityPrompt: string,
  toolDefinitions: string,
): string {
  const input = identityPrompt + "\x00" + toolDefinitions;
  let hash = 0x811c9dc5; // FNV offset basis
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = (hash * 0x01000193) >>> 0; // FNV prime, unsigned
  }
  return hash.toString(16).padStart(8, "0");
}

// ---------------------------------------------------------------------------
// Compaction prompt — structured summary template for old history
// ---------------------------------------------------------------------------

/**
 * Build the compaction prompt used to summarize old conversation history.
 *
 * When history exceeds the token budget, instead of hard-truncating old
 * turns (which causes the agent to "forget" early decisions), we ask the
 * LLM to produce a structured handoff summary. This summary becomes a
 * "compacted prefix" that is prepended to recent turns.
 *
 * Inspired by CodeWhale's compact.md template (Tier 9 Precedent).
 */
export function buildCompactionPrompt(oldTranscript: string, previousSummary?: string): string {
  return [
    previousSummary
      ? `Update the anchored summary below using the conversation history above.\nPreserve still-true details, remove stale details, and merge in the new facts.\n<previous-summary>\n${previousSummary}\n</previous-summary>`
      : "Create a new anchored summary from the conversation history.",
    `Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted.`,
    oldTranscript,
  ].join("\n\n");
}

// ---------------------------------------------------------------------------
// Tool output truncation — keep context compact
// ---------------------------------------------------------------------------

/** Max chars for a single tool result in stored history (normal turns). */
export const TOOL_OUTPUT_MAX_CHARS = 6_000;
/** Max chars for a single tool result during compaction transcript building. */
export const TOOL_OUTPUT_MAX_CHARS_COMPACTION = 2_000;
/** Threshold above which we use head+tail truncation. */
export const TOOL_OUTPUT_TRUNCATE_THRESHOLD = 4_000;

/**
 * Truncate a large tool output for storage in conversation history.
 *
 * Strategy: keep the head and tail (most informative parts — the beginning
 * usually has the structure/context, the end has results/conclusions).
 * Replace the middle with an omission marker showing how much was cut.
 *
 * This happens BEFORE saving to history — the agent sees the full output
 * during the current turn, but future turns only see the compact version.
 *
 * @param output  - The full tool result string.
 * @param maxChars - Maximum characters to keep (default: TOOL_OUTPUT_MAX_CHARS).
 * @returns The original output if small enough, or a truncated version.
 */
export function truncateToolOutput(
  output: string,
  maxChars: number = TOOL_OUTPUT_MAX_CHARS,
): string {
  if (!output || output.length <= TOOL_OUTPUT_TRUNCATE_THRESHOLD) {
    return output;
  }

  const headSize = Math.floor(maxChars * 0.6);
  const tailSize = maxChars - headSize - 80; // 80 chars for marker

  const head = output.slice(0, headSize);
  const tail = output.slice(output.length - tailSize);
  const omitted = output.length - headSize - tailSize;

  return `${head}\n\n... [truncated ${omitted.toLocaleString()} chars — head ${headSize} + tail ${tailSize} kept] ...\n\n${tail}`;
}
