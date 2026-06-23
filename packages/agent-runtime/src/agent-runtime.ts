import { getHiveWeaveTools } from "./permissions.js";
import type { ChatCompletionTool } from "./permissions.js";
import { getParadigmCatalogSummary } from "@hiveweave/shared";
import { streamText, generateText, tool, jsonSchema } from "ai";
import type { ModelMessage, ToolResultPart } from "ai";
import { createProviderInstance } from "./provider-factory.js";
import {
  MAX_RETRIES,
  classifyHttpError,
  classifyNetworkError,
  computeBackoff,
  isContextOverflow,
  parseRetryAfterMs,
} from "./retry-utils.js";

// ---------------------------------------------------------------------------
// Local overflow detection (avoids importing @hiveweave/core)
// ---------------------------------------------------------------------------

/** Approximate token count — char-ratio heuristic (~4 chars/token, ~1.5 for CJK). */
function _estimateTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if ((c >= 0x4e00 && c <= 0x9fff) || (c >= 0x3000 && c <= 0x303f) || (c >= 0xff00 && c <= 0xffef)) cjk++;
  }
  return Math.ceil((text.length - cjk) / 4 + cjk / 1.5);
}

/** Serialize a message to a string for token estimation. */
function _serializeMsg(m: { role: string; content?: string | null; tool_calls?: any[]; tool_call_id?: string }): string {
  let t = m.content || "";
  if (m.tool_calls) for (const tc of m.tool_calls) t += `\n[tc:${tc.function?.name}(${tc.function?.arguments})]`;
  if (m.tool_call_id) t = `[tr:${m.tool_call_id}] ${t}`;
  return t;
}

/** Approximate tokens per image for multimodal models (GPT-4V baseline). */
const IMAGE_TOKEN_COST = 85;

