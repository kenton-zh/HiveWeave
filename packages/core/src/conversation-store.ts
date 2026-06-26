/**
 * ConversationStore — per-agent persistent conversation history.
 *
 * Design goals (driven by model context window, not hardcoded limits):
 *
 *   1. **Token-budget trimming**: instead of MAX_MESSAGES, history is trimmed
 *      based on a caller-provided token budget derived from the model's
 *      context window size (see `token-utils.ts`).
 *
 *   2. **Persistent sessions**: each agent maintains a single long-lived
 *      conversation thread. History is persisted to SQLite via the
 *      `conversation_turns` table and survives server restarts.
 *
 *   3. **Lazy loading**: history is loaded from DB on first access per
 *      agent, then cached in memory. Subsequent reads hit the cache.
 *
 *   4. **Turn-level trimming**: we trim by complete turns (user + assistant
 *      + tool exchanges), never breaking assistant(tool_calls) / tool(result)
 *      pairs.
 *
 *   5. **Smart compaction (CodeWhale-inspired)**: when old turns must be
 *      discarded to fit the budget, instead of hard-truncating, an optional
 *      `compactor` callback summarizes them via LLM into a structured handoff.
 *      This "compacted prefix" is prepended to recent history on subsequent
 *      calls, preserving goals/decisions/context that would otherwise be lost.
 *
 * Cache-friendly design for DeepSeek prefix caching:
 *   - The identity prompt (first system message) stays constant → cache hit.
 *   - Dynamic context (memories, handoffs, inbox) goes in the second system
 *     message → may change between calls without affecting the cached prefix.
 *   - Compacted prefix (if any) goes as a third system message in history.
 *   - Conversation history follows after all system messages.
 */

import { randomUUID } from "crypto";
import { conversationTurns } from "@hiveweave/db";
import type { Database } from "@hiveweave/db";
import { eq, asc, and } from "drizzle-orm";
import { estimateTokens } from "./token-utils.js";

// ---------------------------------------------------------------------------
// Types (mirrors the OpenAI message subset used by AgentRuntime)
// ---------------------------------------------------------------------------

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export type StoredMessage =
  | { role: "system"; content: string }
  | { role: "user"; content: string }
  | { role: "assistant"; content: string | null; tool_calls?: ToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string };

// ---------------------------------------------------------------------------
// Store configuration
// ---------------------------------------------------------------------------

/**
 * Configuration for the conversation store.
 *
 * The `compactor` is an async callback that summarizes old conversation turns
 * into a structured handoff string. It is called when trimming would otherwise
 * discard messages. If the compactor fails or is not provided, the store
 * falls back to hard truncation (oldest turns dropped).
 *
 * The callback is injected by the caller (typically chat.ts) which has
 * access to the LLM API key and can make the summarization call.
 */
export interface StoreConfig {
  /**
   * Summarize old messages into a compact handoff string.
   * Returns null on failure → store falls back to hard truncation.
   */
  compactor?: (oldMessages: StoredMessage[]) => Promise<string | null>;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

class ConversationStore {
  /** In-memory cache: agentId → flat StoredMessage[] (recent turns only). */
  private cache: Map<string, StoredMessage[]> = new Map();
  /** Compacted prefix cache: agentId → structured summary of old turns. */
  private compactedPrefixCache: Map<string, string> = new Map();
  /** Store configuration (compactor callback, etc.). */
  private config: StoreConfig = {};

  // -------------------------------------------------------------------------
  // Configuration
  // -------------------------------------------------------------------------

