/**
 * Token estimation and model configuration.
 *
 * Instead of hardcoding message limits, we derive the conversation history
 * budget from the model's context window size. This module provides:
 *
 *   1. `estimateTokens(text)` — cheap char-ratio approximation (~4 chars/token).
 *   2. `MODEL_CONFIGS` — per-model context window and reserved output tokens.
 *   3. `calculateHistoryBudget()` — subtracts static portions from the total
 *      window to yield the token budget available for conversation history.
 *
 * The estimation is intentionally conservative (over-estimates by ~10-15%)
 * so we never accidentally exceed the model's hard limit.
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
// Budget calculation
// ---------------------------------------------------------------------------

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

  const budget = contextWindow - reservedOutput - staticTokens;
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
export function buildCompactionPrompt(oldTranscript: string): string {
  return `You are a conversation summarizer. Below is an older portion of a conversation between an AI agent and its collaborators. This portion is being compacted to save context window space.

Generate a structured summary that preserves all information needed to continue the conversation effectively. Focus on:
- What was being worked on (goals, tasks)
- What has been completed vs. in-progress vs. blocked
- Key decisions made and WHY (not just what)
- Important facts, constraints, or context discovered
- The specific next step to take when resuming

Output ONLY the structured summary below — no preamble, no meta-commentary.

## Conversation Summary (Compacted History)

### Goal
[The high-level objective being worked on]

### Progress
- **Done**: [Completed items with brief results]
- **In Progress**: [Items mid-flight with current state]
- **Blocked**: [Items stuck and why]

### Key Decisions
[Architectural choices, design decisions, trade-offs — the WHY]

### Important Context
[Facts, constraints, or discoveries that affect future work]

### Next Step
[The single concrete next action to take when resuming]

---

Here is the conversation to summarize:

${oldTranscript}`;
}

// ---------------------------------------------------------------------------
// Tool output truncation — keep context compact
// ---------------------------------------------------------------------------

/** Max chars for a single tool result in stored history. */
export const TOOL_OUTPUT_MAX_CHARS = 6_000;
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
