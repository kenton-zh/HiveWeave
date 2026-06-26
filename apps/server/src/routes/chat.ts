import type { FastifyInstance, FastifyReply } from "fastify";
import { randomUUID } from "crypto";
import { OrgService, MemoryService, DispatchService, ToolExecutor, HandoffService, InboxService, ChatMessageService, TeamChatService, RosterService, FileService, ProjectService, communicationService, userPingTracker, drainQuestions, answerQuestion, getTodos, conversationStore, clawhubService, calculateHistoryBudget, estimateTokens, computePrefixHash, buildCompactionPrompt, statusEventBus, TemplateService, PermissionService, ApprovalService, waitForApprovalResponse, cancelApprovalWait, ModelService, ShellService, WebService, getGameTimeService, AlarmService, buildTimeContextBlock, prefixTriggerMessage, SettingsService, formatGoalsForPrompt, TOOL_OUTPUT_MAX_CHARS_COMPACTION } from "@hiveweave/core";
import type { ReviewLLMCallback } from "@hiveweave/core";
import type { StoredMessage } from "@hiveweave/core";
import { AgentRuntime, buildIdentityPrompt, createProviderInstance, ToolOutputStore } from "@hiveweave/agent-runtime";
import type { AgentRuntimeConfig, StreamEvent, ToolExecutorCallback, Message, QueuedMessage, MessagePoller } from "@hiveweave/agent-runtime";
import { generateText } from "ai";
import { z } from "zod";
import { db, lookupAgentWorkspace, ensureProjectDb, getProjectDbForAgent, agents } from "@hiveweave/db";
import { eq } from "drizzle-orm";
import { formatCharterForPrompt } from "@hiveweave/shared";
import { registerAlarmTrigger } from "../game-time-scheduler.js";

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/** Cached operator name — refreshed per request, not module-scoped. */
async function getOperatorName(): Promise<string> {
  const svc = new SettingsService(db);
  return svc.get("operatorName");
}