  /**
   * Configure the store. Call once at startup.
   *
   * @param config - Store configuration including the optional compactor.
   */
  configure(config: StoreConfig): void {
    this.config = { ...config };
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * Get an agent's conversation history, loading from DB on first access.
   *
   * The returned array may start with a `{ role: "system" }` message
   * containing the compacted prefix (if old turns were previously summarized).
   * This integrates seamlessly with the AgentRuntime's message pipeline —
   * system messages in history are placed between the identity/context prompts
   * and the conversation turns.
   *
   * @param agentId      - The agent's UUID.
   * @param tokenBudget  - Max tokens for the returned history. Messages
   *                       exceeding this budget are trimmed from the oldest
   *                       turns first (or compacted if a compactor is set).
   * @param db           - The per-project database instance.
   */
  async getHistory(agentId: string, tokenBudget: number, db: Database): Promise<StoredMessage[]> {
    // Hit cache
    if (this.cache.has(agentId)) {
      return this.prependCompacted(agentId, this.cache.get(agentId)!);
    }

    // Cold start: load all turns from DB
    const turns = await db
      .select()
      .from(conversationTurns)
      .where(eq(conversationTurns.agentId, agentId))
      .orderBy(asc(conversationTurns.turnIndex));

    // Check for a previously persisted compacted prefix (turnIndex = -1)
    const compactedTurn = turns.find((t) => t.turnIndex === -1);
    if (compactedTurn) {
      try {
        const msgs = JSON.parse(compactedTurn.rawMessages) as StoredMessage[];
        if (msgs.length > 0 && msgs[0].role === "system") {
          this.compactedPrefixCache.set(agentId, msgs[0].content);
        }
      } catch {
        // Ignore corrupt compacted prefix
      }
    }

    // Flatten non-compacted turns into a single message array
    const regularTurns = turns.filter((t) => t.turnIndex >= 0);
    const allMessages: StoredMessage[] = [];
    for (const turn of regularTurns) {
      try {
        const msgs = JSON.parse(turn.rawMessages) as StoredMessage[];
        allMessages.push(...msgs);
      } catch {
        // Skip corrupt turns
      }
    }

    // Trim to token budget (accounting for compacted prefix tokens)
    const trimmed = await this.trimSmart(agentId, allMessages, tokenBudget, db);

    // Cache recent turns (without compacted prefix — that's separate)
    this.cache.set(agentId, trimmed);

    return this.prependCompacted(agentId, trimmed);
  }

  /**
   * Append a turn (user msg + assistant reply + tool exchanges) to the
   * agent's persistent history.
   *
   * Steps:
   *   1. Ensure history is loaded (warm cache).
   *   2. Append new messages to the flat history.
   *   3. Save the new turn as a separate DB row.
   *   4. If total tokens exceed budget, compact or trim oldest turns.
   *
   * @param agentId     - The agent's UUID.
   * @param messages    - New messages from this turn (no system messages).
   * @param tokenBudget - Max tokens for history (same as getHistory).
   * @param db          - The per-project database instance.
   */
  async appendTurn(
    agentId: string,
    messages: StoredMessage[],
    tokenBudget: number,
    db: Database,
  ): Promise<void> {
    if (messages.length === 0) return;

    // Ensure cache is warm
    const existing = await this.getHistory(agentId, tokenBudget, db);

    // Strip compacted prefix from existing — it's managed separately
    const recentExisting = existing.filter((m) => m.role !== "system" || !this.isCompactedPrefix(agentId, m));
    const combined = [...recentExisting, ...messages];

    // Save the new turn to DB (always persist before compaction)
    const turnIndex = await this.getNextTurnIndex(agentId, db);
    const turnTokens = messages.reduce(
      (sum, m) => sum + estimateTokens(this.serializeMessage(m)),
      0,
    );

    await db.insert(conversationTurns).values({
      id: randomUUID(),
      agentId,
      turnIndex,
      rawMessages: JSON.stringify(messages),
      approxTokens: turnTokens,
      createdAt: Date.now(),
    });

    // Smart trim — may trigger compaction
    const trimmed = await this.trimSmart(agentId, combined, tokenBudget, db);
    this.cache.set(agentId, trimmed);
  }

  /** Clear a single agent's history (cache + DB). */
  async clear(agentId: string, db: Database): Promise<void> {
    this.cache.delete(agentId);
    this.compactedPrefixCache.delete(agentId);
    await db.delete(conversationTurns).where(eq(conversationTurns.agentId, agentId));
  }

  /** Clear all agents' in-memory caches (e.g. on project reset). */
  async clearAll(): Promise<void> {
    this.cache.clear();
    this.compactedPrefixCache.clear();
  }

  /**
   * Get the compacted prefix (cumulative summary of old turns) for an agent.
   * Returns undefined if no compaction has occurred yet.
   *
   * This is the single source of truth — the mid-turn compactor should read
   * from here to enable incremental summaries across multiple compactions.
   */
  getCompactedPrefix(agentId: string): string | undefined {
    return this.compactedPrefixCache.get(agentId);
  }

  /**
   * Set the compacted prefix for an agent. Called after a successful mid-turn
   * compaction to enable incremental summaries across multiple compactions.
   *
   * @param agentId - The agent's UUID.
   * @param prefix  - The compacted summary string.
   * @param db      - The per-project database instance (for persistence).
   */
  async setCompactedPrefix(agentId: string, prefix: string, db: Database): Promise<void> {
    this.compactedPrefixCache.set(agentId, prefix);
    await this.persistCompacted(agentId, prefix, db);
  }

  /** Get the number of cached messages for an agent (0 if not loaded). */
  size(agentId: string): number {
    return (this.cache.get(agentId) || []).length;
  }

  /**
   * Get the approximate token count of cached history for an agent.
   * Includes the compacted prefix if present.
   * Returns 0 if the agent's history hasn't been loaded yet.
   */
  cachedTokenCount(agentId: string): number {
    const msgs = this.cache.get(agentId);
    if (!msgs) return 0;
    let total = msgs.reduce(
      (sum, m) => sum + estimateTokens(this.serializeMessage(m)),
      0,
    );
    const prefix = this.compactedPrefixCache.get(agentId);
    if (prefix) {
      total += estimateTokens(prefix);
    }
    return total;
  }

  // -------------------------------------------------------------------------
  // Smart trimming
  // -------------------------------------------------------------------------

  /**
   * Trim messages to fit within a token budget.
   *
   * Strategy (inspired by CodeWhale's smart compaction):
   *
   *   1. If everything fits → return as-is.
   *   2. Split into [oldMessages, recentMessages] at a turn boundary.
   *   3. If a `compactor` is configured → summarize old messages via LLM,
   *      store the result as a compacted prefix, return recent messages.
   *   4. Otherwise → hard truncate (drop oldest turns).
   */
  private async trimSmart(
    agentId: string,
    messages: StoredMessage[],
    tokenBudget: number,
    db: Database,
  ): Promise<StoredMessage[]> {
    if (messages.length === 0) return [];

    const totalTokens = this.calculateTokens(messages);
    const existingPrefix = this.compactedPrefixCache.get(agentId) || "";
    const prefixTokens = estimateTokens(existingPrefix);

    if (totalTokens + prefixTokens <= tokenBudget) return messages;

    // Need to trim — split at a turn boundary keeping recent turns
    const { oldMessages, recentMessages } = this.splitAtTurnBoundary(messages, tokenBudget, prefixTokens);

    if (oldMessages.length === 0) return recentMessages;

    // Try LLM compaction if a compactor is configured
    if (this.config.compactor) {
      try {
        const summary = await this.config.compactor(oldMessages);

        if (summary) {
          this.compactedPrefixCache.set(agentId, summary);
          await this.persistCompacted(agentId, summary, db);
          return recentMessages;
        }
      } catch (err) {
        console.error(`[ConversationStore] Compaction failed for ${agentId}:`, err);
        // Fall through to hard truncation
      }
    }

    // Fallback: hard truncation (drop old turns) + persist to DB
    if (recentMessages.length < messages.length) {
      await this.persistTrimmed(agentId, recentMessages, messages.length, db);
    }
    return recentMessages;
  }

  /**
   * Split messages into [old, recent] at a turn boundary (user message).
   *
   * Recent messages are kept from the newest turn backwards until the
   * budget is filled. Everything before that point goes into "old".
   */
  private splitAtTurnBoundary(
    messages: StoredMessage[],
    tokenBudget: number,
    existingPrefixTokens: number,
  ): { oldMessages: StoredMessage[]; recentMessages: StoredMessage[] } {
    const turnBoundaries: number[] = [];
    for (let i = 0; i < messages.length; i++) {
      if (messages[i].role === "user") turnBoundaries.push(i);
    }

    if (turnBoundaries.length <= 1) {
      // Only one turn (or none) — can't split further
      return { oldMessages: [], recentMessages: messages };
    }

    // Work from newest to oldest, accumulating tokens for recent
    let keepFrom = turnBoundaries[turnBoundaries.length - 1];
    let runningTokens = existingPrefixTokens;

    for (let t = turnBoundaries.length - 1; t >= 0; t--) {
      const start = turnBoundaries[t];
      const end = t + 1 < turnBoundaries.length ? turnBoundaries[t + 1] : messages.length;

      let turnTokens = 0;
      for (let i = start; i < end; i++) {
        turnTokens += estimateTokens(this.serializeMessage(messages[i]));
      }

      if (runningTokens + turnTokens > tokenBudget) break;
      runningTokens += turnTokens;
      keepFrom = start;
    }

    // Ensure we always keep at least the last turn
    if (keepFrom >= messages.length) {
      keepFrom = turnBoundaries[turnBoundaries.length - 1];
    }

    return {
      oldMessages: messages.slice(0, keepFrom),
      recentMessages: messages.slice(keepFrom),
    };
  }

  // -------------------------------------------------------------------------
  // Compacted prefix helpers
  // -------------------------------------------------------------------------

  /**
   * Prepend the compacted prefix (if any) to recent messages.
   * Returns a new array — does not mutate the input.
   */
  private prependCompacted(agentId: string, recent: StoredMessage[]): StoredMessage[] {
    const prefix = this.compactedPrefixCache.get(agentId);
    if (!prefix) return [...recent];
    return [{ role: "system", content: prefix }, ...recent];
  }

  /** Check if a system message is the compacted prefix for this agent. */
  private isCompactedPrefix(agentId: string, msg: StoredMessage): boolean {
    if (msg.role !== "system") return false;
    const prefix = this.compactedPrefixCache.get(agentId);
    return prefix !== undefined && msg.content === prefix;
  }

  /**
   * Convert old messages to a readable transcript for the compactor prompt.
   * Includes the previous compacted prefix (if any) so the new summary
   * is cumulative — it covers the full conversation arc.
   */
  private messagesToTranscript(messages: StoredMessage[], previousPrefix: string): string {
    const parts: string[] = [];

    if (previousPrefix) {
      parts.push(`[Previous Summary]\n${previousPrefix}\n`);
    }

    for (const msg of messages) {
      switch (msg.role) {
        case "system":
          parts.push(`[System]: ${msg.content.slice(0, 200)}...`);
          break;
        case "user":
          parts.push(`[User]: ${msg.content}`);
          break;
        case "assistant": {
          let text = msg.content || "";
          if (msg.tool_calls) {
            for (const tc of msg.tool_calls) {
              const args = tc.function.arguments.length > 200
                ? tc.function.arguments.slice(0, 200) + "..."
                : tc.function.arguments;
              text += `\n  → tool_call: ${tc.function.name}(${args})`;
            }
          }
          parts.push(`[Assistant]: ${text}`);
          break;
        }
        case "tool": {
          const content = msg.content.length > 500
            ? msg.content.slice(0, 500) + "..."
            : msg.content;
          parts.push(`[Tool Result]: ${content}`);
          break;
        }
      }
    }

    return parts.join("\n\n");
  }

  /** Persist the compacted prefix as a DB row with turnIndex = -1. */
  private async persistCompacted(agentId: string, prefix: string, db: Database): Promise<void> {
    // Delete any existing compacted prefix
    await db
      .delete(conversationTurns)
      .where(and(
        eq(conversationTurns.agentId, agentId),
        eq(conversationTurns.turnIndex, -1),
      ));

    await db.insert(conversationTurns).values({
      id: randomUUID(),
      agentId,
      turnIndex: -1,
      rawMessages: JSON.stringify([{ role: "system", content: prefix }]),
      approxTokens: estimateTokens(prefix),
      createdAt: Date.now(),
    });
  }

  // -------------------------------------------------------------------------
  // DB helpers
  // -------------------------------------------------------------------------

  /**
   * After trimming, rewrite the DB rows to match the trimmed in-memory state.
   *
   * Strategy: delete all existing rows for this agent, then insert the
   * remaining messages as a single consolidated turn. This is simpler
   * than trying to figure out which original turns survived trimming.
   */
  private async persistTrimmed(
    agentId: string,
    messages: StoredMessage[],
    _oldTurnCount: number,
    db: Database,
  ): Promise<void> {
    await db
      .delete(conversationTurns)
      .where(eq(conversationTurns.agentId, agentId));

    if (messages.length === 0) return;

    // Re-group messages into turns for cleaner storage
    const turns = this.groupIntoTurns(messages);

    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      const turnTokens = turn.reduce(
        (sum, m) => sum + estimateTokens(this.serializeMessage(m)),
        0,
      );
      await db.insert(conversationTurns).values({
        id: randomUUID(),
        agentId,
        turnIndex: i,
        rawMessages: JSON.stringify(turn),
        approxTokens: turnTokens,
        createdAt: Date.now(),
      });
    }
  }

