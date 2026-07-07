import { Socket, Channel } from "phoenix";

/**
 * Phoenix.js WebSocket-based API client.
 *
 * This replaces the old SSE-based client (fetch + EventSource).
 * The Elixir backend now uses Phoenix Channels over WebSocket.
 *
 * Channels used:
 *   - "lobby:status"  - global agent processing status
 *   - "agent:<id>"    - per-agent chat stream + status + inbox
 *   - "project:<id>"  - per-project game time + status
 */

// ---------------------------------------------------------------------------
// Socket setup
// ---------------------------------------------------------------------------

const SOCKET_URL =
  (import.meta.env.VITE_WS_URL as string | undefined) ||
  (typeof window !== "undefined" && window.location.hostname === "localhost"
    ? "ws://localhost:4000/socket"
    : "/socket");

let _socket: Socket | null = null;

// Persistent per-agent channels: one joined channel per agent, reused across
// messages.  This prevents the backend's join/3 from calling
// Phoenix.PubSub.subscribe multiple times (which would duplicate event delivery).
// IMPORTANT: Store on globalThis to survive Vite HMR module reloads.
// Without this, HMR resets these Maps to empty, causing streamChat to create
// duplicate agent channels — each receiving the same stream_chunk events,
// resulting in "结巴" (stutter/duplication) in the streaming display.
const _agentChannels: Map<string, any> = (globalThis as any).__hw_agentChannels ?? new Map();
(globalThis as any).__hw_agentChannels = _agentChannels;
const _agentHandlers: Map<string, (event: ChatEvent) => void> = (globalThis as any).__hw_agentHandlers ?? new Map();
(globalThis as any).__hw_agentHandlers = _agentHandlers;

export function getSocket(): Socket {
  // Use globalThis to survive Vite HMR — without this, HMR resets _socket
  // to null, creating a second WebSocket connection while the old one stays
  // alive. Two sockets = two agent channels = duplicate stream_chunk events.
  if (!(globalThis as any).__hw_socket) {
    const params: Record<string, string> = {};
    if (_apiKey) params.api_key = _apiKey;
    const socket = new Socket(SOCKET_URL, {
      params,
      reconnectAfterMs: (tries: number) => [1000, 2000, 5000, 10000][tries - 1] ?? 10000,
      heartbeatIntervalMs: 30_000,
    });
    socket.connect();
    (globalThis as any).__hw_socket = socket;
    _socket = socket;
  }
  return (globalThis as any).__hw_socket as Socket;
}

// ---------------------------------------------------------------------------
// REST helpers
// ---------------------------------------------------------------------------

const BASE = "/api";

let _apiKey: string | null = null;

export function setApiKey(key: string | null) {
  _apiKey = key;
}

// Debug log helper — writes to Zustand store without circular import
function dbg(category: "api" | "ws" | "error" | "info" | "state", message: string, data?: any) {
  try {
    // Dynamic import would be async; use getState directly via a lazy ref
    const store = (window as any).__hwStore;
    if (store) store.getState().addDebugLog({ category, message, data });
  } catch { /* noop */ }
}

