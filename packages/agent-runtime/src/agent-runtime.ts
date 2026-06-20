import { query } from "@anthropic-ai/claude-code";
import { getToolsForPermissionType } from "./permissions.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Configuration for creating an AgentRuntime instance */
export interface AgentRuntimeConfig {
  agentId: string;
  agentName: string;
  role: string;
  permissionType: "coordinator" | "executor";
  goal: string;
  backstory: string;
  systemPrompt: string;
  workDir: string;
}

/** A single event emitted during an agent chat stream */
export interface StreamEvent {
  type: "text" | "tool_use" | "tool_result" | "error" | "done";
  content: string;
  metadata?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// AgentRuntime
// ---------------------------------------------------------------------------

/**
 * Wraps the Claude Code SDK `query()` function with HiveWeave's permission
 * matrix. Each instance represents a single agent with a fixed role and
 * permission level (coordinator or executor).
 */
export class AgentRuntime {
  private config: AgentRuntimeConfig;

  constructor(config: AgentRuntimeConfig) {
    this.config = config;
  }

  /**
   * Send a message to this agent and receive a streaming response.
   *
   * @param message - The user message to send.
   * @param conversationHistory - Optional prior conversation turns.
   * @yields StreamEvent objects as they arrive from the SDK.
   */
  async *chat(
    message: string,
    conversationHistory: Array<{ role: string; content: string }> = [],
  ): AsyncGenerator<StreamEvent> {
    const allowedTools = getToolsForPermissionType(this.config.permissionType);

    // Build the full system prompt with agent context
    const systemPrompt = this.buildSystemPrompt();

    try {
      // Use Claude Code SDK's query function
      const result = query({
        prompt: message,
        options: {
          allowedTools,
          systemPrompt,
          cwd: this.config.workDir,
          maxTurns: this.config.permissionType === "coordinator" ? 5 : 15,
          // Coordinator agents get stricter permission prompts;
          // executor agents auto-accept file edits to reduce friction.
          permissionMode:
            this.config.permissionType === "coordinator"
              ? "default"
              : "acceptEdits",
        },
      });

      let fullText = "";
      for await (const event of result) {
        if (event.type === "assistant" && "content" in event) {
          const msg = event as any;
          for (const block of msg.content || []) {
            if (block.type === "text") {
              fullText += block.text;
              yield { type: "text", content: block.text };
            } else if (block.type === "tool_use") {
              yield {
                type: "tool_use",
                content: block.name,
                metadata: { input: block.input },
              };
            }
          }
        } else if (event.type === "result") {
          yield { type: "done", content: fullText };
        }
      }
    } catch (error: any) {
      yield { type: "error", content: error.message || "Unknown error" };
    }
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  /**
   * Compose the full system prompt by combining the agent's identity,
   * permission-level instructions, and any additional system prompt text.
   */
  private buildSystemPrompt(): string {
    return `You are "${this.config.agentName}", a ${this.config.role} in the HiveWeave engineering organization.

## Your Role
${this.config.goal}

## Background
${this.config.backstory}

## Permission Level: ${this.config.permissionType}
${
  this.config.permissionType === "coordinator"
    ? `You are a COORDINATOR. You can:
- Read code and work logs of your subordinates
- Dispatch tasks to subordinates using hiveweave__dispatch_task
- Review and approve/reject subordinate work
- Trigger integration tests
You CANNOT write code or run shell commands directly.`
    : `You are an EXECUTOR (leaf agent). You can:
- Read and write code files
- Run tests and shell commands
- Write work logs using hiveweave__write_work_log
- Report task completion using hiveweave__report_completion
You CANNOT spawn sub-agents or access other agents' private memory.`
}

## Communication Rules
- Always respond in the same language the user uses
- When completing a task, use hiveweave__write_work_log to document what you did
- When reporting results, use hiveweave__report_completion with a clear summary

${this.config.systemPrompt}`;
  }
}