  /**
   * Group a flat message array into turns.
   * Each turn starts with a user message and includes all following
   * assistant + tool messages until the next user message.
   */
  private groupIntoTurns(messages: StoredMessage[]): StoredMessage[][] {
    const turns: StoredMessage[][] = [];
    let current: StoredMessage[] = [];

    for (const msg of messages) {
      if (msg.role === "user" && current.length > 0) {
        turns.push(current);
        current = [];
      }
      current.push(msg);
    }
    if (current.length > 0) {
      turns.push(current);
    }

    return turns;
  }

  /** Get the next turn index for an agent (max existing + 1, or 0). */
  private async getNextTurnIndex(agentId: string, db: Database): Promise<number> {
    const turns = await db
      .select()
      .from(conversationTurns)
      .where(eq(conversationTurns.agentId, agentId))
      .orderBy(asc(conversationTurns.turnIndex));

    if (turns.length === 0) return 0;
    return Math.max(...turns.map((t) => t.turnIndex)) + 1;
  }

  /** Calculate total tokens for a message array. */
  private calculateTokens(messages: StoredMessage[]): number {
    return messages.reduce(
      (sum, m) => sum + estimateTokens(this.serializeMessage(m)),
      0,
    );
  }

  /**
   * Serialize a StoredMessage to a rough text representation for token
   * estimation. We don't need exact token counts — just a proportional
   * string that estimateTokens() can process.
   */
  private serializeMessage(msg: StoredMessage): string {
    switch (msg.role) {
      case "system":
        return msg.content;
      case "user":
        return msg.content;
      case "assistant": {
        let text = msg.content || "";
        if (msg.tool_calls) {
          for (const tc of msg.tool_calls) {
            text += ` ${tc.function.name} ${tc.function.arguments}`;
          }
        }
        return text;
      }
      case "tool":
        return msg.content;
      default:
        return "";
    }
  }
}

export const conversationStore = new ConversationStore();
