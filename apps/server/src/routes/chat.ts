import type { FastifyInstance, FastifyReply } from "fastify";
import { randomUUID } from "crypto";
import { OrgService, MemoryService, DispatchService } from "@hiveweave/core";
import { AgentRuntime } from "@hiveweave/agent-runtime";
import type { AgentRuntimeConfig, StreamEvent } from "@hiveweave/agent-runtime";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const ChatBody = z.object({
  agentId: z.string().uuid(),
  message: z.string().min(1),
  sessionId: z.string().optional(),
});

// ---------------------------------------------------------------------------
// SSE helpers
// ---------------------------------------------------------------------------

/** Write a single SSE event to the raw response stream. */
function writeSSE(reply: FastifyReply, event: string, data: unknown): void {
  const payload = typeof data === "string" ? data : JSON.stringify(data);
  const dataLines = payload.split("\n").map((l) => `data: ${l}`).join("\n");
  reply.raw.write(`event: ${event}\n${dataLines}\n\n`);
}

/** Set the required SSE response headers. */
function setSSEHeaders(reply: FastifyReply): void {
  reply.raw.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
  });
}

// ---------------------------------------------------------------------------
// Mock streaming (used when ANTHROPIC_API_KEY is not set)
// ---------------------------------------------------------------------------

async function* mockCoordinatorStream(
  agentName: string,
  message: string,
  children: Array<{ id: string; name: string }>,
): AsyncGenerator<StreamEvent> {
  // Simulate thinking delay
  await new Promise((r) => setTimeout(r, 300));

  if (children.length === 0) {
    yield { type: "text", content: `I'm ${agentName}. I received your message: "${message}". However, I have no subordinates to dispatch work to yet.` };
    yield { type: "done", content: "" };
    return;
  }

  const target = children[0];
  const responseText = [
    `I'm **${agentName}** (coordinator). I received your request:`,
    `> ${message}`,
    ``,
    `I'll dispatch this to **${target.name}** (agent ${target.id.slice(0, 8)}...) for execution.`,
  ].join("\n");

  yield { type: "text", content: responseText };
  yield {
    type: "tool_use",
    content: "hiveweave__dispatch_task",
    metadata: { input: { toAgentId: target.id, description: message } },
  };
  yield { type: "text", content: `\n\nDispatch recorded. ${target.name} will pick up this task.` };
  yield { type: "done", content: responseText };
}