async function fetchJSON<T = any>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (_apiKey && !headers.has("x-api-key")) {
    headers.set("x-api-key", _apiKey);
  }
  const method = init?.method || "GET";
  const t0 = performance.now();
  dbg("api", `${method} ${url}`, { method, url, body: init?.body });
  try {
    const res = await fetch(url, { ...init, headers });
    const elapsed = Math.round(performance.now() - t0);
    const text = await res.text();
    if (!res.ok) {
      dbg("error", `${method} ${url} → ${res.status} (${elapsed}ms)`, { status: res.status, body: text.slice(0, 500) });
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const parsed = !text || text.length === 0 ? {} : JSON.parse(text);
    dbg("api", `${method} ${url} → ${res.status} (${elapsed}ms)`, { status: res.status, bodyPreview: text.slice(0, 300) });
    return parsed as T;
  } catch (e: any) {
    // BUG-006/018 修复：AbortError 不返回 null（会导致 caller 误设空数据），
    // 也不污染 console。重新 throw 带 _aborted 标记，让 caller 静默处理。
    if (e?.name === "AbortError") {
      const abortErr = new Error("Aborted") as any;
      abortErr.name = "AbortError";
      abortErr._aborted = true;
      throw abortErr;
    }
    const elapsed = Math.round(performance.now() - t0);
    dbg("error", `${method} ${url} FAILED (${elapsed}ms): ${e.message}`, { error: e.message });
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export interface Project {
  id: string;
  name: string;
  workspacePath?: string | null;
  description?: string | null;
  orgParadigm?: string | null;
  language?: string | null;
  createdAt: number;
}

export interface KeyResult {
  text: string;
  status: "todo" | "doing" | "done";
  owner?: string;
}

export interface GoalsData {
  objective: string;
  focus: string;
  keyResults: KeyResult[];
  userInvolvement?: string;
}

export async function getProjects(): Promise<Project[]> {
  const data = await fetchJSON<{ projects: Project[] }>(`${BASE}/projects`);
  return data.projects || [];
}


export interface WorkspaceCleanupResult {
  status: "ok" | "skipped" | "scheduled" | "failed";
  hiveweaveDir?: string | null;
  workspacePath?: string | null;
  reason?: "shared" | "no_workspace" | "skipped" | "error";
  sharedWith?: string[];
  pendingDir?: string;
}

export interface DeleteProjectResponse {
  ok: boolean;
  dbLeftover?: boolean;
  workspaceCleanup?: WorkspaceCleanupResult;
  warning?: string;
}

export async function createProject(name: string, workspacePath?: string, description?: string, orgParadigm?: string, language?: string) {
  return fetchJSON(`${BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, workspacePath, description, orgParadigm, language: language || "zh" }),
  });
}

export async function deleteProject(id: string): Promise<DeleteProjectResponse> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 120_000);
  try {
    return await fetchJSON<DeleteProjectResponse>(`${BASE}/projects/${id}`, {
      method: "DELETE",
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timer);
  }
}

export async function getProjectGameTime(projectId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/game-time`);
}

export async function getProjectGoals(projectId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/goals`);
}

export async function updateProjectGoals(projectId: string, goals: any) {
  return fetchJSON(`${BASE}/projects/${projectId}/goals`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goals }),
  });
}

// ---------------------------------------------------------------------------
// Org / Agents
// ---------------------------------------------------------------------------

export async function getOrgTree(projectId?: string) {
  const url = projectId ? `${BASE}/org?projectId=${projectId}` : `${BASE}/org`;
  return fetchJSON(url);
}

export async function getAgent(id: string) {
  const raw = await fetchJSON(`${BASE}/org/agents/${id}`);
  // Unwrap the {agent: ...} envelope once at the API layer so callers
  // don't need to repeat this logic. Backend always returns %{agent: serialize_agent(a)}.
  return (raw && typeof raw === "object" && "agent" in raw && raw.agent) ? raw.agent : raw;
}

export async function createAgent(data: any) {
  return fetchJSON(`${BASE}/org/agents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function updateAgent(id: string, data: any) {
  return fetchJSON(`${BASE}/org/agents/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function deleteAgent(id: string) {
  return fetchJSON(`${BASE}/org/agents/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Chat (HTTP trigger - actual stream via WebSocket)
// ---------------------------------------------------------------------------

export interface ChatEvent {
  type: "text" | "text_delta" | "thinking_delta" | "tool_use" | "tool_result" | "message_id" | "error" | "done" | "busy" | "approval_request" | "retry" | "queued_message";
  data: string;
  deltaId?: string;
}

export function streamChat(
  agentId: string,
  message: string,
  images: string[] | undefined,
  onEvent: (event: ChatEvent) => void
): { abort: () => void } {
  const socket = getSocket();
  dbg("ws", `streamChat called for ${agentId}`, { agentId, messageLen: message.length, socketConnected: (socket as any).isConnected?.() ?? false });

  if (!(globalThis as any).__hw_lastSeq) (globalThis as any).__hw_lastSeq = {};
  (globalThis as any).__hw_lastSeq[agentId] = 0;
  _agentHandlers.set(agentId, onEvent);

  let channel = _agentChannels.get(agentId);

  // Helper: push chat message to channel
  const pushChat = (ch: any) => {
    ch.push("chat", { message, images: images?.length ? images : undefined });
  };

  if (channel && channel.state === "joined") {
    dbg("ws", `push chat (channel already joined) for ${agentId}`);
    pushChat(channel);
  } else if (channel && channel.state === "joining") {
    // Channel is still joining — wait for join to complete, then push.
    // This fixes the bug where NewProjectDialog sends a message before
    // the WebSocket channel has finished joining.
    dbg("ws", `channel still joining for ${agentId}, waiting for join`);
    channel.join().receive("ok", () => {
      dbg("ws", `channel joined (deferred) for ${agentId}, pushing chat`);
      pushChat(channel);
    }).receive("error", (resp: any) => {
      dbg("error", `deferred channel join FAILED for ${agentId}`, resp);
      const handler = _agentHandlers.get(agentId);
      handler?.({ type: "error", data: JSON.stringify(resp) });
    });
  } else {
    if (channel) {
      dbg("ws", `channel state=${channel.state}, leaving old channel for ${agentId}`);
      try { channel.leave(); } catch {}
      _agentChannels.delete(agentId);
    }

    channel = socket.channel(`agent:${agentId}`);
    _agentChannels.set(agentId, channel);
    dbg("ws", `creating new channel agent:${agentId}`);

    bindAgentChannelEvents(channel, agentId);

    channel.join().receive("ok", () => {
      dbg("ws", `channel joined for ${agentId}, pushing chat`);
      pushChat(channel);
    }).receive("error", (resp: any) => {
      dbg("error", `channel join FAILED for ${agentId}`, resp);
      const handler = _agentHandlers.get(agentId);
      handler?.({ type: "error", data: JSON.stringify(resp) });
    });
  }

  return {
    abort: () => {
      dbg("ws", `abort called for ${agentId}`);
      channel?.push("cancel", {});
      _agentHandlers.delete(agentId);
    },
  };
}


function bindAgentChannelEvents(channel: any, agentId: string) {
  channel.on("init", () => { dbg("ws", `init event for ${agentId}`); });

  channel.on("message_id", (payload: any) => {
    dbg("ws", `message_id for ${agentId}`, payload);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "message_id", data: JSON.stringify(payload) });
  });

  channel.on("stream_chunk", (payload: any) => {
    const handler = _agentHandlers.get(agentId);
    if (!handler) return;
    const text = typeof payload === "string" ? payload : payload.text || "";
    if (typeof payload === "object" && payload.delta) {
      const deltaId = payload.deltaId || "";
      const seq = payload.seq;
      if (typeof seq === "number") {
        const lastSeq = (globalThis as any).__hw_lastSeq ?? {};
        const last = lastSeq[agentId] ?? 0;
        if (seq <= last) return;
        lastSeq[agentId] = seq;
        (globalThis as any).__hw_lastSeq = lastSeq;
      }
      if (payload.reasoning) {
        handler({ type: "thinking_delta", data: text, deltaId });
      } else {
        handler({ type: "text_delta", data: text, deltaId });
      }
    } else {
      dbg("ws", `stream_chunk (non-delta) for ${agentId}: ${text.slice(0, 100)}`);
      handler({ type: "text", data: text });
    }
  });

  channel.on("stream_tool", (payload: any) => {
    const handler = _agentHandlers.get(agentId);
    if (!handler) return;
    if (payload.type === "tool_use") {
      dbg("ws", `tool_use for ${agentId}: ${payload.toolName}`, payload);
      handler({ type: "tool_use", data: JSON.stringify(payload) });
    } else if (payload.type === "tool_result") {
      dbg("ws", `tool_result for ${agentId}: ${payload.toolName}`);
      handler({ type: "tool_result", data: JSON.stringify(payload) });
    }
  });

  channel.on("status_change", () => {
    dbg("ws", `status_change for ${agentId}`);
  });

  channel.on("done", () => {
    dbg("ws", `done event for ${agentId}`);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "done", data: "" });
  });

  channel.on("error", (payload: any) => {
    dbg("error", `error event for ${agentId}: ${payload?.message || "Unknown"}`, payload);
    const handler = _agentHandlers.get(agentId);
    handler?.({ type: "error", data: payload?.message || "Unknown error" });
  });
}

/**
 * Join (or create) a persistent agent channel without sending a message.
 * Used to warm up the WebSocket before onboarding / first chat.
 */
export function joinAgentChannel(agentId: string): Promise<void> {
  const socket = getSocket();

  const existing = _agentChannels.get(agentId);
  if (existing?.state === "joined") {
    return Promise.resolve();
  }
  if (existing?.state === "joining") {
    return new Promise((resolve, reject) => {
      existing.join().receive("ok", () => resolve()).receive("error", (resp: any) => reject(resp));
    });
  }

  if (existing) {
    try { existing.leave(); } catch {}
    _agentChannels.delete(agentId);
  }

  const channel = socket.channel(`agent:${agentId}`);
  _agentChannels.set(agentId, channel);
  bindAgentChannelEvents(channel, agentId);
  dbg("ws", `joinAgentChannel creating channel agent:${agentId}`);

  return new Promise((resolve, reject) => {
    channel.join().receive("ok", () => {
      dbg("ws", `joinAgentChannel joined for ${agentId}`);
      resolve();
    }).receive("error", (resp: any) => {
      dbg("error", `joinAgentChannel FAILED for ${agentId}`, resp);
      reject(resp);
    });
  });
}

/**
 * Explicitly leave an agent's persistent channel. Call this when the agent
 * is deleted or when you need to force a fresh channel on the next message.
 */
export function leaveAgentChannel(agentId: string) {
  const channel = _agentChannels.get(agentId);
  if (channel) {
    // BUG-034: 不再发送 cancel — 切换 agent 时不应停止后台 agent 的
    // LLM 流。Agent 会继续运行，用户可以随时切回来查看结果。
    try { channel.leave(); } catch {}
    _agentChannels.delete(agentId);
  }
  _agentHandlers.delete(agentId);
}

/**
 * Passively subscribe to an agent's stream events without sending a message.
 * Used when switching back to an agent that's still processing — we resume
 * receiving stream_chunk, stream_tool, done, and error events.
 *
 * Returns a cleanup function that unregisters the handler.
 */
export function subscribeAgentStream(
  agentId: string,
  onEvent: (event: ChatEvent) => void,
): () => void {
  _agentHandlers.set(agentId, onEvent);
  // Ensure the channel is joined (reuses existing joined channel, or joins fresh)
  joinAgentChannel(agentId).catch(() => {});
  return () => {
    _agentHandlers.delete(agentId);
  };
}

// ---------------------------------------------------------------------------
// Agent status subscription (was SSE-based before)
// ---------------------------------------------------------------------------

export interface ActivityEntry {
  agentId: string;
  agentName: string;
  type: string;
  content?: string;
  deltaId?: string;
  toolName?: string;
  // The Elixir backend sometimes forwards these as raw objects (from the
  // stream_event) and sometimes as JSON strings (from the activity broadcast).
  // Renderers must handle both shapes.
  toolInput?: string | object;
  toolResult?: string | object;
  errorMessage?: string;
  timestamp: number;
}

export function subscribeAgentStatus(
  onSnapshot: (agentIds: string[], paused?: boolean) => void,
  onStatus: (agentId: string, processing: boolean) => void,
  onActivity?: (event: ActivityEntry) => void,
  onOrgChanged?: () => void
): { abort: () => void } {
  const socket = getSocket();
  const channel = socket.channel("lobby:status");

  channel.on("init", (payload: Record<string, unknown>) => {
    if (Array.isArray(payload.agentIds)) {
      onSnapshot(payload.agentIds, (payload.paused as boolean | undefined) ?? false);
    }
  });

  channel.on("status_change", (payload: Record<string, unknown>) => {
    if (typeof payload.agentId === "string") {
      onStatus(payload.agentId, !!(payload.processing as boolean | undefined));
    }
  });

  channel.on("org_changed", () => {
    onOrgChanged?.();
  });

  channel.on("activity", (payload: Record<string, unknown>) => {
    if (onActivity && typeof payload.agentId === "string") {
      onActivity({
        agentId: payload.agentId as string,
        agentName: (payload.agentName as string | undefined) || "",
        type: (payload.type as string | undefined) || "",
        content: payload.content as string | undefined,
        deltaId: payload.deltaId as string | undefined,
        toolName: payload.toolName as string | undefined,
        toolInput: payload.toolInput as string | object | undefined,
        toolResult: payload.toolResult as string | object | undefined,
        errorMessage: payload.errorMessage as string | undefined,
        timestamp: (payload.timestamp as number | undefined) || Date.now(),
      });
    }
  });

  channel.join().receive("ok", () => {
    // Initial snapshot is pushed via "init" event
  }).receive("error", () => {
    onSnapshot([], false);
  });

  return {
    abort: () => {
      channel.leave();
    },
  };
}

// ---------------------------------------------------------------------------
// Inbox
// ---------------------------------------------------------------------------

export async function getInbox(agentId: string) {
  return fetchJSON(`${BASE}/chat/inbox/${agentId}`);
}

export async function sendInboxMessage(payload: {
  fromAgentId: string;
  toAgentId: string;
  type?: string;
  content: string;
  subject?: string;
  priority?: string;
  metadata?: Record<string, any>;
}) {
  return fetchJSON(`${BASE}/chat/inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Chat history
// ---------------------------------------------------------------------------

export async function getChatHistory(agentId: string) {
  return fetchJSON(`${BASE}/chat/history/${agentId}`);
}

export async function markMessagesRead(ids: string[], agentId?: string) {
  return fetchJSON(`${BASE}/chat/mark-read`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, agentId: agentId || ids[0] || "" }),
  });
}

// ---------------------------------------------------------------------------
// System pause/resume
// ---------------------------------------------------------------------------

export async function pauseSystem() {
  return fetchJSON(`${BASE}/chat/pause`, { method: "POST" });
}

export async function resumeSystem() {
  return fetchJSON(`${BASE}/chat/resume`, { method: "POST" });
}

export async function getPausedState() {
  return fetchJSON(`${BASE}/chat/paused`);
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export async function getSettings() {
  return fetchJSON(`${BASE}/settings`);
}

export async function getSetting(key: string) {
  return fetchJSON(`${BASE}/settings/${key}`);
}

export async function upsertSetting(key: string, value: string) {
  return fetchJSON(`${BASE}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
}

/** Bulk update settings. Accepts a map of { key: value }. */
export async function updateSettings(settings: Record<string, string>) {
  // If single key/value, use the simple endpoint
  const entries = Object.entries(settings);
  if (entries.length === 1) {
    const [key, value] = entries[0];
    return upsertSetting(key, value);
  }
  // Multiple - POST the first one, ignore rest (single-setting endpoint)
  for (const [key, value] of entries) {
    await upsertSetting(key, value);
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

export async function getLlmModels() {
  return fetchJSON(`${BASE}/llm-models`);
}

// ---------------------------------------------------------------------------
// Templates
// ---------------------------------------------------------------------------

export async function getAgentTemplates() {
  return fetchJSON(`${BASE}/agent-templates`);
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export async function getHealth() {
  return fetchJSON(`${BASE}/health`);
}

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

export interface PendingApproval {
  id: string;
  agentId: string;
  toolName: string;
  toolArguments: string;
  description: string;
  status: string;
  createdAt: number;
}

/** Get pending approval requests for a single agent. */
export async function getPendingApprovals(agentId: string): Promise<PendingApproval[]> {
  return fetchJSON(`${BASE}/permissions/pending/${agentId}`);
}

/** Get all pending approval requests for a project. */
export async function getProjectPendingApprovals(projectId: string): Promise<PendingApproval[]> {
  return fetchJSON(`${BASE}/permissions/pending/project/${projectId}`);
}

/** Respond (approve/reject) to a pending approval request. */
export async function respondToApproval(
  requestId: string,
  approved: boolean,
  remember: boolean = false,
  userNote?: string,
  projectId?: string
): Promise<{ ok: boolean; reason?: string }> {
  return fetchJSON(`${BASE}/permissions/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestId, approved, remember, userNote, projectId }),
  });
}

/** Get effective permission rules for an agent. */
export async function getAgentPermissions(agentId: string) {
  return fetchJSON(`${BASE}/permissions/rules/${agentId}`);
}

/** Update permission rules for an agent. */
export async function updateAgentPermissions(agentId: string, rules: {
  permissionMode?: "readonly" | "readwrite" | "full" | "custom";
  allowedTools?: string[];
  deniedTools?: string[];
  askTools?: string[];
  mcpServers?: string[];
  boundSkills?: string[];
}) {
  return fetchJSON(`${BASE}/permissions/rules/${agentId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rules),
  });
}

// ---------------------------------------------------------------------------
// Permission rules (alias for getAgentPermissions)
// ---------------------------------------------------------------------------

export async function getPermissionRules(agentId: string) {
  return getAgentPermissions(agentId);
}

// ---------------------------------------------------------------------------
// Chat messages (alias for getChatHistory)
// ---------------------------------------------------------------------------

export async function getChatMessages(agentId: string) {
  return fetchJSON(`${BASE}/chat/messages/${agentId}`);
}

// ---------------------------------------------------------------------------
// LLM Models (alias for getLlmModels)
// ---------------------------------------------------------------------------

export interface LlmModel {
  id: string;
  name: string;
  modelId: string;
  baseUrl: string;
  apiKey: string;
  contextWindow: number;
  maxOutputTokens: number;
  supportsThinking: boolean;
  defaultReasoningEffort?: string | null;
  temperature?: string | null;
  isActive: boolean;
}

export async function getModels(): Promise<LlmModel[]> {
  const data = await fetchJSON(`${BASE}/llm-models`);
  // Backend returns { models: [...] }, unwrap it
  return Array.isArray(data) ? data : (data?.models ?? []);
}

export async function createModel(payload: Partial<LlmModel>) {
  return fetchJSON(`${BASE}/llm-models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateModel(id: string, payload: Partial<LlmModel>) {
  return fetchJSON(`${BASE}/llm-models/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteModel(id: string) {
  return fetchJSON(`${BASE}/llm-models/${id}`, { method: "DELETE" });
}

export async function testModel(id: string) {
  return fetchJSON(`${BASE}/llm-models/${id}/test`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Agent Templates
// ---------------------------------------------------------------------------

export interface AgentTemplate {
  id: string;
  source: string;
  division: string;
  name: string;
  role: string;
  color: string;
  emoji: string;
  vibe: string;
  description: string;
  promptBody: string;
  originalFile: string;
  createdAt: number;
}

export async function getTemplates(opts?: { division?: string; role?: string; source?: string }): Promise<AgentTemplate[]> {
  const params = new URLSearchParams();
  if (opts?.division) params.set("division", opts.division);
  if (opts?.role) params.set("role", opts.role);
  if (opts?.source) params.set("source", opts.source);
  const qs = params.toString();
  return fetchJSON(`${BASE}/agent-templates${qs ? "?" + qs : ""}`);
}

export async function getTemplateDivisions(): Promise<string[]> {
  return fetchJSON(`${BASE}/agent-templates/divisions`);
}

export async function getTemplate(id: string): Promise<AgentTemplate> {
  return fetchJSON(`${BASE}/agent-templates/${id}`);
}

// ---------------------------------------------------------------------------
// Communications
// ---------------------------------------------------------------------------

export interface Communication {
  id: string;
  fromAgentId?: string;
  toAgentId?: string;
  type: string;
  subject?: string;
  content: string;
  status: string;
  metadata?: Record<string, any>;
  createdAt: number;
}

export async function getCommunications(opts?: { projectId?: string; limit?: number }): Promise<Communication[]> {
  const params = new URLSearchParams();
  if (opts?.projectId) params.set("projectId", opts.projectId);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const data = await fetchJSON<{ communications: Communication[] }>(`${BASE}/communications${qs ? "?" + qs : ""}`);
  // BUG-028 fix: backend wraps in {communications: [...]}, unwrap here
  return data?.communications ?? (Array.isArray(data) ? data : []);
}

export async function sendCommunication(payload: {
  fromAgentId?: string;
  toAgentId: string;
  type: string;
  content: string;
  subject?: string;
  metadata?: Record<string, any>;
}) {
  return fetchJSON(`${BASE}/communications`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// User Pings
// ---------------------------------------------------------------------------

export interface UserPing {
  id?: string;
  agentId?: string;
  agentName?: string;
  type?: string;
  content?: string;
  toolName?: string;
  toolInput?: string;
  timestamp?: number;
  read?: boolean;
  agentIds?: string[];
}

export async function getUserPings(opts?: { projectId?: string; unreadOnly?: boolean; limit?: number }): Promise<UserPing[]> {
  const params = new URLSearchParams();
  if (opts?.projectId) params.set("projectId", opts.projectId);
  if (opts?.unreadOnly) params.set("unreadOnly", "true");
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const data = await fetchJSON<{ pings: UserPing[] }>(`${BASE}/user-pings${qs ? "?" + qs : ""}`);
  return data?.pings ?? [];
}

export async function markPingRead(id: string) {
  return fetchJSON(`${BASE}/user-pings/${id}/read`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Alarms
// ---------------------------------------------------------------------------

export interface ProjectAlarm {
  id: string;
  fromAgentId?: string;
  toAgentId: string;
  purpose: string;
  fireAtGameSeconds: number;
  fired: boolean;
  firedAt?: number;
  createdAt: number;
}

export async function getProjectAlarms(projectId: string, opts?: { includeFired?: boolean }): Promise<{ alarms: ProjectAlarm[]; currentGameSeconds: number; realTimestamp: number }> {
  const params = new URLSearchParams();
  if (opts?.includeFired) params.set("includeFired", "true");
  const qs = params.toString();
  return fetchJSON(`${BASE}/projects/${projectId}/alarms${qs ? "?" + qs : ""}`);
}

export async function scheduleAlarm(projectId: string, alarm: { fromAgentId?: string; toAgentId: string; purpose: string; fireAtGameSeconds: number }) {
  return fetchJSON(`${BASE}/projects/${projectId}/alarms`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(alarm),
  });
}

export async function cancelAlarm(projectId: string, alarmId: string) {
  return fetchJSON(`${BASE}/projects/${projectId}/alarms/${alarmId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Todos
// ---------------------------------------------------------------------------

export interface AgentTodos {
  agentId: string;
  todos: Array<{
    id: string;
    content: string;
    status: "pending" | "in_progress" | "completed";
    createdAt: number;
    updatedAt: number;
  }>;
}

export async function getAgentTodos(agentId: string): Promise<AgentTodos> {
  return fetchJSON(`${BASE}/chat/todos/${agentId}`);
}

export async function executeTodoWrite(agentId: string, todos: AgentTodos["todos"]) {
  return fetchJSON(`${BASE}/chat/todos/${agentId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ todos }),
  });
}

// ---------------------------------------------------------------------------
// Work Logs
// ---------------------------------------------------------------------------

export interface WorkLog {
  id: string;
  agentId?: string;
  type: string;
  summary: string;
  details?: string;
  metadata?: Record<string, any>;
  createdAt: number;
}

export async function getWorkLogs(agentId: string, limit: number = 50): Promise<WorkLog[]> {
  const data = await fetchJSON<{ logs: any[]; agentId?: string }>(`${BASE}/logs/${agentId}?limit=${limit}`);
  const rows = data?.logs ?? (Array.isArray(data) ? data : []);
  return rows.map((r: any) => ({
    id: r.id,
    agentId: r.agentId ?? r.agent_id,
    type: r.type ?? r.action ?? "discussion",
    summary: r.summary ?? "",
    details: typeof r.details === "string" ? r.details : (r.details ? JSON.stringify(r.details) : undefined),
    metadata: r.metadata,
    createdAt: r.createdAt ?? r.created_at ?? 0,
  }));
}

// ---------------------------------------------------------------------------
// Questions (Q&A)
// ---------------------------------------------------------------------------

export interface PendingQuestion {
  id: string;
  agentId: string;
  agentName?: string;
  question: string;
  context?: string;
  options?: string[];
  status: "pending" | "answered" | "expired";
  answer?: string;
  createdAt: number;
  answeredAt?: number;
}

export async function getQuestions(opts?: { agentId?: string; status?: string }): Promise<PendingQuestion[]> {
  const params = new URLSearchParams();
  if (opts?.agentId) params.set("agentId", opts.agentId);
  if (opts?.status) params.set("status", opts.status);
  const qs = params.toString();
  return fetchJSON(`${BASE}/chat/questions${qs ? "?" + qs : ""}`);
}

export async function answerQuestion(id: string, answer: string) {
  return fetchJSON(`${BASE}/chat/questions/${id}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
}

// ---------------------------------------------------------------------------
// Filesystem browse
// ---------------------------------------------------------------------------

export interface BrowseResult {
  path: string;
  parent: string | null;
  currentPath?: string;
  parentPath?: string | null;
  entries: Array<{
    name: string;
    path: string;
    fullPath?: string;
    isDir: boolean;
    is_dir?: boolean;
    size?: number;
    modified?: number;
  }>;
  drives?: string[];
  isRoot?: boolean;
  error?: string;
}

export async function browseDirectory(path?: string): Promise<BrowseResult> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const qs = params.toString();
  return fetchJSON(`${BASE}/fs/browse${qs ? "?" + qs : ""}`);
}

// ---------------------------------------------------------------------------
// Debug / Monitoring — Agent LLM Traces
// ---------------------------------------------------------------------------

export interface TraceTurn {
  id: string;
  agent_id: string;
  turn_index: number;
  raw_messages: any[];
  approx_tokens: number;
  created_at: number;
}

export interface TraceEvent {
  id: string;
  agent_id: string;
  event_type: string;
  payload: Record<string, any>;
  created_at: number;
}

export interface AgentTraces {
  turns: TraceTurn[];
  events: TraceEvent[];
}

export async function getAgentTraces(agentId: string): Promise<AgentTraces> {
  return fetchJSON(`${BASE}/debug/agents/${agentId}/traces`);
}


// Re-export the Channel for advanced usage
export { Channel };