const ChatBody = z.object({
  agentId: z.string().uuid(),
  message: z.string().min(1),
  sessionId: z.string().optional(),
  images: z.array(z.string()).optional(),
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
// Model resolution — look up agent's configured model from the registry
// ---------------------------------------------------------------------------

interface ResolvedModel {
  baseUrl: string;
  modelId: string;
  modelName: string;
  apiKey: string;
  provider: string;
  supportsImages: boolean;
  contextWindow: number;
  maxOutputTokens: number;
  /** Explicit input token limit from model (model.limit.input). If set, used instead of context for overflow. */
  limitInput?: number;
  /** Whether this provider respects Anthropic-style inline cache hints. */
  respectsInlineCacheHints?: boolean;
  temperature?: number;
  reasoningEffort?: string;
}

const modelService = new ModelService(db);

async function resolveModelForAgent(agentId: string): Promise<ResolvedModel> {
  const projectDb = getProjectDbForAgent(agentId);
  let agentModelId: string | null = null;
  let agentReasoningEffort: string | null = null;
  let agentRole = "";
  let agentPermissionType = "";

  if (projectDb) {
    const rows = await projectDb
      .select({ modelId: agents.modelId, reasoningEffort: agents.reasoningEffort, role: agents.role, permissionType: agents.permissionType })
      .from(agents)
      .where(eq(agents.id, agentId));
    if (rows.length > 0) {
      agentModelId = rows[0].modelId;
      agentReasoningEffort = rows[0].reasoningEffort;
      agentRole = rows[0].role || "";
      agentPermissionType = rows[0].permissionType || "";
    }
  }

  // 1. Try agent's specified model
  let model = agentModelId ? await modelService.getById(agentModelId) : null;
  // 2. Permission-based auto-selection
  if (!model) {
    const isCoordinator = agentPermissionType === "coordinator";
    if (isCoordinator) {
      // Management (CEO, HR, managers): DeepSeek via OpenCode Go
      model = await modelService.findActiveByModelId("deepseek-v4-flash") || await modelService.getDefault();
    } else {
      // Leaf executors (developers, QA): Step 3.7 Flash
      model = await modelService.findActiveByModelId("step-3.7-flash") || await modelService.getDefault();
    }
  }
  // 3. No models configured at all
  if (!model) throw new Error("No LLM model configured. Please add a model in Settings.");

  return {
    baseUrl: model.baseUrl,
    modelId: model.modelId,
    modelName: model.name,
    apiKey: model.apiKey,
    provider: model.provider,
    supportsImages: model.supportsImages,
    contextWindow: model.contextWindow,
    maxOutputTokens: model.maxOutputTokens,
    limitInput: (model as any).limitInput,
    respectsInlineCacheHints: (model as any).respectsInlineCacheHints ?? (model.provider === "anthropic" || model.provider === "bedrock"),
    temperature: model.temperature ? parseFloat(model.temperature) : undefined,
    reasoningEffort: agentReasoningEffort || model.defaultReasoningEffort || undefined,
  };
}

// ---------------------------------------------------------------------------
// Mock streaming (used when no model is configured)
// ---------------------------------------------------------------------------

async function* mockCoordinatorStream(
  agentName: string,
  message: string,
  children: Array<{ id: string; name: string }>,
): AsyncGenerator<StreamEvent> {
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
  const projectService = new ProjectService(db);
  const templateService = new TemplateService(db);

  // Shared tool output store — enables truncation-to-file for large tool outputs.
  // Full outputs are saved to temp dir; agent sees a preview + file path hint.
  const toolOutputStore = new ToolOutputStore();
  fastify.addHook("onClose", () => toolOutputStore.destroy());

  /** Create chat + team chat services sharing one ChatMessageService instance. */
  function createChatServices(projectDb: ReturnType<typeof ensureProjectDb>) {
    const chatMessageService = new ChatMessageService(projectDb);
    return {
      chatMessageService,
      teamChatService: new TeamChatService(chatMessageService),
    };
  }

  /** Helper to resolve an agent's project and create per-project services. */
  async function getProjectServices(agentId: string) {
    // First, find which project this agent belongs to by scanning project DBs
    const allProjects = await projectService.listProjects();
    for (const proj of allProjects) {
      if (!proj.workspacePath) continue;
      try {
        const projectDb = ensureProjectDb(proj.workspacePath);
        const orgService = new OrgService(projectDb, proj.workspacePath);
        const agent = await orgService.getAgent(agentId);
        if (agent) {
          return {
            project: proj,
            projectDb,
            orgService,
            memoryService: new MemoryService(projectDb),
            dispatchService: new DispatchService(projectDb),
            handoffService: new HandoffService(projectDb),
            inboxService: new InboxService(projectDb),
            ...createChatServices(projectDb),
            rosterService: new RosterService(projectDb),
            fileService: new FileService(),
            permissionService: new PermissionService(projectDb),
            approvalService: new ApprovalService(projectDb),
          };
        }
      } catch {
        continue;
      }
    }
    return null;
  }


  const gameTimeService = getGameTimeService(db);

  type ProjectServices = NonNullable<Awaited<ReturnType<typeof getProjectServices>>>;

  function makeToolExecutor(services: ProjectServices, projectId?: string | null, resolvedModel?: ResolvedModel): ToolExecutor {
    const alarmService = new AlarmService(services.projectDb);

    // Build review LLM callback from resolved model (only needed for Reviewer agent)
    let reviewLLM: ReviewLLMCallback | undefined;
    if (resolvedModel) {
      const provider = createProviderInstance(resolvedModel.provider, resolvedModel.baseUrl, resolvedModel.apiKey);
      reviewLLM = async (systemPrompt: string, userPrompt: string): Promise<string> => {
        const { text } = await generateText({
          model: provider(resolvedModel.modelId),
          system: systemPrompt,
          prompt: userPrompt,
          temperature: 0.1, // low temperature for consistent review quality
          maxOutputTokens: 4096,
        });
        return text;
      };
    }

    return new ToolExecutor(
      services.dispatchService,
      services.memoryService,
      services.orgService,
      services.handoffService,
      services.inboxService,
      services.rosterService,
      services.fileService,
      projectService,
      templateService,
      new ShellService(),
      new WebService(),
      services.teamChatService,
      gameTimeService,
      alarmService,
      projectId ?? null,
      reviewLLM,
    );
  }

  async function buildTimeBlock(projectId: string | null | undefined): Promise<string> {
    if (!projectId) return "";
    await gameTimeService.initProject(projectId);
    return buildTimeContextBlock(gameTimeService.getSnapshot(projectId));
  }

  function prefixTriggerForProject(projectId: string | null | undefined, message: string): string {
    if (!projectId) return message;
    return prefixTriggerMessage(gameTimeService.getSnapshot(projectId), message);
  }

  /** Build a workspace info block for agent context prompts. */
  async function buildWorkspaceBlock(projectId: string | null, agentRole?: string): Promise<string> {
    if (!projectId) return "";
    try {
      const project = await projectService.getProject(projectId);
      if (project?.workspacePath) {
        const base = `## Workspace\nYour project workspace is at: \`${project.workspacePath}\`\nAll file paths are relative to the workspace root.`;
        const role = agentRole?.toLowerCase();
        if (role === "ceo") {
          return `${base}\nYou have filesystem and shell access: use list_files, read_file, search_files, glob, bash, and run_command to inspect and manage the workspace. Do not write or edit files directly — delegate code changes to executors. Do not use MCP tools for filesystem operations.\n\n**IMPORTANT:** \`.hiveweave\` is the HiveWeave system directory — never touch it.`;
        }
        if (role === "hr") {
          return `${base}\nHR does not edit project files; use org and personnel tools instead of write_file, edit_file, or delete_file.`;
        }
        return `${base}\nUse the file tools (read_file, write_file, edit_file, list_files, search_files, delete_file) to read and write files in this directory.\nIMPORTANT: Always read a file before editing it. The write tool will reject overwrites if the file changed since your last read.`;
      }
    } catch { /* proceed without */ }
    return "";
  }

  /** Build a project info block for agent context prompts — name, description, paradigm. */
  async function buildProjectBlock(projectId: string | null): Promise<string> {
    if (!projectId) return "";
    try {
      const project = await projectService.getProject(projectId);
      if (!project) return "";

      const parts: string[] = [`- 项目名称: ${project.name}`];

      if (project.description) {
        parts.push(`- 项目描述: ${project.description}`);
      } else {
        parts.push("- 项目描述: （用户未提供描述，请通过对话了解项目内容）");
      }

      if (project.orgParadigm) {
        const { getParadigmById } = await import("@hiveweave/shared");
        const paradigm = getParadigmById(project.orgParadigm);
        if (paradigm) {
          parts.push(`- 组织范式: ${paradigm.name} (${paradigm.englishName}) — ${paradigm.description}`);
        } else {
          parts.push(`- 组织范式: ${project.orgParadigm}`);
        }
      } else {
        parts.push("- 组织范式: （尚未选定；CEO 负责选定组织范式并维护章程，HR 负责招聘与编制执行）");
      }

      return `## Project\n${parts.join("\n")}`;
    } catch { /* proceed without */ }
    return "";
  }



  async function buildCharterBlock(projectId: string | null): Promise<string> {
    if (!projectId) return "";
    try {
      const charter = await projectService.getCharter(projectId);
      return formatCharterForPrompt(charter);
    } catch {
      return "";
    }
  }

  async function buildGoalsBlock(projectId: string | null): Promise<string> {
    if (!projectId) return "";
    try {
      const goals = await projectService.getGoals(projectId);
      return formatGoalsForPrompt(goals);
    } catch {
      return "";
    }
  }

  /** Tell the CEO whether this is a fresh project or an established one. */
  async function buildProjectStatusBlock(projectId: string | null, role: string): Promise<string> {
    if (!projectId || role.toLowerCase() !== "ceo") return "";
    try {
      const project = await projectService.getProject(projectId);
      if (!project) return "";
      const ageMs = Date.now() - project.createdAt;
      const ageMinutes = Math.floor(ageMs / 60000);
      if (ageMinutes < 10) {
        return `## Project Status\n🆕 This project was just created. No team exists yet — only you (CEO), HR, and QA. The user will guide you on the next steps.`;
      }
      return `## Project Status\nProject created ${ageMinutes} min ago. Use \`read_roster\` or \`list_subordinates\` to see current team size.`;
    } catch {
      return "";
    }
  }

  // --- Structured Expert Command Parsing ---

  interface ExpertCommand {
    command: string;
    role: string;
    module: string;
  }

  const EXPERT_COMMAND_MAP: Record<string, string> = {
    review: "code_reviewer",
    test: "test_engineer",
    audit: "security_auditor",
    perf: "web_perf_auditor",
  };

  function parseExpertCommand(message: string): ExpertCommand | null {
    const trimmed = message.trim();
    // Match: /review <module>, /test <module>, /audit <module>, /perf <module>
    const match = trimmed.match(/^\/(review|test|audit|perf)\s+(.+)$/i);
    if (!match) return null;
    return {
      command: match[1].toLowerCase(),
      role: EXPERT_COMMAND_MAP[match[1].toLowerCase()],
      module: match[2].trim(),
    };
  }

    // --- Smart compaction: LLM summarizes old history (OpenCode-style) ---
  // Incremental summaries are managed by conversationStore as the single source of truth.

  /** Serialize messages for compaction transcript (aligned with OpenCode's TOOL_OUTPUT_MAX_CHARS=2000). */
  function serializeForCompaction(messages: StoredMessage[] | Message[]): string {
    return messages.map((m) => {
      const msg = m as any;
      if (msg.role === "user") return `[User]: ${typeof msg.content === "string" ? msg.content : ""}`;
      if (msg.role === "assistant") {
        let t = msg.content || "";
        if (msg.tool_calls) t += ` (${msg.tool_calls.map((tc: any) => tc.function?.name || tc.toolName || "").join(", ")})`;
        return `[Assistant]: ${t}`;
      }
      if (msg.role === "tool") return `[Tool]: ${(msg.content || "").slice(0, TOOL_OUTPUT_MAX_CHARS_COMPACTION)}`;
      if (msg.role === "system") return `[System]: ${msg.content}`;
      return "";
    }).filter(Boolean).join("\n");
  }

  // Configure the conversation store for stored-message compaction
  conversationStore.configure({
    compactor: async (oldMessages: StoredMessage[]): Promise<string | null> => {
      let model;
      try { model = await modelService.getDefault(); } catch { return null; }
      if (!model) return null;
      const transcript = serializeForCompaction(oldMessages);
      try {
        const providerFactory = createProviderInstance(model.provider, model.baseUrl, model.apiKey);
        const { text } = await generateText({
          model: providerFactory(model.modelId),
          messages: [{ role: "user", content: buildCompactionPrompt(transcript) }],
          maxOutputTokens: 4096,
          temperature: 0.3,
        });
        return text || null;
      } catch {
        return null;
      }
    },
  });

  /**
   * Mid-turn compactor for AgentRuntime — summarizes in-memory messages when context overflows.
   * Uses conversationStore for incremental summaries (single source of truth).
   */
  const makeMidTurnCompactor = (resolved?: ResolvedModel, agentId?: string, projectDb?: ReturnType<typeof ensureProjectDb>) => async (oldMessages: Message[]): Promise<string | null> => {
    let baseUrl: string;
    let apiKey: string;
    let modelId: string;
    let provider: string;
    const agentKey = agentId || "unknown";

    if (resolved) {
      baseUrl = resolved.baseUrl;
      apiKey = resolved.apiKey;
      modelId = resolved.modelId;
      provider = resolved.provider;
    } else {
      try {
        const model = await modelService.getDefault();
        if (!model) return null;
        baseUrl = model.baseUrl;
        apiKey = model.apiKey;
        modelId = model.modelId;
        provider = model.provider;
      } catch { return null; }
    }

    // Read previous summary from conversationStore (single source of truth)
    const previousSummary = conversationStore.getCompactedPrefix(agentKey);
    const transcript = serializeForCompaction(oldMessages);

    try {
      const providerFactory = createProviderInstance(provider, baseUrl, apiKey);
      const { text } = await generateText({
        model: providerFactory(modelId),
        messages: [{ role: "user", content: buildCompactionPrompt(transcript, previousSummary) }],
        maxOutputTokens: 4096,
        temperature: 0.3,
      });
      const summary = text || null;
      // Persist to conversationStore for incremental summaries (single source of truth)
      if (summary && projectDb) {
        await conversationStore.setCompactedPrefix(agentKey, summary, projectDb);
      }
      return summary;
    } catch {
      return null;
    }
  };

  // Prefix hash drift detection — tracks changes to the static prompt portion
  const prefixHashTracker = new Map<string, string>();

  // Track agents currently running auto-trigger to prevent concurrent execution
  const runningAutoTriggers = new Set<string>();

  /**
   * POST /chat — Send a message to an agent and stream the response via SSE.
   *
   * SSE protocol:
   *   event: text         data: "chunk of text"
   *   event: tool_use     data: {"tool":"name","input":{...}}
   *   event: tool_result  data: {"tool":"name","result":"..."}
   *   event: error        data: "error message"
   *   event: done         data: ""
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

    const { agentId, message, sessionId, images } = parsed.data;
    const session = sessionId || randomUUID();

    if (statusEventBus.isPaused) {
      return reply.status(503).send({
        error: "HiveWeave is paused (下班模式). Click 上班 to resume.",
        code: "PAUSED",
      });
    }

    if (statusEventBus.isProcessing(agentId)) {
      return reply.status(409).send({
        error: "Agent is busy processing a previous message",
        code: "AGENT_BUSY",
      });
    }

    // --- Resolve agent via registry (fast path) or scan all projects ---
    let services: NonNullable<Awaited<ReturnType<typeof getProjectServices>>> | null = null;
    const wsPath = lookupAgentWorkspace(agentId);
    if (wsPath) {
      // Fast path: agent is in the registry — build services directly
      const allProjects = await projectService.listProjects();
      const project = allProjects.find((p) => p.workspacePath === wsPath);
      if (project) {
        const projectDb = ensureProjectDb(wsPath);
        services = {
          project,
          projectDb,
          orgService: new OrgService(projectDb, wsPath),
          memoryService: new MemoryService(projectDb),
          dispatchService: new DispatchService(projectDb),
          handoffService: new HandoffService(projectDb),
          inboxService: new InboxService(projectDb),
          ...createChatServices(projectDb),
          rosterService: new RosterService(projectDb),
          fileService: new FileService(),
          permissionService: new PermissionService(projectDb),
          approvalService: new ApprovalService(projectDb),
        };
      }
    }
    if (!services) {
      services = await getProjectServices(agentId);
    }
    if (!services) {
      return reply.status(404).send({ error: "Agent not found in any project" });
    }

    // --- Load agent config from per-project DB ---
    const agent = await services.orgService.getAgent(agentId);
    if (!agent) {
      return reply.status(404).send({ error: "Agent not found" });
    }

    // --- Pre-generate message IDs for client sync ---
    const userMessageId = randomUUID();
    const assistantMessageId = randomUUID();

    // --- Persist user message ---
    await services.chatMessageService.saveMessage({
      id: userMessageId,
      agentId,
      role: "user",
      content: message,
      images: images?.length ? JSON.stringify(images) : undefined,
      isBackground: false,
      isRead: true,
      createdAt: Date.now(),
    });

    await services.chatMessageService.saveMessage({
      id: assistantMessageId,
      agentId,
      role: "assistant",
      content: "",
      toolCalls: "[]",
      isBackground: false,
      isRead: true,
      isStreaming: true,
      createdAt: Date.now(),
    });

    // Mark agent as processing — entire flow wrapped in try/finally for guaranteed cleanup
    statusEventBus.setProcessing(agentId, true);
    try {

    // --- Structured Expert Command Routing ---
    // Intercept /review, /test, /audit, /perf commands and redirect message to the expert agent.
    // The expert's normal message flow handles queue, context, and streaming.
    const expertCmd = parseExpertCommand(message);
    if (expertCmd && agent.projectId) {
      try {
        const expertAgent = await services.orgService.findAgentByRole(agent.projectId, expertCmd.role);
        if (expertAgent) {
          // Check if expert is busy
          if (statusEventBus.isProcessing(expertAgent.id)) {
            // Expert is busy — queue the message in the expert's inbox
            const queuedMsg = `[From: ${agent.name}] /${expertCmd.command} ${expertCmd.module}\n\nPlease inspect "${expertCmd.module}" and provide your full report in your standard format. When done, use \`report_completion\` + \`message_superior\` to return the report.`;
            await services.inboxService.sendMessage(
              agentId, expertAgent.id, queuedMsg, "peer", true, "normal",
            );
            // Persist user message under original agent
            await services.chatMessageService.saveMessage({
              id: userMessageId, agentId, role: "user", content: message,
              isBackground: false, isRead: true, createdAt: Date.now(),
            });
            const busyMsg = `⏳ **${expertAgent.name}**（${expertCmd.role.replace(/_/g, " ")}）正在处理其他请求，你的审查请求已加入队列，专家完成后会自动处理并通知你。`;
            await services.chatMessageService.saveMessage({
              id: assistantMessageId, agentId, role: "assistant", content: busyMsg,
              toolCalls: "[]", isBackground: false, isRead: true, isStreaming: false, createdAt: Date.now(),
            });
            statusEventBus.setProcessing(agentId, false);
            setSSEHeaders(reply);
            writeSSE(reply, "text", busyMsg);
            writeSSE(reply, "done", "");
            return reply;
          }

          // Expert is free — redirect message directly to expert for immediate streaming
          const redirectedMsg = `[From: ${agent.name}]\n/${expertCmd.command} ${expertCmd.module}\n\nPlease inspect "${expertCmd.module}" and provide your full report in your standard format. When done, use \`report_completion\` + \`message_superior\` to return the report.`;

          // Persist under both agents
          await services.chatMessageService.saveMessage({
            id: userMessageId, agentId: expertAgent.id, role: "user", content: redirectedMsg,
            isBackground: false, isRead: true, createdAt: Date.now(),
          });
          await services.chatMessageService.saveMessage({
            id: randomUUID(), agentId, role: "user", content: message,
            isBackground: false, isRead: true, createdAt: Date.now(),
          });
          statusEventBus.setProcessing(agentId, false);
          statusEventBus.setProcessing(expertAgent.id, true);

          try {
            // Build expert context and stream response
            setSSEHeaders(reply);
            const expertResolved = await resolveModelForAgent(expertAgent.id);

            const expertCtxBlock = await (async () => {
              try { return await services.memoryService.buildAgentContext(expertAgent.id, expertAgent.moduleId || undefined); } catch { return ""; }
            })();

            // Cache-optimized split: static before history, dynamic after
            const expertStaticContext = [
              await buildGoalsBlock(expertAgent.projectId),
              await buildWorkspaceBlock(expertAgent.projectId, expertAgent.role),
            ].filter(Boolean).join("\n\n");

            const expertDynamicContext = [
              await buildTimeBlock(expertAgent.projectId),
              expertCtxBlock,
            ].filter(Boolean).join("\n\n");

            const expertFullContext = [expertStaticContext, expertDynamicContext].filter(Boolean).join("\n\n");

            const expertIdentityPrompt = buildIdentityPrompt({
              agentName: expertAgent.name, role: expertAgent.role,
              permissionType: expertAgent.permissionType as "coordinator" | "executor",
              goal: expertAgent.goal, backstory: expertAgent.backstory,
              operatorName: await getOperatorName(),
            });

            const expertHistory = await conversationStore.getHistory(
              expertAgent.id,
              calculateHistoryBudget(expertResolved.contextWindow, expertResolved.maxOutputTokens, expertIdentityPrompt, expertFullContext, redirectedMsg),
              services.projectDb,
            );

            const expertRuntime = new AgentRuntime({
              agentId: expertAgent.id, agentName: expertAgent.name, role: expertAgent.role,
              permissionType: expertAgent.permissionType as "coordinator" | "executor",
              goal: expertAgent.goal, backstory: expertAgent.backstory,
              systemPrompt: "", identityPrompt: expertIdentityPrompt, contextPrompt: expertStaticContext || undefined, dynamicContextPrompt: expertDynamicContext || undefined,
              history: expertHistory as Message[],
              operatorName: await getOperatorName(),
              baseUrl: expertResolved.baseUrl, model: expertResolved.modelId, provider: expertResolved.provider,
              supportsImages: expertResolved.supportsImages, apiKey: expertResolved.apiKey,
              contextWindow: expertResolved.contextWindow, maxOutputTokens: expertResolved.maxOutputTokens,
              limitInput: expertResolved.limitInput,
              respectsInlineCacheHints: expertResolved.respectsInlineCacheHints,
              temperature: expertResolved.temperature, reasoningEffort: expertResolved.reasoningEffort,
              sessionId: randomUUID(),
              toolExecutor: makeToolExecutor(services, expertAgent.projectId),
              messagePoller: createMessagePoller(expertAgent.id, services),
              compactor: makeMidTurnCompactor(expertResolved, expertAgent.id, services.projectDb),
              approvalHandler: async (aId, toolName, toolArgs, description) =>
                services.approvalService.createRequest({ agentId: aId, toolName, toolArguments: toolArgs, description }),
              approvalWaiter: (requestId) => waitForApprovalResponse(requestId),
              toolOutputStore,
            });

            let expertFullText = "";
            const expertToolCallsLog: Array<{ name: string; args: any }> = [];
            try {
              for await (const event of expertRuntime.chat(redirectedMsg)) {
                if (event.type === "text") {
                  expertFullText += event.content || "";
                  writeSSE(reply, "text", event.content);
                } else if (event.type === "tool_use") {
                  expertToolCallsLog.push({ name: event.content, args: (event as any).metadata?.input });
                  writeSSE(reply, "tool_use", { tool: event.content, input: (event as any).metadata?.input });
                } else if (event.type === "tool_result") {
                  writeSSE(reply, "tool_result", { tool: event.content, result: (event as any).metadata?.result });
                } else if (event.type === "approval_request") {
                  writeSSE(reply, "approval_request", { requestId: (event as any).metadata?.requestId, toolName: event.content, input: (event as any).metadata?.input });
                } else if (event.type === "error") {
                  writeSSE(reply, "error", (event as any).metadata?.error || event.content);
                } else if (event.type === "done") { break; }
              }
            } catch (streamErr: any) {
              writeSSE(reply, "error", streamErr?.message || "Expert stream error");
            }

            await services.chatMessageService.saveMessage({
              id: assistantMessageId, agentId: expertAgent.id, role: "assistant",
              content: expertFullText || "(expert report)",
              toolCalls: JSON.stringify(expertToolCallsLog),
              isBackground: false, isRead: true, isStreaming: false, createdAt: Date.now(),
            });

            writeSSE(reply, "done", "");
            return reply;
          } catch (err: any) {
            fastify.log.warn(err, `Failed to route expert command /${expertCmd.command} ${expertCmd.module}`);
            writeSSE(reply, "error", err?.message || "Expert routing error");
            writeSSE(reply, "done", "");
            return reply;
          } finally {
            statusEventBus.setProcessing(expertAgent.id, false);
          }
        }
      } catch (err: any) {
        fastify.log.warn(err, `Expert command routing failed for /${expertCmd.command} ${expertCmd.module}`);
        // Fall through to normal processing if expert routing fails
      }
    }

    // --- Build context (project memories + private memories) ---
    let contextBlock = "";
    try {
      contextBlock = await services.memoryService.buildAgentContext(agentId, agent.moduleId || undefined);
    } catch (err: any) {
      fastify.log.warn(err, "Failed to build agent context, proceeding without");
    }

    // --- If coordinator: load subordinate work logs (log-reading protocol with cursor) ---
    let subordinateLogsBlock = "";
    if (agent.permissionType === "coordinator") {
      try {
        const children = await services.orgService.getChildren(agentId);
        // Always list subordinates so coordinator knows their team
        if (children.length > 0) {
          subordinateLogsBlock += "## Your Subordinates\n";
          for (const child of children) {
            subordinateLogsBlock += `- **${child.name}** (${child.role}, ID: ${child.id})\n`;
          }
        }

        // Get cursor: only inject logs newer than lastSeenLogAt
        const cursor = agent.lastSeenLogAt || 0;
        let maxLogTimestamp = cursor;

        // Append NEW work logs for each subordinate (cursor-based)
        for (const child of children) {
          const logs = await services.dispatchService.getSubordinateLogsSince(child.id, cursor);
          if (logs.length > 0) {
            subordinateLogsBlock += `\n### New logs from ${child.name} (${child.role}, ID: ${child.id}):\n`;
            for (const log of logs) {
              subordinateLogsBlock += `- [${log.type}] ${log.summary}\n`;
              if (log.createdAt > maxLogTimestamp) {
                maxLogTimestamp = log.createdAt;
              }
            }
          }
        }

        // Update cursor to the latest log timestamp we've seen
        if (maxLogTimestamp > cursor) {
          await services.orgService.updateAgent(agentId, { lastSeenLogAt: maxLogTimestamp });
        }
      } catch (err: any) {
        fastify.log.warn(err, "Failed to load subordinate logs");
      }
    }

    // --- Handoff injection: task delivery between agents ---
    let handoffBlock = "";
    let hasExpectReport = false;
    try {
      // All agents: show incoming pending tasks from their superior
      const pending = await services.handoffService.getPendingHandoffs(agentId);
      if (pending.length > 0) {
        handoffBlock += "## Pending Tasks Assigned to You\n";
        handoffBlock += "Your coordinator has assigned you the following tasks. Please work on them and use report_completion when done.\n\n";
        for (const h of pending) {
          const fromAgent = await services.orgService.getAgent(h.fromAgentId);
          const fromName = fromAgent?.name || h.fromAgentId;
          handoffBlock += `- **From ${fromName}** (handoffId: ${h.id}): ${h.summary}\n`;
          if ((h as any).expectReport) {
            hasExpectReport = true;
          }
        }
        // Auto-accept: mark pending handoffs as "accepted"
        await services.handoffService.acceptPendingHandoffs(agentId);
      } else {
        // Check for accepted (in-progress) handoffs
        const accepted = await services.handoffService.getAcceptedHandoffs(agentId);
        if (accepted.length > 0) {
          handoffBlock += "## In-Progress Tasks Assigned to You\n";
          handoffBlock += "You are currently working on these tasks. Use report_completion when done.\n\n";
          for (const h of accepted) {
            const fromAgent = await services.orgService.getAgent(h.fromAgentId);
            const fromName = fromAgent?.name || h.fromAgentId;
            handoffBlock += `- **From ${fromName}** (handoffId: ${h.id}): ${h.summary}\n`;
            if ((h as any).expectReport) {
              hasExpectReport = true;
            }
          }
        }
      }

      // Inject mandatory reporting instruction if any task requires it
      if (hasExpectReport) {
        handoffBlock += "\n> **[SYSTEM MANDATORY]** One or more tasks above require you to report results back. " +
          "After completing them, you MUST call `hiveweave__message_superior` with a summary of your results. " +
          "This is a mandatory requirement from your coordinator.\n";
      }

      // Coordinators additionally: show completed handoffs from subordinates
      if (agent.permissionType === "coordinator") {
        const children = await services.orgService.getChildren(agentId);
        let completedBlock = "";
        for (const child of children) {
          const completed = await services.handoffService.getCompletedFromSubordinate(agentId, child.id, 3);
          if (completed.length > 0) {
            completedBlock += `\n### Completed tasks by ${child.name}:\n`;
            for (const h of completed) {
              completedBlock += `- [completed] ${h.summary}\n`;
            }
          }
        }
        if (completedBlock) {
          handoffBlock += "\n## Task Handoff Results from Subordinates\n" + completedBlock;
        }
      }
    } catch (err: any) {
      fastify.log.warn(err, "Failed to load handoffs");
    }

    // --- Inbox injection: messages from subordinates and peers ---
    let inboxBlock = "";
    if (agent.permissionType === "coordinator") {
      try {
        const pendingMessages = await services.inboxService.getPendingMessages(agentId);
        if (pendingMessages.length > 0) {
          const alarmMsgs = pendingMessages.filter((m: any) => m.messageType === "alarm");
          const superiorMsgs = pendingMessages.filter((m: any) => m.messageType !== "peer" && m.messageType !== "alarm");
          const peerMsgs = pendingMessages.filter((m: any) => m.messageType === "peer");

          if (alarmMsgs.length > 0) {
            inboxBlock += "## Scheduled Alarms\n";
            for (const msg of alarmMsgs) {
              const fromAgent = await services.orgService.getAgent(msg.fromAgentId);
              const fromName = fromAgent?.name || msg.fromAgentId;
              inboxBlock += `- **Alarm from ${fromName}**: "${msg.message}"\n`;
              await services.teamChatService.recordIncoming(agentId, msg.fromAgentId, msg.message, msg.id);
            }
          }

          if (superiorMsgs.length > 0) {
            inboxBlock += "## Messages from Subordinates\n";
            inboxBlock += "Your subordinates have sent you the following messages. Please address them.\n\n";
            for (const msg of superiorMsgs) {
              const fromAgent = await services.orgService.getAgent(msg.fromAgentId);
              const fromName = fromAgent?.name || msg.fromAgentId;
              const reportTag = (msg as any).expectReport ? " **[REPLY REQUIRED]**" : "";
              inboxBlock += `- **${fromName}** asks${reportTag}: "${msg.message}"\n`;
              await services.teamChatService.recordIncoming(agentId, msg.fromAgentId, msg.message, msg.id);
            }
          }
          if (peerMsgs.length > 0) {
            inboxBlock += "\n## Messages from Peers\n";
            inboxBlock += "Peer agents have sent you the following messages. Respond if appropriate.\n\n";
            for (const msg of peerMsgs) {
              const fromAgent = await services.orgService.getAgent(msg.fromAgentId);
              const fromName = fromAgent?.name || msg.fromAgentId;
              const reportTag = (msg as any).expectReport ? " **[REPLY REQUIRED]**" : "";
              inboxBlock += `- **${fromName}** says${reportTag}: "${msg.message}"\n`;
              await services.teamChatService.recordIncoming(agentId, msg.fromAgentId, msg.message, msg.id);
            }
          }
          // Mark as read so they don't appear again
          await services.inboxService.markAsRead(agentId);
        }
      } catch (err: any) {
        fastify.log.warn(err, "Failed to load inbox messages");
      }
    }

    // --- Set SSE headers ---
    setSSEHeaders(reply);

    // --- Send message IDs to client for dedup synchronization ---
    writeSSE(reply, "message_id", { role: "user", id: userMessageId });
    writeSSE(reply, "message_id", { role: "assistant", id: assistantMessageId });

    // --- Resolve model for this agent from the registry ---
    let resolved: ResolvedModel;
    try {
      resolved = await resolveModelForAgent(agentId);
    } catch {
      // No models configured — fall back to mock mode
      resolved = null as any;
    }

    // --- Mock mode (no model configured) ---
    if (!resolved) {
      fastify.log.info("No LLM model configured — using mock streaming");

      let stream: AsyncGenerator<StreamEvent>;

      if (agent.permissionType === "coordinator") {
        const children = await services.orgService.getChildren(agentId);
        stream = mockCoordinatorStream(
          agent.name,
          message,
          children.map((c: any) => ({ id: c.id, name: c.name })),
        );
      } else {
        stream = mockExecutorStream(agent.name, message);
      }

      // Create per-project ToolExecutor for mock mode
      const mockToolExecutor = makeToolExecutor(services, agent.projectId);

      // Collect tool_use events for execution after streaming
      const toolCalls: Array<{ tool: string; input: Record<string, any> }> = [];
      let mockFullText = "";

      for await (const event of stream) {
        switch (event.type) {
          case "text":
            writeSSE(reply, "text", event.content);
            mockFullText += event.content;
            break;
          case "tool_use": {
            const toolName = event.content;
            const input = event.metadata?.input || {};
            writeSSE(reply, "tool_use", { tool: toolName, input });
            toolCalls.push({ tool: toolName, input });
            break;
          }
          case "error":
            writeSSE(reply, "error", event.content);
            break;
          case "done":
            break;
        }
      }

      // Execute collected tool calls via ToolExecutor
      for (const tc of toolCalls) {
        const result = await mockToolExecutor.execute(agentId, session, tc.tool, tc.input);
        writeSSE(reply, "tool_result", { tool: tc.tool, result });
      }

      if (toolCalls.length > 0) {
        writeSSE(reply, "text", `\n\n${toolCalls.length} tool(s) executed successfully.`);
      }

      // Finalize assistant message
      await services.chatMessageService.updateMessage(assistantMessageId, {
        content: mockFullText.trim(),
        toolCalls: JSON.stringify(toolCalls.map(tc => ({ tool: tc.tool, input: tc.input }))),
        isStreaming: false,
      });

      writeSSE(reply, "done", "");
      statusEventBus.setProcessing(agentId, false);
      reply.raw.end();
      return reply;
    }

    // --- Real mode (DeepSeek API) ---
    const focusInstruction = `## Response Guidelines
- Answer the user's current question DIRECTLY and CONCISELY (3-5 sentences max unless they ask for details).
- Do NOT call read_work_logs unless the user specifically asks about subordinate status or progress.
- Do NOT repeat or summarize project history, architecture, or context unless explicitly asked.
- If the user says "hello" or asks a simple question, respond simply without dumping information.
- Only reference context (subordinate logs, memories, handoffs) when DIRECTLY relevant to the current question.
- Avoid bullet-point reports unless the user asks for a status report or detailed analysis.`;

    // --- Inject core team (CEO, HR, QA) into EVERY agent's context ---
    // Everyone must know who leads the org and who handles quality.
    // Other team members are discovered via read_roster / list_subordinates.
    let rosterBlock = "";
    if (agent.projectId) {
      try {
        const ceo = await services.orgService.findAgentByRole(agent.projectId, "ceo");
        const hr = await services.orgService.findAgentByRole(agent.projectId, "hr");
        const qa = await services.orgService.findAgentByRole(agent.projectId, "qa_engineer");
        const core = [ceo, hr, qa].filter(Boolean);
        if (core.length > 0) {
          rosterBlock = "## Core Team (always available)\n";
          for (const m of core) {
            const isYou = m!.id === agent.id ? " ← YOU" : "";
            const roleLabel = m!.role === "ceo" ? "CEO — final decision-maker, owns charter and org structure" :
              m!.role === "hr" ? "HR — creates, transfers, and dismisses agents; manages personnel" :
              "QA Engineer — code review, security audit, test analysis, performance audit";
            rosterBlock += `- **${m!.name}** (${m!.shortId || m!.id.slice(0, 8)}) — ${roleLabel}${isYou}\n`;
          }
          rosterBlock += "\nOther team members can be found via the `read_roster` tool. If you are a manager, use `list_subordinates` to see your direct reports.";
        }
      } catch (err: any) {
        fastify.log.warn(err, "Failed to load core team for agent context");
      }
    }

    // Split prompt: static identity (cacheable) + dynamic context
    const isLeadership = agent.role.toLowerCase() === "ceo" || agent.role.toLowerCase() === "hr";
    const operatorName = await getOperatorName();
    const identityPrompt = buildIdentityPrompt({
      agentName: agent.name,
      role: agent.role,
      permissionType: agent.permissionType as "coordinator" | "executor",
      goal: agent.goal,
      backstory: agent.backstory,
      includeParadigmCatalog: isLeadership,
      hasBindingTools: isLeadership,
      operatorName,
    });

    // --- Workspace info ---
    const workspaceBlock = await buildWorkspaceBlock(agent.projectId, agent.role);

    // --- Project info (name, description, paradigm) ---
    const projectBlock = await buildProjectBlock(agent.projectId);

    const charterBlock = await buildCharterBlock(agent.projectId);

    const goalsBlock = await buildGoalsBlock(agent.projectId);
    const projectStatusBlock = await buildProjectStatusBlock(agent.projectId, agent.role);

    let staffingContactBlock = "";
    const agentRole = agent.role.toLowerCase();
    if (agentRole !== "hr" && agent.projectId) {
      try {
        const hrAgent = await services.orgService.findAgentByRole(agent.projectId, "hr");
        if (hrAgent) {
          const hrLabel = hrAgent.shortId || hrAgent.id.slice(0, 8);
          staffingContactBlock =
            `## Staffing\n` +
            `Need to add team members? Use \`send_message\` to contact **HR** (${hrAgent.name}, ID: ${hrLabel}). ` +
            `Only HR can create, transfer, or dismiss agents.`;
        }
      } catch {
        /* optional */
      }
    }


    // --- Skill injection: load bound skills' SKILL.md from ClawHub ---
    let skillsBlock = "";
    try {
      const boundSkills: string[] = JSON.parse(agent.boundSkills || "[]");
      if (boundSkills.length > 0) {
        skillsBlock = await clawhubService.buildSkillsBlock(boundSkills);
      }
    } catch (err: any) {
      fastify.log.warn(err, "Failed to load bound skills, proceeding without");
    }

    const timeBlock = await buildTimeBlock(agent.projectId);

    // --- Split context into STATIC and DYNAMIC parts for DeepSeek prefix caching ---
    //
    // DeepSeek uses implicit (byte-level) prefix caching: everything before the
    // first differing byte is cached. By putting STATIC content before history
    // and DYNAMIC content AFTER history, we maximize the cacheable prefix:
    //
    //   [identity — cached] [static context — cached] [history — cached prefix]
    //   [dynamic context — miss] [user msg — miss]
    //
    // → ~70%+ cache hit rate (vs ~30% before when dynamic content was inline)

    const staticContextPrompt = [
      focusInstruction,       // STATIC
      projectBlock,           // STATIC (rarely changes)
      charterBlock,           // STATIC (rarely changes)
      goalsBlock,             // SEMI-STATIC (changes occasionally)
      projectStatusBlock,     // SEMI-STATIC (CEO-only: project age, team size)
      staffingContactBlock,   // STATIC
      workspaceBlock,         // STATIC
      rosterBlock,            // SEMI-STATIC
      skillsBlock,            // SEMI-STATIC (changes on bind/unbind)
    ]
      .filter(Boolean)
      .join("\n\n");

    const dynamicContextPrompt = [
      timeBlock,              // DYNAMIC (time changes every call)
      contextBlock,           // DYNAMIC (memories accumulate)
      subordinateLogsBlock ? `\n## Subordinate Work Logs\n${subordinateLogsBlock}` : "", // DYNAMIC
      handoffBlock,           // DYNAMIC
      inboxBlock,             // DYNAMIC
    ]
      .filter(Boolean)
      .join("\n\n");

    // Load conversation history — token budget accounts for all static + dynamic parts
    const fullContextForBudget = [staticContextPrompt, dynamicContextPrompt].filter(Boolean).join("\n\n");
    const historyBudget = calculateHistoryBudget(resolved.contextWindow, resolved.maxOutputTokens, identityPrompt, fullContextForBudget, message);
    const history = await conversationStore.getHistory(agentId, historyBudget, services.projectDb);

    // Prefix hash: detect cache-invalidating changes to the static prompt
    const currentHash = computePrefixHash(identityPrompt, agent.role + ":" + agent.permissionType);
    const prevHash = prefixHashTracker.get(agentId);
    if (prevHash && prevHash !== currentHash) {
      fastify.log.warn(`[PrefixCache] DRIFT detected for ${agent.name} (${agentId.slice(0, 8)}): ${prevHash} → ${currentHash}. DeepSeek prefix cache invalidated.`);
    }
    prefixHashTracker.set(agentId, currentHash);

    // Create per-project ToolExecutor (with reviewLLM for Reviewer agent)
    const toolExec = makeToolExecutor(services, agent.projectId, resolved);

    const runtimeConfig: AgentRuntimeConfig = {
      agentId,
      agentName: agent.name,
      role: agent.role,
      permissionType: agent.permissionType as "coordinator" | "executor",
      goal: agent.goal,
      backstory: agent.backstory,
      systemPrompt: "", // unused in new mode
      identityPrompt,
      contextPrompt: staticContextPrompt || undefined,
      dynamicContextPrompt: dynamicContextPrompt || undefined,
      history: history as Message[],
      operatorName,
      baseUrl: resolved.baseUrl,
      model: resolved.modelId,
      provider: resolved.provider,
      supportsImages: resolved.supportsImages,
      apiKey: resolved.apiKey,
      contextWindow: resolved.contextWindow,
      maxOutputTokens: resolved.maxOutputTokens,
      limitInput: resolved.limitInput,
      respectsInlineCacheHints: resolved.respectsInlineCacheHints,
      temperature: resolved.temperature,
      reasoningEffort: resolved.reasoningEffort,
      sessionId: session,
      toolExecutor: toolExec,
      // Permission gate: check → ask → wait
      permissionChecker: (aId, toolName, toolArgs) =>
        services.permissionService.checkPermission(aId, toolName, toolArgs),
      approvalHandler: async (aId, toolName, toolArgs, description) =>
        services.approvalService.createRequest({ agentId: aId, toolName, toolArguments: toolArgs, description }),
      approvalWaiter: (requestId) =>
        waitForApprovalResponse(requestId, 5 * 60 * 1000),
      compactor: makeMidTurnCompactor(resolved, agent.id || agentId, services.projectDb),
      messagePoller: createMessagePoller(agentId, services),
      toolOutputStore,
    };

    const prefixedMessage = prefixTriggerForProject(agent.projectId, message);

    const runtime = new AgentRuntime(runtimeConfig);

    // Collect dispatch_task events for auto-trigger
    const dispatchedSubordinates: Array<{ toAgentId: string; description: string }> = [];
    const peersMessaged: string[] = [];
    let fullText = "";
    const allToolCalls: Array<{ tool: string; input: Record<string, any> }> = [];
    let finalMessages: StoredMessage[] = [];
    let pendingApprovalId: string | null = null;

    // Activity buffer: merge consecutive text/thinking deltas to avoid spam
    let textBuffer = "";
    let thinkingBuffer = "";
    let bufferTimer: ReturnType<typeof setTimeout> | null = null;
    const BUFFER_THRESHOLD_MS = 300;

    function flushActivityBuffer(): void {
      if (bufferTimer) { clearTimeout(bufferTimer); bufferTimer = null; }
      if (thinkingBuffer) {
        statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "thinking", content: thinkingBuffer, timestamp: Date.now() });
        thinkingBuffer = "";
      }
      if (textBuffer) {
        statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "text", content: textBuffer, timestamp: Date.now() });
        textBuffer = "";
      }
    }

    // Cancel pending approval wait if client disconnects mid-approval
    const handleClose = () => {
      console.warn(`[CHAT:${agentId.slice(0, 8)}] SSE client disconnected`);
      if (pendingApprovalId) {
        cancelApprovalWait(pendingApprovalId);
        fastify.log.info(`SSE disconnected — cancelled approval wait ${pendingApprovalId}`);
        pendingApprovalId = null;
      }
      // Clear processing flag on disconnect — the main finally block
      // may not be reached if the stream throws before it
      statusEventBus.setProcessing(agentId, false);
    };
    reply.raw.on("close", handleClose);

    try {
      // Use manual iterator + Promise.race for SSE keepalive during approval waits
      const iter = runtime.chat(prefixedMessage, images)[Symbol.asyncIterator]();
      while (true) {
        if (statusEventBus.isPaused) {
          fastify.log.info(`[CHAT:${agentId.slice(0, 8)}] Paused mid-stream — stopping`);
          flushActivityBuffer();
          writeSSE(reply, "error", "HiveWeave 已下班，所有工作已暂停。");
          break;
        }

        const result = await Promise.race([
          iter.next(),
          new Promise<{ keepalive: true }>((r) => setTimeout(() => r({ keepalive: true }), 15_000)),
        ]);

        // SSE keepalive: send comment to prevent proxy timeout during approval wait
        if ("keepalive" in result) {
          reply.raw.write(": keepalive\n\n");
          flushActivityBuffer(); // text may have finished accumulating
          continue;
        }

        if (result.done) {
          flushActivityBuffer(); // flush any remaining text/thinking
          break;
        }
        const event: StreamEvent = result.value;

        switch (event.type) {
          case "text":
            writeSSE(reply, "text", event.content);
            fullText += event.content;
            textBuffer += event.content;
            // Debounce: emit merged text after a pause, or on next non-text event
            if (bufferTimer) clearTimeout(bufferTimer);
            bufferTimer = setTimeout(() => flushActivityBuffer(), BUFFER_THRESHOLD_MS);
            break;
          case "thinking":
            writeSSE(reply, "thinking", event.content);
            thinkingBuffer += event.content;
            if (bufferTimer) clearTimeout(bufferTimer);
            bufferTimer = setTimeout(() => flushActivityBuffer(), BUFFER_THRESHOLD_MS);
            break;
          case "tool_use": {
            writeSSE(reply, "tool_use", {
              tool: event.content,
              input: event.metadata?.input || {},
            });
            const toolName = event.content.replace(/^hiveweave__/, "");
            allToolCalls.push({ tool: toolName, input: event.metadata?.input || {} });
            flushActivityBuffer();
            statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "tool_use", toolName, toolInput: JSON.stringify(event.metadata?.input || {}).slice(0, 200), timestamp: Date.now() });
            // Track dispatch_task for auto-trigger
            if (toolName === "dispatch_task" && event.metadata?.input) {
              const inp = event.metadata.input as Record<string, any>;
              if (inp.toAgentId) {
                dispatchedSubordinates.push({
                  toAgentId: inp.toAgentId,
                  description: inp.description || "",
                });
              }
            }
            // Immediately trigger non-user recipients of send_message
            if (toolName === "send_message" && event.metadata?.input) {
              const inp = event.metadata.input as Record<string, any>;
              const recipients = String(inp.recipients || inp.toAgentId || "").split(",").map((s: string) => s.trim()).filter(Boolean);
              for (const rcpt of recipients) {
                if (rcpt.toLowerCase() === "user") continue;
                try {
                  const target = await services.orgService.resolveAgent(rcpt);
                  if (target) {
                    // Wake up recipient immediately — don't wait for stream end
                    if (target.permissionType === "coordinator") {
                      triggerCoordinator(target.id).catch(() => {});
                    } else {
                      triggerSubordinate(target.id).catch(() => {});
                    }
                    if (!peersMessaged.includes(target.id)) {
                      peersMessaged.push(target.id);
                    }
                  }
                } catch { /* resolution failed */ }
              }
            }
            // Track message_superior for auto-trigger (superior needs to process the report)
            if (toolName === "message_superior" && agentId) {
              // Resolve the sender's parent ID
              const senderAgent = await services.orgService.getAgent(agentId);
              const superiorId = senderAgent?.parentId;
              if (superiorId && !peersMessaged.includes(superiorId)) {
                peersMessaged.push(superiorId);
              }
            }
            break;
          }
          case "tool_result":
            pendingApprovalId = null; // Approval resolved, no longer pending
            writeSSE(reply, "tool_result", {
              tool: event.content,
              result: event.metadata?.result || "",
            });
            flushActivityBuffer();
            statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "tool_result", toolName: event.content.replace(/^hiveweave__/, ""), toolResult: String(event.metadata?.result || "").slice(0, 300), timestamp: Date.now() });
            break;
          case "approval_request":
            pendingApprovalId = (event.metadata?.requestId as string) || null;
            writeSSE(reply, "approval_request", {
              tool: event.content,
              input: event.metadata?.input || {},
              requestId: event.metadata?.requestId || "",
            });
            break;
          case "compacting":
            flushActivityBuffer();
            writeSSE(reply, "compacting", event.content);
            break;
          case "retry":
            flushActivityBuffer();
            writeSSE(reply, "retry", {
              reason: event.content,
              attempt: event.metadata?.attempt,
              maxRetries: event.metadata?.maxRetries,
              delayMs: event.metadata?.delayMs,
            });
            break;
          case "queued_message":
            writeSSE(reply, "queued_message", {
              content: event.content,
              messages: event.metadata?.messages || [],
            });
            break;
          case "error":
            writeSSE(reply, "error", event.content);
            flushActivityBuffer();
            statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "error", errorMessage: event.content, timestamp: Date.now() });
            break;
          case "done":
            // Capture final messages for history storage
            if (event.metadata?.messages) {
              finalMessages = event.metadata.messages as StoredMessage[];
            }
            writeSSE(reply, "done", "");
            flushActivityBuffer();
            statusEventBus.emitActivity({ agentId: agent.id, agentName: agent.name, type: "done", timestamp: Date.now() });
            break;
        }
      }
    } catch (error: any) {
      console.error(`[CHAT:${agentId.slice(0, 8)}] Streaming error:`, error.message);
      fastify.log.error(error, "Streaming error");
      writeSSE(reply, "error", error.message || "Stream failed");
    }

    // Clean up close handler — stream ended normally or with error
    reply.raw.removeListener("close", handleClose);
    pendingApprovalId = null;

    console.log(`[CHAT:${agentId.slice(0, 8)}] Stream ended: text=${fullText.length}chars, tools=${allToolCalls.length}, finalMsgs=${finalMessages.length}`);

    reply.raw.end();

    // Store conversation history — persists to DB and trims to token budget
    if (finalMessages.length > 0) {
      await conversationStore.appendTurn(agentId, finalMessages, historyBudget, services.projectDb);
    }

    // Finalize assistant message in DB
    try {
      await services.chatMessageService.updateMessage(assistantMessageId, {
        content: fullText.trim(),
        toolCalls: JSON.stringify(allToolCalls),
        isStreaming: false,
      });
    } finally {
      // Agent done processing — always clear even if save fails
      statusEventBus.setProcessing(agentId, false);
    }

    // --- Auto-trigger: fire-and-forget background tasks for dispatched subordinates ---
    if (dispatchedSubordinates.length > 0 && resolved) {
      for (const sub of dispatchedSubordinates) {
        if (runningAutoTriggers.has(sub.toAgentId)) {
          fastify.log.info(`Auto-trigger skipped: ${sub.toAgentId} already running`);
          continue;
        }
        // Fire and forget — don't await
        triggerSubordinate(sub.toAgentId).catch((err) =>
          fastify.log.error(err, `Auto-trigger failed for ${sub.toAgentId}`),
        );
      }
    }

    // --- Auto-trigger: wake up peers who received messages ---
    if (peersMessaged.length > 0 && resolved) {
      for (const peerId of peersMessaged) {
        if (runningAutoTriggers.has(peerId)) {
          fastify.log.info(`Peer auto-trigger skipped: ${peerId} already running`);
          continue;
        }
        fastify.log.info(`Peer auto-trigger: ${agent.name} messaged peer ${peerId}`);
        triggerCoordinator(peerId).catch((err) =>
          fastify.log.error(err, `Peer auto-trigger failed for ${peerId}`),
        );
      }
    }

    } finally {
      // Outer safety net — guarantees processing is cleared even if context building,
      // inbox injection, model resolution, or any intermediate step throws.
      statusEventBus.setProcessing(agentId, false);
    }

    return reply;
  });

  // ---------------------------------------------------------------------------
  // Chat history & background message endpoints
  // ---------------------------------------------------------------------------

  const MarkReadBody = z.object({ ids: z.array(z.string()) });

  /** Helper: get per-project ChatMessageService for an agent */
  function getChatMessageServiceForAgent(agentId: string): ChatMessageService | null {
    const ws = lookupAgentWorkspace(agentId);
    if (!ws) return null;
    try {
      return new ChatMessageService(ensureProjectDb(ws));
    } catch { return null; }
  }

  /** GET /messages/:agentId — full chat history for an agent */
  fastify.get<{ Params: { agentId: string } }>("/messages/:agentId", async (request, reply) => {
    const svc = getChatMessageServiceForAgent(request.params.agentId);
    if (!svc) return reply.status(404).send({ error: "Agent not found" });
    const messages = await svc.getMessages(request.params.agentId);
    return messages;
  });

  /** GET /unread/:agentId — unread background messages (for polling) */
  fastify.get<{ Params: { agentId: string } }>("/unread/:agentId", async (request, reply) => {
    const svc = getChatMessageServiceForAgent(request.params.agentId);
    if (!svc) return reply.status(404).send({ error: "Agent not found" });
    const messages = await svc.getUnreadBackground(request.params.agentId);
    return messages;
  });

  /** GET /user-pings — agents that have sent user-directed messages since last poll */
  fastify.get("/user-pings", async (_request, _reply) => {
    return { agentIds: userPingTracker.drain() };
  });

  /** GET /questions — pending user questions from agents (frontend polls this) */
  fastify.get("/questions", async (_request, _reply) => {
    return drainQuestions();
  });

  /** POST /questions/:id/answer — user answers an agent's question */
  fastify.post<{ Params: { id: string }; Body: { answer: string } }>("/questions/:id/answer", async (request, reply) => {
    const { id } = request.params;
    const { answer } = request.body;
    if (!answer) return reply.status(400).send({ error: "answer is required" });
    const ok = answerQuestion(id, answer);
    return ok ? { ok: true } : reply.status(404).send({ error: "Question not found or already answered" });
  });

  /** GET /todos/:agentId — get task list for an agent */
  fastify.get<{ Params: { agentId: string } }>("/todos/:agentId", async (request, reply) => {
    const todos = getTodos(request.params.agentId);
    return todos || { agentId: request.params.agentId, todos: [], updatedAt: 0 };
  });

  /** POST /mark-read — mark specific messages as read */
  fastify.post<{ Body: z.infer<typeof MarkReadBody> }>("/mark-read", async (request, reply) => {
    const parsed = MarkReadBody.safeParse(request.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: "Invalid body", issues: parsed.error.issues });
    }
    // For mark-read, we need to find which project these messages belong to.
    // Messages could belong to multiple agents in different projects, but typically
    // they're all from one agent. Use the first message's agent to resolve the project.
    // As a practical approach, scan all project DBs.
    const ids = parsed.data.ids;
    if (ids.length === 0) return { marked: 0 };

    // Try all project DBs until one succeeds
    const allProjects = await projectService.listProjects();
    let totalMarked = 0;
    for (const proj of allProjects) {
      if (!proj.workspacePath) continue;
      try {
        const projectDb = ensureProjectDb(proj.workspacePath);
        const svc = new ChatMessageService(projectDb);
        const count = await svc.markAsRead(ids);
        totalMarked += count;
      } catch { /* continue */ }
    }
    return { marked: totalMarked };
  });

  /**
   * GET /status — SSE stream for real-time agent processing status.
   *
   * Sends:
   *   event: snapshot  data: {"agentIds":["id1","id2"]}   — current state on connect
   *   event: status    data: {"agentId":"...","processing":true|false}  — incremental update
   *   comment: ": keepalive\n\n" every 30s to prevent proxy timeout
   */
  fastify.get("/status", async (request, reply) => {
    setSSEHeaders(reply);

    // Send current snapshot immediately
    writeSSE(reply, "snapshot", { agentIds: statusEventBus.getAllProcessing(), paused: statusEventBus.isPaused });

    // Subscribe to future changes
    const unsubscribe = statusEventBus.subscribe((agentId, processing) => {
      try {
        writeSSE(reply, "status", { agentId, processing });
      } catch {
        // Connection may be closed — ignore
      }
    });

    // Subscribe to real-time activity events (live only — no replay of stale events)
    const unsubActivity = statusEventBus.subscribeActivityLive((event) => {
      try {
        writeSSE(reply, "activity", event);
      } catch { /* closed */ }
    });

    // Heartbeat to keep connection alive through proxies
    const keepalive = setInterval(() => {
      try {
        reply.raw.write(": keepalive\n\n");
      } catch {
        clearInterval(keepalive);
      }
    }, 30_000);

    // Clean up on disconnect
    const handleClose = () => {
      clearInterval(keepalive);
      unsubscribe();
      unsubActivity();
    };
    reply.raw.on("close", handleClose);

    // Block the handler from returning (keep SSE connection open)
    await new Promise<void>((resolve) => {
      reply.raw.on("close", () => resolve());
    });
  });

  /**
   * POST /pause — pause all agent activity (下班).
   * Clears processing state, blocks new requests.
   */
  fastify.post("/pause", async (_request, reply) => {
    statusEventBus.pause();
    return reply.send({ paused: true });
  });

  /**
   * POST /resume — resume agent activity (上班).
   */
  fastify.post("/resume", async (_request, reply) => {
    statusEventBus.resume();
    return reply.send({ paused: false });
  });

  /**
   * GET /paused — check if system is paused.
   */
  fastify.get("/paused", async (_request, reply) => {
    return reply.send({ paused: statusEventBus.isPaused });
  });

  /** GET /resolved-model/:agentId — return actual model an agent would use */
  fastify.get<{ Params: { agentId: string } }>("/resolved-model/:agentId", async (request, reply) => {
    try {
      const resolved = await resolveModelForAgent(request.params.agentId);
      return reply.send({ agentId: request.params.agentId, modelName: resolved.modelName, modelId: resolved.modelId, source: "auto" });
    } catch (err: any) {
      return reply.status(500).send({ error: err.message });
    }
  });

  // ---------------------------------------------------------------------------
  // Auto-trigger: run subordinate AgentRuntime in the background
  // ---------------------------------------------------------------------------

  /**
   * Create a message poller for a specific agent. The poller checks the inbox
   * for new messages that arrived during a running turn, marks them as read,
   * and returns them as QueuedMessage[]. Called by AgentRuntime at natural
   * breakpoints between tool turns.
   */
  function createMessagePoller(
    agentId: string,
    pollerServices: NonNullable<Awaited<ReturnType<typeof getProjectServices>>>,
  ): MessagePoller {
    return async (): Promise<QueuedMessage[]> => {
      const pending = await pollerServices.inboxService.getPendingMessages(agentId);
      if (pending.length === 0) return [];
      // Mark as read immediately so they won't be picked up again
      await pollerServices.inboxService.markAsRead(agentId);
      const result: QueuedMessage[] = [];
      for (const msg of pending) {
        const fromAgent = await pollerServices.orgService.getAgent(msg.fromAgentId);
        await pollerServices.teamChatService.recordIncoming(
          agentId,
          msg.fromAgentId,
          msg.message,
          msg.id,
        );
        result.push({
          fromName: fromAgent?.name || msg.fromAgentId,
          fromAgentId: msg.fromAgentId,
          message: msg.message,
          messageType: (msg as any).messageType || "superior",
          expectReport: (msg as any).expectReport || false,
          priority: (msg as any).priority || "normal",
        });
      }
      return result;
    };
  }

  async function triggerSubordinate(agentIdOrShort: string): Promise<void> {
    // Resolve shortId or UUID prefix to full UUID via registry or scan
    let resolved: any = null;
    let subServices: NonNullable<Awaited<ReturnType<typeof getProjectServices>>> | null = null;

    // Fast path: if it's a full UUID in the registry
    const wsPath = lookupAgentWorkspace(agentIdOrShort);
    if (wsPath) {
      const allProjects = await projectService.listProjects();
      const project = allProjects.find((p) => p.workspacePath === wsPath);
      if (project) {
        const projectDb = ensureProjectDb(wsPath);
        const orgSvc = new OrgService(projectDb, wsPath);
        const agent = await orgSvc.resolveAgent(agentIdOrShort);
        if (agent) {
          resolved = agent;
          subServices = {
            project, projectDb,
            orgService: orgSvc,
            memoryService: new MemoryService(projectDb),
            dispatchService: new DispatchService(projectDb),
            handoffService: new HandoffService(projectDb),
            inboxService: new InboxService(projectDb),
            ...createChatServices(projectDb),
            rosterService: new RosterService(projectDb),
            fileService: new FileService(),
            permissionService: new PermissionService(projectDb),
            approvalService: new ApprovalService(projectDb),
          };
        }
      }
    }

    // Fallback: scan all projects
    if (!resolved) {
      const allProjects = await projectService.listProjects();
      for (const proj of allProjects) {
        if (!proj.workspacePath) continue;
        try {
          const projectDb = ensureProjectDb(proj.workspacePath);
          const orgSvc = new OrgService(projectDb, proj.workspacePath);
          const agent = await orgSvc.resolveAgent(agentIdOrShort);
          if (agent) {
            resolved = agent;
            subServices = {
              project: proj, projectDb,
              orgService: orgSvc,
              memoryService: new MemoryService(projectDb),
              dispatchService: new DispatchService(projectDb),
              handoffService: new HandoffService(projectDb),
              inboxService: new InboxService(projectDb),
              ...createChatServices(projectDb),
              rosterService: new RosterService(projectDb),
              fileService: new FileService(),
              permissionService: new PermissionService(projectDb),
              approvalService: new ApprovalService(projectDb),
            };
            break;
          }
        } catch { continue; }
      }
    }

    if (!resolved || !subServices) {
      fastify.log.warn(`triggerSubordinate: cannot resolve agent "${agentIdOrShort}"`);
      return;
    }
    const agentId = resolved.id;
    if (runningAutoTriggers.has(agentId)) {
      return; // Already running
    }
    runningAutoTriggers.add(agentId);
    statusEventBus.setProcessing(agentId, true);
    const bgAssistantId = randomUUID();
    try {
      const subAgent = resolved;

      await subServices.chatMessageService.saveMessage({
        id: bgAssistantId,
        agentId,
        role: "assistant",
        content: "",
        toolCalls: "[]",
        isBackground: true,
        isRead: false,
        isStreaming: true,
        createdAt: Date.now(),
      });

      // Resolve model for this subordinate agent
      let subModel: ResolvedModel;
      try {
        subModel = await resolveModelForAgent(agentId);
      } catch {
        fastify.log.warn(`triggerSubordinate: no model configured for ${subAgent.name}, skipping`);
        return;
      }

      // Track communication: coordinator -> subordinate
      if (subAgent.parentId) {
        communicationService.addCommunication(subAgent.parentId, agentId, "trigger");
      }

      // Build context
      let ctxBlock = "";
      try {
        ctxBlock = await subServices.memoryService.buildAgentContext(agentId, subAgent.moduleId || undefined);
      } catch { /* proceed without context */ }

      // Build handoff block
      let hoBlock = "";
      let hasExpectReport = false;
      const pending = await subServices.handoffService.getPendingHandoffs(agentId);
      if (pending.length > 0) {
        hoBlock = "## Pending Tasks Assigned to You\n";
        hoBlock += "Your coordinator has assigned you the following tasks. Please work on them and use report_completion when done.\n\n";
        for (const h of pending) {
          const fromAgent = await subServices.orgService.getAgent(h.fromAgentId);
          const fromName = fromAgent?.name || h.fromAgentId;
          hoBlock += `- **From ${fromName}** (handoffId: ${h.id}): ${h.summary}\n`;
          if ((h as any).expectReport) {
            hasExpectReport = true;
          }
        }
        await subServices.handoffService.acceptPendingHandoffs(agentId);
      } else {
        const accepted = await subServices.handoffService.getAcceptedHandoffs(agentId);
        if (accepted.length > 0) {
          hoBlock = "## In-Progress Tasks Assigned to You\n";
          for (const h of accepted) {
            const fromAgent = await subServices.orgService.getAgent(h.fromAgentId);
            const fromName = fromAgent?.name || h.fromAgentId;
            hoBlock += `- **From ${fromName}** (handoffId: ${h.id}): ${h.summary}\n`;
            if ((h as any).expectReport) {
              hasExpectReport = true;
            }
          }
        }
      }

      // Inject mandatory reporting instruction if any task requires it
      if (hasExpectReport) {
        hoBlock += "\n> **[SYSTEM MANDATORY]** One or more tasks above require you to report results back. " +
          "After completing them, you MUST call `hiveweave__message_superior` with a summary of your results. " +
          "This is a mandatory requirement from your coordinator.\n";
      }

      // Check inbox for rework requests or other messages from coordinator
      let reworkBlock = "";
      try {
        const inboxMsgs = await subServices.inboxService.getPendingMessages(agentId);
        if (inboxMsgs.length > 0) {
          const reworkMsgs = inboxMsgs.filter((m: any) =>
            m.message && m.message.includes("[REWORK REQUESTED]"),
          );
          const otherMsgs = inboxMsgs.filter((m: any) =>
            !m.message || !m.message.includes("[REWORK REQUESTED]"),
          );

          if (reworkMsgs.length > 0) {
            reworkBlock = "## ⚠️ WORK REJECTED — Rework Required\n";
            reworkBlock += "Your coordinator has reviewed your work and requested changes. Address the feedback below FIRST, then use report_completion and message_superior when done.\n\n";
            for (const msg of reworkMsgs) {
              const fromAgent = await subServices.orgService.getAgent(msg.fromAgentId);
              const fromName = fromAgent?.name || msg.fromAgentId;
              reworkBlock += `- **From ${fromName}**: "${msg.message}"\n`;
            }
            reworkBlock += "\n> **[SYSTEM MANDATORY]** You MUST address the feedback above and re-submit your work using report_completion + message_superior.\n";
          }

          if (otherMsgs.length > 0) {
            reworkBlock += "\n## Other Messages\n";
            for (const msg of otherMsgs) {
              const fromAgent = await subServices.orgService.getAgent(msg.fromAgentId);
              const fromName = fromAgent?.name || msg.fromAgentId;
              reworkBlock += `- **From ${fromName}**: "${msg.message}"\n`;
            }
          }

          // Mark all inbox messages as read
          await subServices.inboxService.markAsRead(agentId);
        }
      } catch { /* proceed without */ }

      const subTimeBlock = await buildTimeBlock(subAgent.projectId);
      const subGoalsBlock = await buildGoalsBlock(subAgent.projectId);
      const subWorkspaceBlock = await buildWorkspaceBlock(subAgent.projectId, subAgent.role);

      // Core team roster for subordinate (same as main handler)
      let subRosterBlock = "";
      if (subAgent.projectId) {
        try {
          const ceo = await subServices.orgService.findAgentByRole(subAgent.projectId, "ceo");
          const hr = await subServices.orgService.findAgentByRole(subAgent.projectId, "hr");
          const qa = await subServices.orgService.findAgentByRole(subAgent.projectId, "qa_engineer");
          const core = [ceo, hr, qa].filter(Boolean);
          if (core.length > 0) {
            subRosterBlock = "## Core Team (always available)\n";
            for (const m of core) {
              const isYou = m!.id === agentId ? " ← YOU" : "";
              const roleLabel = m!.role === "ceo" ? "CEO — final decision-maker, owns charter and org structure" :
                m!.role === "hr" ? "HR — creates, transfers, and dismisses agents; manages personnel" :
                "QA Engineer — code review, security audit, test analysis, performance audit";
              subRosterBlock += `- **${m!.name}** (${m!.shortId || m!.id.slice(0, 8)}) — ${roleLabel}${isYou}\n`;
            }
            subRosterBlock += "\nOther team members can be found via the `read_roster` tool.";
          }
        } catch {}
      }

      // Cache-optimized split: static before history, dynamic after
      const subStaticContext = [subGoalsBlock, subRosterBlock, subWorkspaceBlock].filter(Boolean).join("\n\n");
      const subDynamicContext = [subTimeBlock, ctxBlock, reworkBlock, hoBlock].filter(Boolean).join("\n\n");
      const subFullContext = [subStaticContext, subDynamicContext].filter(Boolean).join("\n\n");

      // Build identity prompt first to calculate token budget for history
      const subIsLeadership = subAgent.role.toLowerCase() === "ceo" || subAgent.role.toLowerCase() === "hr";
      const subOperatorName = await getOperatorName();
      const subIdentityPrompt = buildIdentityPrompt({
        agentName: subAgent.name,
        role: subAgent.role,
        permissionType: subAgent.permissionType as "coordinator" | "executor",
        goal: subAgent.goal,
        backstory: subAgent.backstory,
        includeParadigmCatalog: subIsLeadership,
        hasBindingTools: subIsLeadership,
        operatorName: subOperatorName,
      });

      // Determine the user message for budget calculation
      const userMsg = reworkBlock
        ? "Your coordinator has rejected your work and provided feedback. Please review the rework instructions carefully, address the feedback, and re-submit using report_completion + message_superior when done."
        : pending.length > 0
          ? hasExpectReport
            ? "You have new pending tasks from your coordinator. Some tasks REQUIRE you to report results back via message_superior when done. Review them carefully and follow all instructions."
            : "You have new pending tasks from your coordinator. Please review them carefully and work on them. Use write_work_log to document progress and report_completion when finished."
          : "Check your current tasks and continue working on them. Use write_work_log to document progress and report_completion when finished.";

      // Load conversation history — token budget derived from model's context window
      const subBudget = calculateHistoryBudget(subModel.contextWindow, subModel.maxOutputTokens, subIdentityPrompt, subFullContext, userMsg);
      const subHistory = await conversationStore.getHistory(agentId, subBudget, subServices.projectDb);

      // Prefix hash drift detection (subordinate)
      const subHash = computePrefixHash(subIdentityPrompt, subAgent.role + ":" + subAgent.permissionType);
      const subPrevHash = prefixHashTracker.get(agentId);
      if (subPrevHash && subPrevHash !== subHash) {
        fastify.log.warn(`[PrefixCache] DRIFT for subordinate ${subAgent.name} (${agentId.slice(0, 8)}): ${subPrevHash} → ${subHash}`);
      }
      prefixHashTracker.set(agentId, subHash);

      // Create per-project ToolExecutor for subordinate (pass model for QA review tools)
      const subToolExec = makeToolExecutor(subServices, subAgent.projectId, subModel);

      const runtime = new AgentRuntime({
        agentId,
        agentName: subAgent.name,
        role: subAgent.role,
        permissionType: subAgent.permissionType as "coordinator" | "executor",
        goal: subAgent.goal,
        backstory: subAgent.backstory,
        systemPrompt: "", // unused in new mode
        identityPrompt: subIdentityPrompt,
        contextPrompt: subStaticContext || undefined,
        dynamicContextPrompt: subDynamicContext || undefined,
        history: subHistory as Message[],
        operatorName: subOperatorName,
        baseUrl: subModel.baseUrl,
        model: subModel.modelId,
        provider: subModel.provider,
        supportsImages: subModel.supportsImages,
        apiKey: subModel.apiKey,
        contextWindow: subModel.contextWindow,
        maxOutputTokens: subModel.maxOutputTokens,
        limitInput: subModel.limitInput,
        respectsInlineCacheHints: subModel.respectsInlineCacheHints,
        temperature: subModel.temperature,
        reasoningEffort: subModel.reasoningEffort,
        sessionId: randomUUID(),
        toolExecutor: subToolExec,
        messagePoller: createMessagePoller(agentId, subServices),
        compactor: makeMidTurnCompactor(subModel, agentId, subServices.projectDb),
        toolOutputStore,
      });

      let calledMessageSuperior = false;
      let calledReportCompletion = false;
      let subFinalMessages: StoredMessage[] = [];
      const subDispatchedSubordinates: Array<{ toAgentId: string; description: string }> = [];
      const subPeersMessaged: Array<string> = [];
      let subFullText = "";
      const subToolCalls: Array<{ tool: string; input: Record<string, any> }> = [];

      // Activity buffering for auto-trigger (same as main chat handler)
      let subThinkBuf = "";
      let subTextBuf = "";
      let subActTimer: ReturnType<typeof setTimeout> | null = null;
      const flushSubActivity = () => {
        if (subActTimer) { clearTimeout(subActTimer); subActTimer = null; }
        if (subThinkBuf) {
          statusEventBus.emitActivity({ agentId, agentName: subAgent.name, type: "thinking", content: subThinkBuf, timestamp: Date.now() });
          subThinkBuf = "";
        }
        if (subTextBuf) {
          statusEventBus.emitActivity({ agentId, agentName: subAgent.name, type: "text", content: subTextBuf, timestamp: Date.now() });
          subTextBuf = "";
        }
      };

      for await (const event of runtime.chat(prefixTriggerForProject(subAgent.projectId, userMsg))) {
        if (event.type === "thinking") {
          subThinkBuf += event.content;
          subActTimer = setTimeout(flushSubActivity, 300);
        } else if (event.type === "text") {
          flushSubActivity();
          subTextBuf += event.content;
          subActTimer = setTimeout(flushSubActivity, 300);
          subFullText += event.content;
        } else if (event.type === "tool_use") {
          flushSubActivity();
          const tool = event.content.replace(/^hiveweave__/, "");
          subToolCalls.push({ tool, input: event.metadata?.input || {} });
          statusEventBus.emitActivity({ agentId, agentName: subAgent.name, type: "tool_use", toolName: tool, toolInput: JSON.stringify(event.metadata?.input || {}).slice(0, 200), timestamp: Date.now() });
        } else if (event.type === "tool_result") {
          const tool = (event.content || "").replace(/^hiveweave__/, "");
          statusEventBus.emitActivity({ agentId, agentName: subAgent.name, type: "tool_result", toolName: tool, toolResult: String(event.metadata?.result || "").slice(0, 300), timestamp: Date.now() });
        } else if (event.type === "done") {
          flushSubActivity();
        }
        // Detect message_superior calls so we can trigger coordinator auto-reply
        if (event.type === "tool_use" && event.content === "hiveweave__message_superior") {
          calledMessageSuperior = true;
        }
        // Detect report_completion so coordinator is notified of task completion
        if (event.type === "tool_use" && event.content === "hiveweave__report_completion") {
          calledReportCompletion = true;
        }
        // Detect dispatch_task so sub-subordinates are auto-triggered
        if (event.type === "tool_use" && event.content === "hiveweave__dispatch_task" && event.metadata?.input) {
          const inp = event.metadata.input as Record<string, any>;
          if (inp.toAgentId) {
            subDispatchedSubordinates.push({
              toAgentId: inp.toAgentId,
              description: inp.description || "",
            });
          }
        }
        // Immediately trigger non-user recipients of send_message
        if (event.type === "tool_use" && event.content === "hiveweave__send_message" && event.metadata?.input) {
          const inp = event.metadata.input as Record<string, any>;
          const recipients = String(inp.recipients || inp.toAgentId || "").split(",").map((s: string) => s.trim()).filter(Boolean);
          for (const rcpt of recipients) {
            if (rcpt.toLowerCase() === "user") continue;
            try {
              const target = await subServices.orgService.resolveAgent(rcpt);
              if (target) {
                if (target.permissionType === "coordinator") {
                  triggerCoordinator(target.id).catch(() => {});
                } else {
                  triggerSubordinate(target.id).catch(() => {});
                }
                if (!subPeersMessaged.includes(target.id)) {
                  subPeersMessaged.push(target.id);
                }
              }
            } catch { /* resolution failed */ }
          }
        }
        // Capture final messages for history storage
        if (event.type === "done" && event.metadata?.messages) {
          subFinalMessages = event.metadata.messages as StoredMessage[];
        }
      }

      // Store conversation history — persists to DB and trims to token budget
      if (subFinalMessages.length > 0) {
        await conversationStore.appendTurn(agentId, subFinalMessages, subBudget, subServices.projectDb);
      }

      // Finalize subordinate's background message (visible in team comms panel)
      await subServices.chatMessageService.updateMessage(bgAssistantId, {
        content: subFullText.trim(),
        toolCalls: JSON.stringify(subToolCalls),
        isStreaming: false,
      });

      // If subordinate called message_superior, mark its handoffs as reported up
      // so the self-check in triggerCoordinator won't re-inject the reporting instruction
      if (calledMessageSuperior) {
        const marked = await subServices.handoffService.markReportedUp(agentId);
        if (marked > 0) {
          fastify.log.info(`Subordinate ${subAgent.name}: marked ${marked} handoff(s) as reportedUp`);
        }
      }

      // If subordinate sent a message to superior OR completed a task, trigger coordinator auto-reply
      if ((calledMessageSuperior || calledReportCompletion) && subAgent.parentId) {
        fastify.log.info(`Subordinate ${subAgent.name} ${calledMessageSuperior ? "sent message_superior" : "reported completion"}, triggering coordinator ${subAgent.parentId}`);
        triggerCoordinator(subAgent.parentId).catch((err) =>
          fastify.log.error(err, `Coordinator auto-trigger failed for ${subAgent.parentId}`),
        );
      }

      // Auto-trigger sub-subordinates that were dispatched during this run (recursive cascade)
      if (subDispatchedSubordinates.length > 0) {
        for (const sub of subDispatchedSubordinates) {
          if (runningAutoTriggers.has(sub.toAgentId)) {
            fastify.log.info(`Sub-dispatch auto-trigger skipped: ${sub.toAgentId} already running`);
            continue;
          }
          fastify.log.info(`Sub-dispatch auto-trigger: ${subAgent.name} dispatched to ${sub.toAgentId}`);
          triggerSubordinate(sub.toAgentId).catch((err) =>
            fastify.log.error(err, `Sub-dispatch auto-trigger failed for ${sub.toAgentId}`),
          );
        }
      }

      // Auto-trigger peers that this subordinate messaged
      if (subPeersMessaged.length > 0) {
        for (const peerId of subPeersMessaged) {
          if (runningAutoTriggers.has(peerId)) {
            fastify.log.info(`Subordinate-peer auto-trigger skipped: ${peerId} already running`);
            continue;
          }
          fastify.log.info(`Subordinate-peer auto-trigger: ${subAgent.name} messaged peer ${peerId}`);
          triggerCoordinator(peerId).catch((err) =>
            fastify.log.error(err, `Peer auto-trigger failed for ${peerId}`),
          );
        }
      }
    } finally {
      statusEventBus.emitActivity({ agentId, agentName: "Agent", type: "done", timestamp: Date.now() });
      statusEventBus.setProcessing(agentId, false);
      runningAutoTriggers.delete(agentId);
    }
  }

  // ---------------------------------------------------------------------------
  // Auto-trigger: coordinator auto-reply to subordinate inbox messages
  // ---------------------------------------------------------------------------

  async function triggerCoordinator(coordinatorIdOrShort: string): Promise<void> {
    // Resolve shortId or UUID prefix to full UUID via registry or scan
    let resolved: any = null;
    let coordServices: NonNullable<Awaited<ReturnType<typeof getProjectServices>>> | null = null;

    const wsPath = lookupAgentWorkspace(coordinatorIdOrShort);
    if (wsPath) {
      const allProjects = await projectService.listProjects();
      const project = allProjects.find((p) => p.workspacePath === wsPath);
      if (project) {
        const projectDb = ensureProjectDb(wsPath);
        const orgSvc = new OrgService(projectDb, wsPath);
        const agent = await orgSvc.resolveAgent(coordinatorIdOrShort);
        if (agent) {
          resolved = agent;
          coordServices = {
            project, projectDb,
            orgService: orgSvc,
            memoryService: new MemoryService(projectDb),
            dispatchService: new DispatchService(projectDb),
            handoffService: new HandoffService(projectDb),
            inboxService: new InboxService(projectDb),
            ...createChatServices(projectDb),
            rosterService: new RosterService(projectDb),
            fileService: new FileService(),
            permissionService: new PermissionService(projectDb),
            approvalService: new ApprovalService(projectDb),
          };
        }
      }
    }

    if (!resolved) {
      const allProjects = await projectService.listProjects();
      for (const proj of allProjects) {
        if (!proj.workspacePath) continue;
        try {
          const projectDb = ensureProjectDb(proj.workspacePath);
          const orgSvc = new OrgService(projectDb, proj.workspacePath);
          const agent = await orgSvc.resolveAgent(coordinatorIdOrShort);
          if (agent) {
            resolved = agent;
            coordServices = {
              project: proj, projectDb,
              orgService: orgSvc,
              memoryService: new MemoryService(projectDb),
              dispatchService: new DispatchService(projectDb),
              handoffService: new HandoffService(projectDb),
              inboxService: new InboxService(projectDb),
              ...createChatServices(projectDb),
              rosterService: new RosterService(projectDb),
              fileService: new FileService(),
              permissionService: new PermissionService(projectDb),
              approvalService: new ApprovalService(projectDb),
            };
            break;
          }
        } catch { continue; }
      }
    }

    if (!resolved || !coordServices) {
      fastify.log.warn(`triggerCoordinator: cannot resolve agent "${coordinatorIdOrShort}"`);
      return;
    }
    const coordinatorId = resolved.id;
    if (runningAutoTriggers.has(coordinatorId)) {
      return; // Already running
    }
    runningAutoTriggers.add(coordinatorId);
    statusEventBus.setProcessing(coordinatorId, true);
    const coordAssistantId = randomUUID();
    try {
      const coordAgent = resolved;
      statusEventBus.emitActivity({ agentId: coordinatorId, agentName: coordAgent.name, type: "thinking", content: "Auto-triggered", timestamp: Date.now() });

      await coordServices.chatMessageService.saveMessage({
        id: coordAssistantId,
        agentId: coordinatorId,
        role: "assistant",
        content: "",
        toolCalls: "[]",
        isBackground: true,
        isRead: true,
        isStreaming: true,
        createdAt: Date.now(),
      });

      // Resolve model for this coordinator agent
      let coordModel: ResolvedModel;
      try {
        coordModel = await resolveModelForAgent(coordinatorId);
      } catch {
        fastify.log.warn(`triggerCoordinator: no model configured for ${coordAgent.name}, skipping`);
        return;
      }

      // Check for pending inbox messages
      const pendingMsgs = await coordServices.inboxService.getPendingMessages(coordinatorId);
      if (pendingMsgs.length === 0) {
        await coordServices.chatMessageService.updateMessage(coordAssistantId, {
          content: "",
          isStreaming: false,
        });
        return;
      }

      for (const msg of pendingMsgs) {
        await coordServices.teamChatService.recordIncoming(coordinatorId, msg.fromAgentId, msg.message, msg.id);
      }

      // Track communication: each subordinate who sent a message -> coordinator
      for (const msg of pendingMsgs) {
        communicationService.addCommunication(msg.fromAgentId, coordinatorId, "message");
      }

      // Build context block
      let ctxBlock = "";
      try {
        ctxBlock = await coordServices.memoryService.buildAgentContext(coordinatorId, coordAgent.moduleId || undefined);
      } catch { /* proceed without */ }

      // Build subordinate logs block
      let subLogsBlock = "";
      try {
        const children = await coordServices.orgService.getChildren(coordinatorId);
        if (children.length > 0) {
          subLogsBlock += "## Your Subordinates\n";
          for (const child of children) {
            subLogsBlock += `- **${child.name}** (${child.role}, ID: ${child.id})\n`;
          }
        }
        for (const child of children) {
          const logs = await coordServices.dispatchService.getSubordinateLogs(child.id, 5);
          if (logs.length > 0) {
            subLogsBlock += `\n### Recent logs from ${child.name} (${child.role}, ID: ${child.id}):\n`;
            for (const log of logs) {
              subLogsBlock += `- [${log.type}] ${log.summary}\n`;
            }
          }
        }
      } catch { /* proceed without */ }

      // Build handoff block
      let hoBlock = "";
      try {
        // Show completed handoffs from subordinates
        const children = await coordServices.orgService.getChildren(coordinatorId);
        let completedBlock = "";
        for (const child of children) {
          const completed = await coordServices.handoffService.getCompletedFromSubordinate(coordinatorId, child.id, 3);
          if (completed.length > 0) {
            completedBlock += `\n### Completed tasks by ${child.name}:\n`;
            for (const h of completed) {
              completedBlock += `- [completed] ${h.summary}\n`;
            }
          }
        }
        if (completedBlock) {
          hoBlock = "## Task Handoff Results from Subordinates\n" + completedBlock;
        }
      } catch { /* proceed without */ }

      // Build inbox block — separate subordinate reports from peer messages
      const superiorMsgs = pendingMsgs.filter((m: any) => m.messageType !== "peer");
      const peerMsgs = pendingMsgs.filter((m: any) => m.messageType === "peer");

      let hasPeerExpectReport = false;

      let inboxBlock = "";
      if (superiorMsgs.length > 0) {
        inboxBlock += "## Messages from Subordinates\n";
        inboxBlock += "Your subordinates have sent you the following messages. Please address them.\n\n";
        for (const msg of superiorMsgs) {
          const fromAgent = await coordServices.orgService.getAgent(msg.fromAgentId);
          const fromName = fromAgent?.name || msg.fromAgentId;
          const reportTag = (msg as any).expectReport ? " **[REPLY REQUIRED]**" : "";
          inboxBlock += `- **${fromName}** asks${reportTag}: "${msg.message}"\n`;
        }
      }
      if (peerMsgs.length > 0) {
        inboxBlock += "\n## Messages from Peers\n";
        inboxBlock += "Peer agents have sent you the following messages. Respond if appropriate.\n\n";
        for (const msg of peerMsgs) {
          const fromAgent = await coordServices.orgService.getAgent(msg.fromAgentId);
          const fromName = fromAgent?.name || msg.fromAgentId;
          const reportTag = (msg as any).expectReport ? " **[REPLY REQUIRED]**" : "";
          inboxBlock += `- **${fromName}** says${reportTag}: "${msg.message}"\n`;
          if ((msg as any).expectReport) {
            hasPeerExpectReport = true;
          }
        }
        if (hasPeerExpectReport) {
          inboxBlock += "\n> **[SYSTEM MANDATORY]** Some peer messages above require your reply. " +
            "You MUST respond to those peers using `hiveweave__send_message` before finishing.\n";
        }
      }

      // Self-check: does this coordinator have a task from its own superior that requires reporting?
      // Only inject the instruction if there are unreported handoffs (already reported = no re-remind)
      let selfReportBlock = "";
      if (coordAgent.parentId) {
        const needsReport = await coordServices.handoffService.getUnreportedAcceptedHandoffs(coordinatorId);
        if (needsReport.length > 0) {
          selfReportBlock = "> **[SYSTEM MANDATORY]** You have an assigned task that requires you to report results back to your superior. " +
            "After processing the information above, you MUST call `hiveweave__message_superior` with a summary of the results. " +
            "This is a mandatory requirement. " +
            "IMPORTANT: Only report NEW information. If you have already reported and have nothing new, do NOT call message_superior again.\n";
        }
      }

      const coordTimeBlock = await buildTimeBlock(coordAgent.projectId);
      const coordGoalsBlock = await buildGoalsBlock(coordAgent.projectId);
      const coordWorkspaceBlock = await buildWorkspaceBlock(coordAgent.projectId, coordAgent.role);

      // Core team roster for coordinator (same as main handler)
      let coordRosterBlock = "";
      if (coordAgent.projectId) {
        try {
          const ceo = await coordServices.orgService.findAgentByRole(coordAgent.projectId, "ceo");
          const hr = await coordServices.orgService.findAgentByRole(coordAgent.projectId, "hr");
          const qa = await coordServices.orgService.findAgentByRole(coordAgent.projectId, "qa_engineer");
          const core = [ceo, hr, qa].filter(Boolean);
          if (core.length > 0) {
            coordRosterBlock = "## Core Team (always available)\n";
            for (const m of core) {
              const isYou = m!.id === coordinatorId ? " ← YOU" : "";
              const roleLabel = m!.role === "ceo" ? "CEO — final decision-maker, owns charter and org structure" :
                m!.role === "hr" ? "HR — creates, transfers, and dismisses agents; manages personnel" :
                "QA Engineer — code review, security audit, test analysis, performance audit";
              coordRosterBlock += `- **${m!.name}** (${m!.shortId || m!.id.slice(0, 8)}) — ${roleLabel}${isYou}\n`;
            }
            coordRosterBlock += "\nOther team members can be found via the `read_roster` tool. If you are a manager, use `list_subordinates` to see your direct reports.";
          }
        } catch {}
      }

      // Cache-optimized split: static before history, dynamic after
      const coordStaticContext = [coordGoalsBlock, coordRosterBlock, coordWorkspaceBlock].filter(Boolean).join("\n\n");
      const coordDynamicContext = [coordTimeBlock, ctxBlock, subLogsBlock, hoBlock, inboxBlock, selfReportBlock].filter(Boolean).join("\n\n");
      const coordFullContext = [coordStaticContext, coordDynamicContext].filter(Boolean).join("\n\n");

      // Build identity prompt first to calculate token budget for history
      const coordOperatorName = await getOperatorName();
      const coordIdentityPrompt = buildIdentityPrompt({
        agentName: coordAgent.name,
        role: coordAgent.role,
        permissionType: coordAgent.permissionType as "coordinator" | "executor",
        goal: coordAgent.goal,
        backstory: coordAgent.backstory,
        operatorName: coordOperatorName,
      });

      const userMsg = "You have received new messages from your subordinates and/or peers. Please review them and respond appropriately. Use your tools to check work logs and provide guidance. For peer messages, respond if collaboration is needed.";

      // Load conversation history — token budget derived from model's context window
      const coordBudget = calculateHistoryBudget(coordModel.contextWindow, coordModel.maxOutputTokens, coordIdentityPrompt, coordFullContext, userMsg);
      const coordHistory = await conversationStore.getHistory(coordinatorId, coordBudget, coordServices.projectDb);

      // Prefix hash drift detection (coordinator)
      const coordHash = computePrefixHash(coordIdentityPrompt, coordAgent.role + ":" + coordAgent.permissionType);
      const coordPrevHash = prefixHashTracker.get(coordinatorId);
      if (coordPrevHash && coordPrevHash !== coordHash) {
        fastify.log.warn(`[PrefixCache] DRIFT for coordinator ${coordAgent.name} (${coordinatorId.slice(0, 8)}): ${coordPrevHash} → ${coordHash}`);
      }
      prefixHashTracker.set(coordinatorId, coordHash);

      // Create per-project ToolExecutor for coordinator
      const coordToolExec = makeToolExecutor(coordServices, coordAgent.projectId);

      const runtime = new AgentRuntime({
        agentId: coordinatorId,
        agentName: coordAgent.name,
        role: coordAgent.role,
        permissionType: coordAgent.permissionType as "coordinator" | "executor",
        goal: coordAgent.goal,
        backstory: coordAgent.backstory,
        systemPrompt: "", // unused in new mode
        identityPrompt: coordIdentityPrompt,
        contextPrompt: coordStaticContext || undefined,
        dynamicContextPrompt: coordDynamicContext || undefined,
        history: coordHistory as Message[],
        operatorName: coordOperatorName,
        baseUrl: coordModel.baseUrl,
        model: coordModel.modelId,
        provider: coordModel.provider,
        supportsImages: coordModel.supportsImages,
        apiKey: coordModel.apiKey,
        contextWindow: coordModel.contextWindow,
        maxOutputTokens: coordModel.maxOutputTokens,
        limitInput: coordModel.limitInput,
        respectsInlineCacheHints: coordModel.respectsInlineCacheHints,
        temperature: coordModel.temperature,
        reasoningEffort: coordModel.reasoningEffort,
        sessionId: randomUUID(),
        toolExecutor: coordToolExec,
        messagePoller: createMessagePoller(coordinatorId, coordServices),
        compactor: makeMidTurnCompactor(coordModel, coordinatorId, coordServices.projectDb),
        toolOutputStore,
      });

      let fullText = "";
      const toolCalls: Array<{ tool: string; input: Record<string, any> }> = [];
      let coordFinalMessages: StoredMessage[] = [];
      let coordCalledMessageSuperior = false;
      const coordDispatchedSubordinates: Array<{ toAgentId: string; description: string }> = [];
      const coordPeersMessaged: Array<string> = [];

      for await (const event of runtime.chat(prefixTriggerForProject(coordAgent.projectId, userMsg))) {
        if (event.type === "text") {
          fullText += event.content;
        } else if (event.type === "tool_use") {
          const tool = event.content.replace(/^hiveweave__/, "");
          toolCalls.push({ tool, input: event.metadata?.input || {} });
          // Detect message_superior so we can cascade to the parent coordinator
          if (event.content === "hiveweave__message_superior") {
            coordCalledMessageSuperior = true;
          }
          // Detect dispatch_task so dispatched subordinates are auto-triggered
          if (event.content === "hiveweave__dispatch_task" && event.metadata?.input) {
            const inp = event.metadata.input as Record<string, any>;
            if (inp.toAgentId) {
              coordDispatchedSubordinates.push({
                toAgentId: inp.toAgentId,
                description: inp.description || "",
              });
            }
          }
          // Immediately trigger non-user recipients of send_message
          if (event.content === "hiveweave__send_message" && event.metadata?.input) {
            const inp = event.metadata.input as Record<string, any>;
            const recipients = String(inp.recipients || inp.toAgentId || "").split(",").map((s: string) => s.trim()).filter(Boolean);
            for (const rcpt of recipients) {
              if (rcpt.toLowerCase() === "user") continue;
              try {
                const target = await coordServices.orgService.resolveAgent(rcpt);
                if (target) {
                  if (target.permissionType === "coordinator") {
                    triggerCoordinator(target.id).catch(() => {});
                  } else {
                    triggerSubordinate(target.id).catch(() => {});
                  }
                  if (!coordPeersMessaged.includes(target.id)) {
                    coordPeersMessaged.push(target.id);
                  }
                }
              } catch { /* resolution failed */ }
            }
          }
        }
        // Capture final messages for history storage
        if (event.type === "done" && event.metadata?.messages) {
          coordFinalMessages = event.metadata.messages as StoredMessage[];
        }
      }

      // Store conversation history — persists to DB and trims to token budget
      if (coordFinalMessages.length > 0) {
        await conversationStore.appendTurn(coordinatorId, coordFinalMessages, coordBudget, coordServices.projectDb);
      }

      // Mark inbox messages as read
      await coordServices.inboxService.markAsRead(coordinatorId);

      // If coordinator called message_superior, mark its handoffs as reportedUp
      // to prevent the self-check from re-injecting the reporting instruction on next trigger
      if (coordCalledMessageSuperior) {
        const marked = await coordServices.handoffService.markReportedUp(coordinatorId);
        if (marked > 0) {
          fastify.log.info(`Coordinator ${coordAgent.name}: marked ${marked} handoff(s) as reportedUp`);
        }
      }

      // Finalize coordinator's reply in the main chat area
      await coordServices.chatMessageService.updateMessage(coordAssistantId, {
        content: fullText.trim(),
        toolCalls: JSON.stringify(toolCalls),
        isStreaming: false,
      });

      // Cascade: if this coordinator sent message_superior, trigger the parent coordinator
      if (coordCalledMessageSuperior && coordAgent.parentId) {
        fastify.log.info(`Coordinator ${coordAgent.name} sent message_superior, triggering parent coordinator ${coordAgent.parentId}`);
        triggerCoordinator(coordAgent.parentId).catch((err) =>
          fastify.log.error(err, `Parent coordinator auto-trigger failed for ${coordAgent.parentId}`),
        );
      }

      // Cascade: auto-trigger subordinates that this coordinator dispatched
      if (coordDispatchedSubordinates.length > 0) {
        for (const sub of coordDispatchedSubordinates) {
          if (runningAutoTriggers.has(sub.toAgentId)) {
            fastify.log.info(`Coordinator-dispatch auto-trigger skipped: ${sub.toAgentId} already running`);
            continue;
          }
          fastify.log.info(`Coordinator-dispatch auto-trigger: ${coordAgent.name} dispatched to ${sub.toAgentId}`);
          triggerSubordinate(sub.toAgentId).catch((err) =>
            fastify.log.error(err, `Coordinator-dispatch auto-trigger failed for ${sub.toAgentId}`),
          );
        }
      }

      // Cascade: auto-trigger peers that this coordinator messaged
      if (coordPeersMessaged.length > 0) {
        for (const peerId of coordPeersMessaged) {
          if (runningAutoTriggers.has(peerId)) {
            fastify.log.info(`Peer auto-trigger skipped: ${peerId} already running`);
            continue;
          }
          fastify.log.info(`Peer auto-trigger: ${coordAgent.name} messaged peer ${peerId}`);
          triggerCoordinator(peerId).catch((err) =>
            fastify.log.error(err, `Peer auto-trigger failed for ${peerId}`),
          );
        }
      }

    } finally {
      statusEventBus.emitActivity({ agentId: coordinatorId, agentName: "Coordinator", type: "done", timestamp: Date.now() });
      statusEventBus.setProcessing(coordinatorId, false);
      runningAutoTriggers.delete(coordinatorId);
    }
  }

  registerAlarmTrigger(triggerSubordinate);
}