async function* mockExecutorStream(
  agentName: string,
  message: string,
): AsyncGenerator<StreamEvent> {
  await new Promise((r) => setTimeout(r, 300));

  const responseText = [
    `I'm **${agentName}** (executor). I'll work on this:`,
    `> ${message}`,
    ``,
    `I've noted the task and will begin implementation. A work log entry has been created to track progress.`,
  ].join("\n");

  yield { type: "text", content: responseText };
  yield {
    type: "tool_use",
    content: "hiveweave__write_work_log",
    metadata: { input: { type: "discussion", summary: `Received task: ${message}` } },
  };
  yield { type: "done", content: responseText };
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export async function chatRoutes(fastify: FastifyInstance) {
  const orgService = new OrgService();
  const memoryService = new MemoryService();
  const dispatchService = new DispatchService();

  const hasApiKey = !!process.env.ANTHROPIC_API_KEY;

  /**
   * POST /chat — Send a message to an agent and stream the response via SSE.
   *
   * SSE protocol:
   *   event: text      data: "chunk of text"
   *   event: tool_use  data: {"tool":"name","input":{...}}
   *   event: error     data: "error message"
   *   event: done      data: ""
   */
  fastify.post("/", async (request, reply) => {
    // --- Validate request body ---
    const parsed = ChatBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({
        error: "Validation failed",
        issues: parsed.error.issues,
      });
    }

    const { agentId, message, sessionId } = parsed.data;
    const session = sessionId || randomUUID();

    // --- Load agent config from DB ---
    const agent = await orgService.getAgent(agentId);
    if (!agent) {
      return reply.status(404).send({ error: "Agent not found" });
    }

    // --- Build context (project memories + private memories) ---
    let contextBlock = "";
    try {
      contextBlock = await memoryService.buildAgentContext(agentId, agent.moduleId || undefined);
    } catch (err: any) {
      fastify.log.warn(err, "Failed to build agent context, proceeding without");
    }

    // --- If coordinator: load subordinate work logs (log-reading protocol) ---
    let subordinateLogsBlock = "";
    if (agent.permissionType === "coordinator") {
      try {
        const children = await orgService.getChildren(agentId);
        for (const child of children) {
          const logs = await dispatchService.getSubordinateLogs(child.id, 5);
          if (logs.length > 0) {
            subordinateLogsBlock += `\n### Recent logs from ${child.name} (${child.role}):\n`;
            for (const log of logs) {
              subordinateLogsBlock += `- [${log.type}] ${log.summary}\n`;
            }
          }
        }
      } catch (err: any) {
        fastify.log.warn(err, "Failed to load subordinate logs");
      }
    }

    // --- Set SSE headers ---
    setSSEHeaders(reply);

    // --- Mock mode (no API key) ---
    if (!hasApiKey) {
      fastify.log.info("ANTHROPIC_API_KEY not set — using mock streaming");

      let stream: AsyncGenerator<StreamEvent>;

      if (agent.permissionType === "coordinator") {
        const children = await orgService.getChildren(agentId);
        stream = mockCoordinatorStream(
          agent.name,
          message,
          children.map((c: any) => ({ id: c.id, name: c.name })),
        );

        // Create a dispatch log for the coordinator
        if (children.length > 0) {
          try {
            await dispatchService.dispatchTask({
              fromAgentId: agentId,
              toAgentId: children[0].id,
              description: message,
              sessionId: session,
            });
          } catch (err: any) {
            fastify.log.warn(err, "Failed to create mock dispatch log");
          }
        }
      } else {
        stream = mockExecutorStream(agent.name, message);

        // Create a work log for the executor
        try {
          await dispatchService.writeWorkLog({
            agentId,
            sessionId: session,
            type: "discussion",
            summary: `Received task: ${message}`,
            details: { source: "mock" },
          });
        } catch (err: any) {
          fastify.log.warn(err, "Failed to create mock work log");
        }
      }

      for await (const event of stream) {
        switch (event.type) {
          case "text":
            writeSSE(reply, "text", event.content);
            break;
          case "tool_use":
            writeSSE(reply, "tool_use", {
              tool: event.content,
              input: event.metadata?.input || {},
            });
            break;
          case "error":
            writeSSE(reply, "error", event.content);
            break;
          case "done":
            writeSSE(reply, "done", "");
            break;
        }
      }

      reply.raw.end();
      return reply;
    }

    // --- Real mode (Anthropic API) ---
    const systemPrompt = [
      contextBlock,
      subordinateLogsBlock ? `\n## Subordinate Work Logs\n${subordinateLogsBlock}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");

    const runtimeConfig: AgentRuntimeConfig = {
      agentId,
      agentName: agent.name,
      role: agent.role,
      permissionType: agent.permissionType as "coordinator" | "executor",
      goal: agent.goal,
      backstory: agent.backstory,
      systemPrompt,
      workDir: process.env.HIVEWEAVE_WORK_DIR || process.cwd(),
    };

    const runtime = new AgentRuntime(runtimeConfig);

    try {
      for await (const event of runtime.chat(message)) {
        switch (event.type) {
          case "text":
            writeSSE(reply, "text", event.content);
            break;
          case "tool_use":
            writeSSE(reply, "tool_use", {
              tool: event.content,
              input: event.metadata?.input || {},
            });
            break;
          case "tool_result":
            // Tool results are internal — skip SSE emission for now
            break;
          case "error":
            writeSSE(reply, "error", event.content);
            break;
          case "done":
            writeSSE(reply, "done", "");
            break;
        }
      }
    } catch (error: any) {
      fastify.log.error(error, "Streaming error");
      writeSSE(reply, "error", error.message || "Stream failed");
    }

    reply.raw.end();
    return reply;
  });
}