/** Check whether the message array is approaching context overflow (≥85%). */
function _isOverflow(messages: Array<{ role: string; content?: string | null; tool_calls?: any[]; tool_call_id?: string; images?: string[] }>, contextWindow: number, maxOutputTokens: number): boolean {
  const threshold = (contextWindow - maxOutputTokens) * 0.85;
  let total = 0;
  for (const m of messages) {
    total += _estimateTokens(_serializeMsg(m));
    // Account for image token overhead in multimodal messages
    if (m.images && m.images.length > 0) {
      total += m.images.length * IMAGE_TOKEN_COST;
    }
    if (total >= threshold) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Callback interface for executing tools — injected by the caller. */
export interface ToolExecutorCallback {
  execute(
    agentId: string,
    sessionId: string,
    toolName: string,
    input: Record<string, any>,
  ): Promise<string>;
}

/** A message queued from another agent, delivered at a natural breakpoint. */
export interface QueuedMessage {
  fromName: string;
  fromAgentId: string;
  message: string;
  messageType: string;
  expectReport: boolean;
}

/** Callback to poll for queued inbox messages during a running agent turn. */
export type MessagePoller = () => Promise<QueuedMessage[]>;

/** Configuration for creating an AgentRuntime instance. */
export interface AgentRuntimeConfig {
  agentId: string;
  agentName: string;
  role: string;
  permissionType: "coordinator" | "executor";
  goal: string;
  backstory: string;
  /** @deprecated Use identityPrompt + contextPrompt instead. Kept for backward compatibility. */
  systemPrompt: string;
  /** Static identity prompt — stays constant across calls (enables prompt caching). */
  identityPrompt?: string;
  /** Dynamic context (handoffs, inbox, logs) — may change between calls. */
  contextPrompt?: string;
  /** Previous conversation messages to include before the new user message. */
  history?: Message[];
  apiKey: string;
  /** API base URL — resolved from model registry by caller. */
  baseUrl: string;
  /** Model identifier for the API request body — resolved from model registry by caller. */
  model: string;
  /** Provider type for AI SDK: "openai" | "anthropic" | "google" | "openai-compatible". */
  provider?: string;
  /** Whether this model supports multimodal image input. */
  supportsImages?: boolean;
  /** Total context window in tokens — from model registry. Used for overflow detection. */
  contextWindow: number;
  /** Max output tokens for the model — from model registry. */
  maxOutputTokens?: number;
  /** Sampling temperature — null means don't send (use API default). */
  temperature?: number;
  /** Reasoning effort for thinking models: "low"|"medium"|"high"|"max" — null means don't send. */
  reasoningEffort?: string;
  sessionId?: string;
  toolExecutor?: ToolExecutorCallback;
  /** Optional poller for queued inbox messages — called at natural breakpoints between tool turns. */
  messagePoller?: MessagePoller;
  /** Optional permission checker. If undefined, all tools are allowed. */
  permissionChecker?: (
    agentId: string,
    toolName: string,
    toolArgs: Record<string, any>,
  ) => Promise<"allow" | "ask" | "deny">;
  /** Optional approval handler. Called when permissionChecker returns "ask".
   *  Creates a pending approval request and returns its requestId. */
  approvalHandler?: (
    agentId: string,
    toolName: string,
    toolArgs: Record<string, any>,
    description: string,
  ) => Promise<string>;
  /** Optional approval waiter. Blocks until the user approves/rejects or timeout. */
  approvalWaiter?: (
    requestId: string,
  ) => Promise<{ approved: boolean; timedOut: boolean }>;
  /** Optional mid-turn compactor. Summarizes old messages when context overflows. */
  compactor?: (oldMessages: Message[]) => Promise<string | null>;
}

/** A single event emitted during an agent chat stream. */
export interface StreamEvent {
  type: "text" | "tool_use" | "tool_result" | "error" | "done" | "queued_message" | "approval_request" | "compacting" | "thinking" | "retry";
  content: string;
  metadata?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// OpenAI message types (minimal subset)
// ---------------------------------------------------------------------------

interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export type Message =
  | { role: "system"; content: string }
  | { role: "user"; content: string; images?: string[] }
  | { role: "assistant"; content: string | null; tool_calls?: ToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string };

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Safety cap — prevents truly infinite loops from bugs. NOT a design limit. */
const MAX_TURNS = 200;
/** Doom loop threshold — same tool + same args N times in a row. */
const DOOM_LOOP_THRESHOLD = 3;

// ---------------------------------------------------------------------------
// Tool output truncation — keep stored history compact
// ---------------------------------------------------------------------------

/** Threshold above which truncation kicks in (chars). */
const TOOL_OUTPUT_THRESHOLD = 4_000;
/** Maximum chars to keep after truncation (head + tail). */
const TOOL_OUTPUT_MAX = 6_000;

/**
 * Truncate a large tool result for storage in conversation history.
 * Keeps the head (60%) and tail (40%) — usually the most informative parts.
 * The agent sees the full output during the current turn via SSE; this
 * compact version is what gets persisted for future turns.
 */
function truncateForHistory(output: string): string {
  if (!output || output.length <= TOOL_OUTPUT_THRESHOLD) return output;
  const headSize = Math.floor(TOOL_OUTPUT_MAX * 0.6);
  const tailSize = TOOL_OUTPUT_MAX - headSize - 80;
  const head = output.slice(0, headSize);
  const tail = output.slice(output.length - tailSize);
  const omitted = output.length - headSize - tailSize;
  return `${head}\n\n... [truncated ${omitted.toLocaleString()} chars] ...\n\n${tail}`;
}

// ---------------------------------------------------------------------------
// AgentRuntime
// ---------------------------------------------------------------------------

/**
 * Wraps the DeepSeek Chat Completions API with HiveWeave's permission matrix
 * and multi-turn tool execution loop.
 *
 * Each chat() call runs a streaming conversation:
 *   1. Send messages to DeepSeek with tool definitions
 *   2. Stream text chunks and tool_calls back
 *   3. If tool_calls present → execute via ToolExecutor → append results → repeat
 *   4. If no tool_calls → yield done
 */
export class AgentRuntime {
  private config: AgentRuntimeConfig;

  constructor(config: AgentRuntimeConfig) {
    this.config = config;
  }

  /**
   * Send a message and receive a streaming response with potential tool calls.
   * Handles the full multi-turn tool loop internally.
   *
   * @param userMessage - The text message from the user.
   * @param images      - Optional array of base64 data URLs for multimodal input.
   */
  async *chat(userMessage: string, images?: string[]): AsyncGenerator<StreamEvent> {
    const baseUrl = this.config.baseUrl;
    const model = this.config.model;
    const tools = getHiveWeaveTools(this.config.permissionType, this.config.role);
    const sessionId = this.config.sessionId || globalThis.crypto.randomUUID();

    // Build initial message history with cache-friendly ordering:
    //   [system: static identity] → [system: dynamic context] → [...history] → [user]
    let messages: Message[] = [];

    if (this.config.identityPrompt) {
      // New split-prompt mode: static identity first (maximizes prefix cache)
      messages.push({ role: "system", content: this.config.identityPrompt });
      if (this.config.contextPrompt) {
        messages.push({ role: "system", content: this.config.contextPrompt });
      }
    } else {
      // Legacy mode: single system prompt (backward compat)
      messages.push({ role: "system", content: this.buildSystemPrompt() });
    }

    // Inject conversation history for continuity + cache hits
    if (this.config.history && this.config.history.length > 0) {
      messages.push(...this.config.history);
    }

    // Track where old content ends (system prompts + history) so we only
    // return NEW messages from this call. Must be captured BEFORE pushing
    // the user message below.
    const newMsgStart = messages.length;

    // Current user message (always last before the LLM call)
    const userMsg: Message = { role: "user", content: userMessage };
    if (images && images.length > 0 && this.config.supportsImages) {
      (userMsg as any).images = images;
    }
    messages.push(userMsg);

    let fullText = "";
    let turnCount = 0;
    const recentToolCalls: Array<{ name: string; args: string }> = [];

    while (true) {
      turnCount++;

      // Safety cap — prevents truly infinite loops from bugs
      if (turnCount > MAX_TURNS) {
        console.warn(`[RUNTIME:${this.config.agentName}] Safety cap reached (${MAX_TURNS}). Breaking.`);
        yield { type: "error", content: `Safety cap reached (${MAX_TURNS} turns). This may indicate a bug.` };
        break;
      }

      console.log(`[RUNTIME:${this.config.agentName}] Turn ${turnCount} — calling LLM (${messages.length} msgs, ${tools.length} tools)`);

      // Call LLM API (streaming) — manually iterate to capture return value
      const streamGen = this.callLLMStreaming(messages, tools);
      let streamResult: { text: string; toolCalls: ToolCall[]; ok: boolean } = { text: "", toolCalls: [], ok: false };

      while (true) {
        const next = await streamGen.next();
        if (next.done) {
          streamResult = next.value;
          break;
        }
        yield next.value; // Forward StreamEvents to caller
      }

      if (!streamResult.ok) {
        console.error(`[RUNTIME:${this.config.agentName}] Turn ${turnCount} failed — stopping chat loop`);
        break;
      }

      // result contains: text accumulated, tool_calls (if any)
      fullText += streamResult.text;
      console.log(`[RUNTIME:${this.config.agentName}] Turn ${turnCount} result: text=${streamResult.text.length}chars, toolCalls=${streamResult.toolCalls?.length || 0}`);

      // If no tool calls, we're done — natural exit
      if (!streamResult.toolCalls || streamResult.toolCalls.length === 0) {
        // Push the final assistant text reply so it's included in turn history.
        // Without this, appendTurn stores only the user message and the LLM
        // loses track of its own response on subsequent turns.
        messages.push({
          role: "assistant",
          content: streamResult.text || null,
        });
        console.log(`[RUNTIME:${this.config.agentName}] Loop exit: no tool calls (turn ${turnCount})`);
        break;
      }

      // Append the assistant message with tool_calls to history
      messages.push({
        role: "assistant",
        content: streamResult.text || null,
        tool_calls: streamResult.toolCalls,
      });

      // Execute each tool call
      if (!this.config.toolExecutor) {
        // No executor — report error for each tool and stop
        for (const tc of streamResult.toolCalls) {
          yield {
            type: "error",
            content: `No tool executor available for ${tc.function.name}`,
          };
        }
        break;
      }

      for (const tc of streamResult.toolCalls) {
        let input: Record<string, any> = {};
        try {
          input = JSON.parse(tc.function.arguments || "{}");
        } catch {
          input = {};
        }
        console.log(`[RUNTIME:${this.config.agentName}] Executing tool: ${tc.function.name}`);

        // --- Doom loop detection ---
        const argStr = tc.function.arguments || "";
        recentToolCalls.push({ name: tc.function.name, args: argStr });
        if (recentToolCalls.length > DOOM_LOOP_THRESHOLD) recentToolCalls.shift();

        if (
          recentToolCalls.length === DOOM_LOOP_THRESHOLD &&
          recentToolCalls.every((t) => t.name === recentToolCalls[0].name && t.args === recentToolCalls[0].args)
        ) {
          console.warn(`[RUNTIME:${this.config.agentName}] Doom loop detected: ${recentToolCalls[0].name} called ${DOOM_LOOP_THRESHOLD}x with identical args`);
          yield {
            type: "error",
            content: `Detected repeated tool call (${recentToolCalls[0].name}) ${DOOM_LOOP_THRESHOLD} times with identical arguments. Breaking to prevent infinite loop.`,
          };
          // Push a tool result so the message array is well-formed, then break outer loop
          messages.push({ role: "tool", tool_call_id: tc.id, content: "Doom loop detected — execution halted." });
          break;
        }

        // --- Permission gate ---
        let permission: "allow" | "ask" | "deny" = "allow";
        if (this.config.permissionChecker) {
          permission = await this.config.permissionChecker(
            this.config.agentId,
            tc.function.name,
            input,
          );
        }

        if (permission === "deny") {
          const denial = `Permission denied: ${tc.function.name} is not allowed for this agent.`;
          yield {
            type: "tool_result",
            content: tc.function.name,
            metadata: { result: denial, toolCallId: tc.id, permission: "denied" },
          };
          messages.push({ role: "tool", tool_call_id: tc.id, content: denial });
          continue;
        }

        if (permission === "ask") {
          if (!this.config.approvalHandler || !this.config.approvalWaiter) {
            const denial = `Permission denied: ${tc.function.name} requires approval but no approval handler is configured.`;
            yield {
              type: "tool_result",
              content: tc.function.name,
              metadata: { result: denial, toolCallId: tc.id, permission: "denied" },
            };
            messages.push({ role: "tool", tool_call_id: tc.id, content: denial });
            continue;
          }

          const description = `Agent "${this.config.agentName}" wants to call ${tc.function.name}`;
          const requestId = await this.config.approvalHandler(
            this.config.agentId,
            tc.function.name,
            input,
            description,
          );

          // Notify the client so it can show the approval dialog immediately
          yield {
            type: "approval_request",
            content: tc.function.name,
            metadata: { input, requestId, toolCallId: tc.id },
          };

          // Block until user responds or timeout (5 min)
          const approvalResult = await this.config.approvalWaiter(requestId);

          if (!approvalResult.approved) {
            const reason = approvalResult.timedOut
              ? `Approval timed out for ${tc.function.name}.`
              : `Permission denied by user for ${tc.function.name}.`;
            yield {
              type: "tool_result",
              content: tc.function.name,
              metadata: { result: reason, toolCallId: tc.id, permission: "denied" },
            };
            messages.push({ role: "tool", tool_call_id: tc.id, content: reason });
            continue;
          }
          // Approved — fall through to execution
        }

        // --- Execute tool (permission is "allow" or approval was granted) ---
        yield {
          type: "tool_use",
          content: tc.function.name,
          metadata: { input, toolCallId: tc.id },
        };

        let toolResult: string;
        try {
          toolResult = await this.config.toolExecutor.execute(
            this.config.agentId,
            sessionId,
            tc.function.name,
            input,
          );
          console.log(`[RUNTIME:${this.config.agentName}] Tool ${tc.function.name} OK (${toolResult.length} chars)`);
        } catch (err: any) {
          toolResult = `Tool execution error: ${err.message || "Unknown error"}`;
          console.error(`[RUNTIME:${this.config.agentName}] Tool ${tc.function.name} EXCEPTION:`, err.message);
        }

        yield {
          type: "tool_result",
          content: tc.function.name,
          metadata: { result: toolResult, toolCallId: tc.id },
        };

        // Append tool result to message history (truncated for compact storage)
        messages.push({
          role: "tool",
          tool_call_id: tc.id,
          content: truncateForHistory(toolResult),
        });
      }

      // --- Mid-turn compaction: check for context overflow ---
      if (this.config.compactor && _isOverflow(messages, this.config.contextWindow, this.config.maxOutputTokens || 8192)) {
        console.log(`[RUNTIME:${this.config.agentName}] Context overflow detected — compacting mid-turn`);
        yield { type: "compacting", content: "Compacting context..." };

        const { old, recent } = this.splitMessagesForCompaction(messages);
        if (old.length > 0) {
          const summary = await this.config.compactor(old);
          if (summary) {
            messages = this.rebuildAfterCompaction(messages, summary, recent);
            console.log(`[RUNTIME:${this.config.agentName}] Compacted ${old.length} messages into summary (${recent.length} recent kept)`);
            yield { type: "text", content: "\n[上下文已整理，继续执行...]\n" };
          }
        }
      }

      // Check for queued messages at this natural breakpoint (between tool turns)
      if (this.config.messagePoller) {
        try {
          const queued = await this.config.messagePoller();
          if (queued.length > 0) {
            let queueText = "## Pending Messages Received During Your Work\n";
            queueText += "The following messages arrived while you were working. Please acknowledge them briefly and continue your current task.\n\n";
            for (const q of queued) {
              const label = q.messageType === "peer" ? "Peer" : "Subordinate";
              const reportTag = q.expectReport ? " **[REPLY REQUIRED]**" : "";
              queueText += `- **${label} ${q.fromName}** says${reportTag}: "${q.message}"\n`;
            }
            if (queued.some((q) => q.expectReport)) {
              queueText += "\n> **[SYSTEM]** Some messages above require your reply. You MUST respond to those before finishing.\n";
            }
            messages.push({ role: "user", content: queueText });
            yield {
              type: "queued_message",
              content: `Received ${queued.length} queued message(s)`,
              metadata: { messages: queued },
            };
          }
        } catch {
          // Non-critical: poller failure should not break the agent loop
        }
      }

      // Continue loop — DeepSeek will see tool results and decide next action
    }

    // Only return NEW messages from this call (user msg + assistant reply + tool exchanges)
    // newMsgStart was captured after system prompts + history, before the user message
    const newMessages = messages.slice(newMsgStart);
    yield { type: "done", content: fullText, metadata: { messages: newMessages } };
  }

  // -------------------------------------------------------------------------
  // Private: mid-turn compaction helpers
  // -------------------------------------------------------------------------

  /**
   * Split messages into "old" (to be summarized) and "recent" (to keep verbatim).
   * Preserves system prompts separately and keeps the last 2 user-message boundaries
   * as recent context.
   */
  private splitMessagesForCompaction(messages: Message[]): { old: Message[]; recent: Message[] } {
    // System messages are always preserved (not part of old/recent split)
    const systemEnd = messages.findIndex((m) => m.role !== "system");
    const nonSystem = messages.slice(systemEnd >= 0 ? systemEnd : 0);

    // Find user message boundaries (from the end)
    const userIndices: number[] = [];
    for (let i = 0; i < nonSystem.length; i++) {
      if (nonSystem[i].role === "user") userIndices.push(i);
    }

    // Keep last 2 user-turns as "recent" (or all if fewer than 2)
    const KEEP_TURNS = 2;
    if (userIndices.length <= KEEP_TURNS) {
      // Can't split meaningfully — nothing to compact
      return { old: [], recent: messages };
    }

    const splitPoint = userIndices[userIndices.length - KEEP_TURNS];
    const old = nonSystem.slice(0, splitPoint);
    const recent = nonSystem.slice(splitPoint);

    return { old, recent };
  }

  /**
   * Rebuild the messages array after compaction:
   * [system prompts] + [user: summary] + [assistant: ack] + [recent messages]
   */
  private rebuildAfterCompaction(
    messages: Message[],
    summary: string,
    recent: Message[],
  ): Message[] {
    // Extract system messages (always preserved)
    const systemMsgs = messages.filter((m) => m.role === "system");

    const compactionUser: Message = {
      role: "user",
      content: `[Previous conversation summary — use this as context for continuing your work]\n\n${summary}`,
    };
    const compactionAck: Message = {
      role: "assistant",
      content: "I understand the previous context. I'll continue from where we left off.",
    };

    return [...systemMsgs, compactionUser, compactionAck, ...recent];
  }

  // -------------------------------------------------------------------------
  // Private: streaming API call (AI SDK multi-provider)
  // -------------------------------------------------------------------------

  /**
   * Convert HiveWeave Message[] to AI SDK ModelMessage[] (v6).
   * Handles multimodal images on user messages.
   */
  private toCoreMessages(messages: Message[]): ModelMessage[] {
    return messages.map((m): ModelMessage => {
      if (m.role === "system") {
        return { role: "system", content: m.content };
      }
      if (m.role === "user") {
        // Multimodal: if images are present, convert to content array.
        // AI SDK v6 ImagePart expects `image: DataContent | URL`.
        if (m.images && m.images.length > 0) {
          return {
            role: "user",
            content: [
              { type: "text" as const, text: m.content },
              ...m.images.map((url) => {
                const img: { type: "image"; image: URL } = { type: "image", image: new URL(url) };
                return img;
              }),
            ],
          };
        }
        return { role: "user", content: m.content };
      }
      if (m.role === "assistant") {
        // AI SDK expects tool calls as content parts with `input` (v6).
        if (m.tool_calls && m.tool_calls.length > 0) {
          return {
            role: "assistant",
            content: [
              ...(m.content ? [{ type: "text" as const, text: m.content }] : []),
              ...m.tool_calls.map((tc) => ({
                type: "tool-call" as const,
                toolCallId: tc.id,
                toolName: tc.function.name,
                input: JSON.parse(tc.function.arguments || "{}"),
              })),
            ],
          };
        }
        return { role: "assistant", content: m.content || "" };
      }
      // tool message: v6 uses `output` (not `result`).
      const toolResultParts: ToolResultPart[] = [
        {
          type: "tool-result",
          toolCallId: m.tool_call_id,
          toolName: "",
          output: { type: "text", value: m.content },
        },
      ];
      return { role: "tool", content: toolResultParts };
    });
  }

  /**
   * Convert HiveWeave ChatCompletionTool[] to AI SDK tool() definitions.
   * Tools are defined only — execution stays in HiveWeave's ToolExecutor.
   */
  private toSdkTools(tools: ChatCompletionTool[]): Record<string, ReturnType<typeof tool>> {
    const sdkTools: Record<string, ReturnType<typeof tool>> = {};
    for (const t of tools) {
      sdkTools[t.function.name] = tool({
        description: t.function.description,
        inputSchema: jsonSchema(t.function.parameters as any),
      });
    }
    return sdkTools;
  }

  /**
   * Make a streaming call to the LLM via AI SDK and yield text chunks as they arrive.
   * Returns accumulated tool calls (if any) via the return value.
   * Retries transient errors using classifyHttpError + computeBackoff from retry-utils.
   */
  private async *callLLMStreaming(
    messages: Message[],
    tools: ChatCompletionTool[],
  ): AsyncGenerator<StreamEvent, { text: string; toolCalls: ToolCall[]; ok: boolean }> {
    let lastError = "Unknown error";

    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      let attemptText = "";
      const attemptToolCalls: ToolCall[] = [];

      try {
        // Create provider instance from config
        const providerFactory = createProviderInstance(
          this.config.provider || "openai-compatible",
          this.config.baseUrl,
          this.config.apiKey,
        );
        const sdkModel = providerFactory(this.config.model);

        // Convert messages and tools to AI SDK format
        const coreMessages = this.toCoreMessages(messages);
        const sdkTools = this.toSdkTools(tools);

        // Build provider-specific options (reasoning effort, etc.)
        const providerOptions: Record<string, any> = {};
        if (this.config.reasoningEffort) {
          // OpenAI-compatible: pass as provider metadata
          providerOptions.providerMetadata = {
            [this.config.provider || "openai-compatible"]: {
              reasoning_effort: this.config.reasoningEffort,
            },
          };
        }

        // Call AI SDK streamText
        const result = streamText({
          model: sdkModel,
          messages: coreMessages,
          tools: Object.keys(sdkTools).length > 0 ? sdkTools : undefined,
          maxOutputTokens: this.config.maxOutputTokens,
          temperature: this.config.temperature,
          ...providerOptions,
        });

        // Stream events from AI SDK (v6: text-delta uses `.text`, reasoning via `reasoning-delta`,
        // tool-call parts use `.input`)
        for await (const part of result.fullStream) {
          switch (part.type) {
            case "text-delta":
              attemptText += part.text;
              yield { type: "text", content: part.text };
              break;
            case "reasoning-delta":
              // Thinking/reasoning content (supported by some providers)
              yield { type: "thinking", content: part.text };
              break;
            case "tool-call":
              attemptToolCalls.push({
                id: part.toolCallId,
                type: "function",
                function: {
                  name: part.toolName,
                  arguments: JSON.stringify(part.input),
                },
              });
              break;
            case "error":
              // AI SDK emitted an error event during streaming
              const errStr = String(part.error || "Unknown streaming error");
              console.error(`[RUNTIME:${this.config.agentName}] AI SDK stream error: ${errStr}`);
              // If we have partial results, return them; otherwise retry
              if (attemptText.length > 0 || attemptToolCalls.length > 0) {
                yield { type: "error", content: errStr };
                return { text: attemptText, toolCalls: attemptToolCalls, ok: attemptToolCalls.length > 0 };
              }
              throw new Error(errStr);
            case "finish":
              // v6: tool calls arrive via "tool-call" events only; the finish part
              // carries finishReason/usage but no toolCalls array. Nothing to do here.
              break;
          }
        }

        // Stream completed successfully
        return { text: attemptText, toolCalls: attemptToolCalls, ok: true };
      } catch (err: any) {
        lastError = err.message || "Unknown error";

        // Check if this is a context overflow — not retryable
        if (isContextOverflow(lastError)) {
          console.error(`[RUNTIME:${this.config.agentName}] Context overflow detected: ${lastError}`);
          yield { type: "error", content: `上下文超出模型限制。请减少对话长度或切换到更大上下文的模型。` };
          return { text: "", toolCalls: [], ok: false };
        }

        // Try to extract HTTP status from AI SDK error
        const statusCode = (err as any).statusCode || (err as any).status;
        let classified;
        if (statusCode) {
          const body = (err as any).responseBody || err.message || "";
          const headers = (err as any).responseHeaders || {};
          classified = classifyHttpError(statusCode, body, headers);
        } else {
          classified = classifyNetworkError(err instanceof Error ? err : new Error(lastError));
        }

        // If not retryable or max retries reached, give up
        if (!classified.retryable || attempt >= MAX_RETRIES) {
          console.error(`[RUNTIME:${this.config.agentName}] LLM error (${classified.category}): ${classified.message}`);
          yield { type: "error", content: classified.message };
          return { text: "", toolCalls: [], ok: false };
        }

        // Compute backoff and retry
        const delay = computeBackoff(attempt, classified.retryAfterMs);
        console.warn(`[RUNTIME:${this.config.agentName}] ${classified.category} (retryable), attempt ${attempt + 1}/${MAX_RETRIES + 1}, retrying in ${Math.round(delay)}ms`);
        yield {
          type: "retry",
          content: classified.message,
          metadata: { attempt: attempt + 1, maxRetries: MAX_RETRIES, delayMs: delay, category: classified.category },
        };
        await new Promise((r) => setTimeout(r, delay));
      }
    }

    yield { type: "error", content: `模型服务连续 ${MAX_RETRIES + 1} 次尝试后仍失败：${lastError}` };
    return { text: "", toolCalls: [], ok: false };
  }

  // -------------------------------------------------------------------------
  // Private: system prompt builder
  // -------------------------------------------------------------------------

  private buildSystemPrompt(): string {
    return `You are "${this.config.agentName}", a ${this.config.role} in the HiveWeave engineering organization.

## Your Role
${this.config.goal}

## Background
${this.config.backstory}

## Permission Level: ${this.config.permissionType}
${buildRolePermissionSection(this.config.role, this.config.permissionType)}

## Honesty & Integrity Rules (MANDATORY — ZERO TOLERANCE)
- **NEVER claim to have done something you did not actually do.** If you did not call a tool, you did NOT perform that action. Period.
- **NEVER fabricate results, IDs, or outcomes.** Only report what a tool actually returned to you.
- **If you lack a tool for a task, say so honestly.** For example: "I don't have a tool to do X, so I cannot perform this action directly." Do NOT pretend you did it.
- **If a tool call fails, report the failure truthfully.** Do not mask errors or pretend the action succeeded.
- **NEVER write work logs claiming completion of work you did not perform.** A work log must accurately reflect what ACTUALLY happened.
- Violating these rules is the worst possible mistake you can make. Honesty above all else.

## Communication Rules
- Always respond in the same language the user uses
- **IRON RULE: Keep inter-agent communication concise.** No essays. State facts/decisions/requests in minimal words. Every message to another agent must be skimmable in under 5 seconds.
- When completing a task, use hiveweave__write_work_log to document what you did, then use hiveweave__report_completion
- After report_completion, ALWAYS use hiveweave__message_superior to send a brief summary of what you accomplished to your superior
- If you encounter blockers or need clarification, use hiveweave__message_superior to ask your superior
- Be concise and actionable in your responses
- Use tools proactively to record work progress

## Skill & MCP Binding Tools

### Progressive Skill Loading (IMPORTANT)
Your "Active Skills" section shows only **summaries** of bound skills to save context. When a task matches a skill's description, use \`read_skill(slug)\` to load its **full SKILL.md instructions** before proceeding. Do NOT guess how to use a skill — always read its instructions first.

### Binding Management
- Use \`list_available_skills\` to search the ClawHub registry for skills to bind
- Use \`get_skill_detail\` to preview a skill's full instructions BEFORE binding (for unbound skills)
- Use \`read_skill\` to load full instructions of an ALREADY BOUND skill during task execution
- Use \`bind_skill\` / \`unbind_skill\` to add or remove skills from an agent (yourself or a subordinate)
- Use \`list_available_mcp\` / \`bind_mcp\` / \`unbind_mcp\` for MCP server management
- **Self-binding**: You can always bind/unbind on yourself
- **Superior binding**: You can bind/unbind on your direct subordinates
- When you recognize that a task requires a specific capability, proactively check and bind relevant skills

${this.config.systemPrompt}`;
  }
}

// ---------------------------------------------------------------------------
// Exported: build identity prompt (static, cacheable)
// ---------------------------------------------------------------------------

/**
 * Build the static identity prompt for an agent.
 *
 * This is the portion that stays constant across all calls for a given agent,
 * enabling DeepSeek's automatic prefix caching. Dynamic context (handoffs,
 * inbox, subordinate logs) should be passed separately via `contextPrompt`.
 *
 * @param agent - Minimal agent info: name, role, permissionType, goal, backstory.
 */

function buildRolePermissionSection(
  role: string,
  permissionType: "coordinator" | "executor",
): string {
  const normalizedRole = role.toLowerCase();

  if (normalizedRole === "ceo") {
    return `You are the CEO — the project leader at the top of the organization.

## Your Mission
- **Design and maintain the project charter** (mission, goals, roles, artifact kinds, staffing policy) using \`read_charter\` and \`save_charter\`.
- **Choose organizational paradigms** and overall team structure — reference the paradigm library below.
- **Delegate all staffing to HR** — you do NOT create agents yourself. Message HR (via \`message_peer\`) with hiring requests aligned to the charter.
- **Coordinate business managers** — dispatch tasks, review work, approve/reject deliverables from your subordinates.

## Organizational Paradigm Library
${getParadigmCatalogSummary()}

## What You Do NOT Do
- **No \`create_agent\`** — only HR hires. You set direction; HR executes staffing.
- **No write/run file tools** — use read-only tools (\`list_files\`, \`read_file\`, \`search_files\`, \`glob\`) to understand the project; executors implement changes.
- **No \`message_superior\`** — you are the top of the hierarchy (besides the human user).

## Coordinator Tools (CEO)
- Dispatch tasks, list subordinates, read work logs, review/approve/reject work
- \`list_all_agents\` for full org visibility
- Read-only workspace exploration: \`list_files\`, \`read_file\`, \`search_files\`, \`glob\`, \`fetch_url\`
- Binding tools to assign skills/MCP to yourself or subordinates
- \`write_memory\` and \`read_project_memory\` for long-term context`;
  }

  if (normalizedRole === "hr") {
    return `You are the HR agent — staffing execution and the communication hub under the CEO.

## Your Authority
- **Only you can create, transfer, and dismiss agents** (\`create_agent\`, \`transfer_agent\`, \`dismiss_agent\`).
- You maintain the **Personnel Roster** via \`update_roster\` and \`read_roster\`.
- **Read the project charter** with \`read_charter\`. If a staffing request conflicts with the charter, report to the CEO via \`message_superior\` before proceeding.

## Staffing Execution
- When managers or the CEO need hires, they message you (\`message_peer\` or \`message_superior\`). You evaluate and execute.
- Members who need more people should \`message_peer\` to HR — you are the hiring desk.
- Use \`browse_templates\` / \`create_from_template\` for catalog-based recruitment.

### IRON RULE — HR NEVER has children
**Never set \`parentId\` to your own ID.** You are a personnel service role, not an org manager.

### Placement Rules
- Default new agents under the **CEO** when no parent is specified.
- Place under the **CEO** or the requesting **business manager** (coordinator) — not at root unless the CEO explicitly directs.
- Never parent new agents under yourself.

## Paradigm Reference (for staffing conversations)
${getParadigmCatalogSummary()}

## What You Do NOT Do
- **No file/code tools** — you staff the team; executors write code.
- **No dispatch/review/approve** — those are business coordinator tools, not HR.`;
  }

  if (permissionType === "coordinator") {
    return `You are a COORDINATOR. You can:
- Read work logs of your subordinates using hiveweave__read_work_logs
- Dispatch tasks to subordinates using hiveweave__dispatch_task
- Review subordinate work using hiveweave__review_code
- Approve or reject subordinate work using hiveweave__approve_work / hiveweave__reject_work
- Trigger integration tests using hiveweave__trigger_integration
You CANNOT write code or run shell commands directly. Focus on coordination and oversight.

## Need more people?
Use \`message_peer\` to contact **HR** with the role you need, parent manager, and charter alignment. Only HR can create agents.

## Task Dispatch Guidelines
- When dispatching tasks, use the \`expectReport\` parameter wisely:
  - Set \`expectReport: true\` ONLY when you need results reported back (e.g., information queries, research results, answers to questions)
  - Leave \`expectReport: false\` (default) for fire-and-forget tasks (e.g., code changes, refactoring, writing tests)
- If YOUR task has expectReport=true, you should propagate this by setting expectReport=true when dispatching to subordinates
- When you receive results from a subordinate for a task that requires reporting up, use hiveweave__message_superior to relay the results

## Review & Approval Workflow
- When subordinates report completion, review their work using hiveweave__review_code
- If work is satisfactory: call hiveweave__approve_work to officially mark it done
- If work needs revision: call hiveweave__reject_work with specific, actionable feedback — the subordinate will be automatically re-triggered to rework
- You can approve/reject multiple subordinates\' work before reporting up to your own superior
- Always provide clear, constructive feedback when rejecting work`;
  }

  return `You are an EXECUTOR (leaf agent). You can:
- Write work logs using hiveweave__write_work_log to document your progress
- Report task completion using hiveweave__report_completion
- Send messages to your superior using hiveweave__message_superior when you need clarification, encounter blockers, or want to report important progress
- Read shared project memory using hiveweave__read_project_memory
Focus on implementation and reporting your work accurately.

## Need more people?
Use \`message_peer\` to contact **HR** with the role you need and who should be the parent manager. Only HR can create agents.`;
}

export function buildIdentityPrompt(agent: {
  agentName: string;
  role: string;
  permissionType: "coordinator" | "executor";
  goal: string;
  backstory: string;
}): string {
  return `You are "${agent.agentName}", a ${agent.role} in the HiveWeave engineering organization.

## Your Role
${agent.goal}

## Background
${agent.backstory}

## Permission Level: ${agent.permissionType}
${buildRolePermissionSection(agent.role, agent.permissionType)}

## Honesty & Integrity Rules (MANDATORY — ZERO TOLERANCE)
- **NEVER claim to have done something you did not actually do.** If you did not call a tool, you did NOT perform that action. Period.
- **NEVER fabricate results, IDs, or outcomes.** Only report what a tool actually returned to you.
- **If you lack a tool for a task, say so honestly.** For example: "I don't have a tool to do X, so I cannot perform this action directly." Do NOT pretend you did it.
- **If a tool call fails, report the failure truthfully.** Do not mask errors or pretend the action succeeded.
- **NEVER write work logs claiming completion of work you did not perform.** A work log must accurately reflect what ACTUALLY happened.
- Violating these rules is the worst possible mistake you can make. Honesty above all else.

## Communication Rules
- Always respond in the same language the user uses
- **IRON RULE: Keep inter-agent communication concise.** No essays. State facts/decisions/requests in minimal words. Every message to another agent must be skimmable in under 5 seconds.
- When completing a task, use hiveweave__write_work_log to document what you did, then use hiveweave__report_completion
- After report_completion, ALWAYS use hiveweave__message_superior to send a brief summary of what you accomplished to your superior
- If you encounter blockers or need clarification, use hiveweave__message_superior to ask your superior
- Be concise and actionable in your responses
- Use tools proactively to record work progress

## Skill & MCP Binding Tools

### Progressive Skill Loading (IMPORTANT)
Your "Active Skills" section shows only **summaries** of bound skills to save context. When a task matches a skill's description, use \`read_skill(slug)\` to load its **full SKILL.md instructions** before proceeding. Do NOT guess how to use a skill — always read its instructions first.

### Binding Management
- Use \`list_available_skills\` to search the ClawHub registry for skills to bind
- Use \`get_skill_detail\` to preview a skill's full instructions BEFORE binding (for unbound skills)
- Use \`read_skill\` to load full instructions of an ALREADY BOUND skill during task execution
- Use \`bind_skill\` / \`unbind_skill\` to add or remove skills from an agent (yourself or a subordinate)
- Use \`list_available_mcp\` / \`bind_mcp\` / \`unbind_mcp\` for MCP server management
- **Self-binding**: You can always bind/unbind on yourself
- **Superior binding**: You can bind/unbind on your direct subordinates
- When you recognize that a task requires a specific capability, proactively check and bind relevant skills`;
}
